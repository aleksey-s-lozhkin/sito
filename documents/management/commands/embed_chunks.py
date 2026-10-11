"""Считает эмбеддинги кусков и кладёт их в поле `dense_vector` индекса.

Зачем отдельная команда, а не работа при загрузке документа. Загрузка не
должна зависеть от Ollama — тот же принцип, по которому описание документа
считается в фоне (documents/signals.py). Плюс эмбеддинги нужны не всем
потребителям: полнотекстовый поиск работает и без них. Поэтому шаг явный.

**Порядок шагов, и это неочевидно.** `reindex_chunks` пересоздаёт индекс из
текста документов и о векторах не знает: после него куски снова без векторов.
Значит после пересборки индекса эту команду нужно запустить снова. Команда
это переживает спокойно — она берёт только те куски, у которых вектора нет.

**Идемпотентность.** Повторный запуск ничего не пересчитывает: выборка
отбирает куски без поля `dense_vector`, а не все подряд. Поэтому команду
можно запускать после каждой загрузки документа и после обрыва — она
продолжит с того места, где остановилась (поиск идёт по текущему состоянию
индекса, а не по сохранённому курсору).

**Маппинг.** Поле `dense_vector` добавляется в индекс прямо здесь
(`PUT _mapping`), если его там ещё нет. Это неразрушающая операция: Elasticsearch
разрешает добавлять новые поля в существующий индекс, пересоздавать индекс и
переиндексировать 28 живых кусков ради одного поля не нужно. Размерность
берётся из сервиса эмбеддингов — там же, где её проверяет сама модель.
"""

import time

from django.core.management.base import BaseCommand, CommandError
from elasticsearch.exceptions import NotFoundError, TransportError
from elasticsearch.helpers import BulkIndexError, bulk
from elasticsearch_dsl.connections import connections

from documents.services import embedding_service
from documents.services.chunk_service import CHUNKS_INDEX

#: Поля, по которым идёт постраничный обход. Пара (документ, номер куска) —
#: естественный ключ куска в индексе: она уникальна и не меняется, поэтому
#: search_after по ней не пропускает и не повторяет записи, даже когда мы
#: правим документы прямо во время обхода.
SORT_KEYS = [{"document_id": "asc"}, {"chunk_index": "asc"}]

#: Отпечаток поля вектора в маппинге. Косинус, а не скалярное произведение:
#: длина текста у кусков разная, и близость не должна зависеть от того, что
#: кусок длиннее.
VECTOR_FIELD = "dense_vector"


class Command(BaseCommand):
    help = "Посчитать эмбеддинги кусков (bge-m3) и записать их в индекс"

    def add_arguments(self, parser):
        parser.add_argument(
            "--index",
            default=CHUNKS_INDEX,
            help=f"Индекс кусков (по умолчанию {CHUNKS_INDEX}; для замеров — chunks_experiment)",
        )
        parser.add_argument("--batch", type=int, default=64, help="Сколько кусков брать и записывать за проход")
        parser.add_argument(
            "--force",
            action="store_true",
            help="Пересчитать вектора и у кусков, где они уже есть (нужно после смены модели)",
        )
        parser.add_argument("--quiet", action="store_true", help="Не печатать прогресс по батчам")

    def handle(self, *args, **options):
        if not embedding_service.is_enabled():
            raise CommandError("OLLAMA_URL не задан — эмбеддинги не настроены")

        index = options["index"]
        batch = max(1, options["batch"])
        model = embedding_service.model_name()

        client = connections.get_connection()
        try:
            self._ensure_mapping(client, index)
        except NotFoundError:
            raise CommandError(f"Индекс «{index}» не создан. Сначала: python manage.py reindex_chunks")
        except TransportError as exc:
            raise CommandError(f"Elasticsearch недоступен: {exc}")

        self.stdout.write(f"Модель: {model}, размерность: {embedding_service.dimension()}, индекс: {index}")

        try:
            total_missing = self._count(client, index, options["force"])
        except TransportError as exc:
            raise CommandError(f"Elasticsearch недоступен: {exc}")

        if total_missing == 0:
            self.stdout.write(self.style.SUCCESS("Все куски уже с векторами — пересчитывать нечего."))
            return

        self.stdout.write(f"Кусков к обработке: {total_missing}")

        started = time.monotonic()
        processed = 0
        empty = 0
        after = None

        while True:
            try:
                hits = self._find_chunks(client, index, batch, options["force"], after)
            except TransportError as exc:
                raise CommandError(f"Elasticsearch недоступен: {exc}")

            if not hits:
                break

            actions = []
            for hit in hits:
                text = (hit["_source"].get("text") or "").strip()
                try:
                    vector = embedding_service.embed_document(text)
                except embedding_service.EmbeddingError as exc:
                    # Ничего не записали в этом проходе — выходим с причиной.
                    # Частично записанные батчи остаются в индексе: команда
                    # идемпотентна, повторный запуск доберёт остальное.
                    raise CommandError(f"Эмбеддинги не посчитаны: {exc}")
                if vector is None:
                    # Пустой кусок пропускаем: у пустоты нет смысла, а команда
                    # не должна падать из-за одной плохой нарезки.
                    empty += 1
                    continue
                actions.append(
                    {
                        "_op_type": "update",
                        "_index": index,
                        "_id": hit["_id"],
                        "doc": {VECTOR_FIELD: vector},
                    }
                )

            if actions:
                try:
                    bulk(client, actions, refresh=False)
                except (BulkIndexError, TransportError) as exc:
                    # Куска может не стать между выборкой и записью —
                    # переиндексация документа идёт параллельно. Это гонка, а
                    # не сломанный запрос: команда идемпотентна, повторный
                    # запуск доберёт всё, чего не хватает.
                    raise CommandError(
                        f"Не удалось записать вектора в индекс «{index}»: {exc}. "
                        f"Часть кусков могла обновиться — запустите команду снова."
                    )
            processed += len(actions)

            # Курсор — по последнему просмотренному куску, а не по последнему
            # записанному: иначе пустой кусок (его мы пропустили) зациклил бы
            # обход на самом себе.
            after = hits[-1].get("sort")

            if not options["quiet"]:
                elapsed = time.monotonic() - started
                self.stdout.write(f"  {processed}/{total_missing} кусков, {elapsed:.1f} с")

        # Один refresh в конце: knn-поиск должен сразу видеть новые вектора.
        client.indices.refresh(index=index)

        elapsed = time.monotonic() - started
        self.stdout.write(
            self.style.SUCCESS(
                f"Готово: посчитано векторов {processed}, пустых кусков пропущено {empty}, "
                f"время {elapsed:.1f} с. Модель: {model}"
            )
        )

    @staticmethod
    def _query(force) -> dict:
        """Какие куски брать.

        Без `--force` — только те, у которых поля с вектором ещё нет: это и
        есть идемпотентность. У пустого списка `must_not` запрос вырождается
        в «всё подряд», поэтому случай `--force` отдельный и явный.
        """
        if force:
            return {"match_all": {}}
        return {"bool": {"must_not": [{"exists": {"field": VECTOR_FIELD}}]}}

    def _count(self, client, index, force) -> int:
        """Сколько кусков ждёт обработки. Нужно для прогресса и итога.

        Через `body`, а не именованными аргументами: у клиента elasticsearch-py
        7.x (он в замке проекта, хотя сервер восьмой) набор именованных
        аргументов запроса уже, чем у восьмого.
        """
        response = client.count(index=index, body={"query": self._query(force)})
        return int(response.get("count", 0))

    def _find_chunks(self, client, index, batch, force, after) -> list:
        """Очередная страница кусков. Без `after` — с начала."""
        body = {
            "query": self._query(force),
            # Текст нужен, остальные поля — нет: их не читаем и не тащим.
            "_source": ["text"],
            "size": batch,
            "sort": SORT_KEYS,
        }
        if after:
            body["search_after"] = after
        response = client.search(index=index, body=body)
        return response["hits"]["hits"]

    def _ensure_mapping(self, client, index) -> None:
        """Добавить поле вектора в индекс, если его там нет.

        Elasticsearch не даёт менять существующие поля, но новые добавляет
        свободно. Поэтому пересоздавать индекс не нужно — и не нужно терять
        уже посчитанные куски и их вектора.
        """
        mapping = client.indices.get_mapping(index=index)
        properties = mapping[index]["mappings"].get("properties", {})
        if VECTOR_FIELD in properties:
            return

        # Через `body`, а не `properties`: у клиента elasticsearch-py 7.x (он
        # в замке проекта, хотя сервер восьмой) `put_mapping` принимает только
        # тело запроса.
        client.indices.put_mapping(
            index=index,
            body={
                "properties": {
                    VECTOR_FIELD: {
                        "type": "dense_vector",
                        "dims": embedding_service.dimension(),
                        "index": True,
                        "similarity": "cosine",
                    }
                }
            },
        )
        self.stdout.write(f"Поле {VECTOR_FIELD} добавлено в индекс {index} (пересоздание не понадобилось).")
