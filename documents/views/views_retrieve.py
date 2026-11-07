"""Поиск отрывков для потребителей поиска.

Форма границы зафиксирована в docs/SEARCH-CONTRACT.md и меняется только вместе
с потребителями (Семён и лапот). Здесь — реализация этой формы: кого искать
решает токен, порог принадлежит поиску, отказ Elasticsearch — это 503, а пустой
результат — нормальный ответ 200.
"""

import logging
from collections import Counter

from drf_spectacular.utils import OpenApiResponse, extend_schema
from elasticsearch.exceptions import NotFoundError, TransportError
from elasticsearch_dsl import Q, Search, connections
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from documents.auth import ApiTokenAuthentication, HasApiToken
from documents.models import ApiToken
from documents.serializers import RetrieveRequestSerializer, RetrieveResponseSerializer
from documents.services.chunk_service import CHUNKS_INDEX

logger = logging.getLogger(__name__)

#: Границы запроса — из контракта (§2).
MIN_QUERY_LENGTH = 3
MAX_QUERY_LENGTH = 1000
DEFAULT_LIMIT = 10
MAX_LIMIT = 50

#: Не больше трёх отрывков одного документа в выдаче: иначе один документ
#: займёт весь ответ (контракт, §6).
MAX_CHUNKS_PER_DOCUMENT = 3

#: Сколько кусков запрашивать у Elasticsearch, чтобы после отсечения по
#: документу и по лимиту осталось нужное количество.
FETCH_FACTOR = 4

#: Порог отсечения по близости. Пока векторной части нет, а у BM25 нет
#: абсолютной шкалы: ноль уже означает «совпадений нет». Порог применяется
#: здесь, а не у потребителя, потому что измерять и настраивать его должен
#: поиск (контракт, §5).
VECTOR_MIN_SCORE = None

#: Поле с вектором. Имя задано маппингом (`documents/documents.py`).
VECTOR_FIELD = "dense_vector"

#: Постоянная сглаживания RRF. Обычное значение 60; проверено, что результат
#: не меняется при 10 и 100 — то есть выбор не подгонка.
RRF_K = 60


class RetrieveView(APIView):
    """POST /api/v1/search/retrieve — отрывки документов для потребителей."""

    authentication_classes = [ApiTokenAuthentication]
    permission_classes = [HasApiToken]

    @extend_schema(
        summary="Поиск отрывков документов",
        description=(
            "Отдаёт отрывки документов, найденные по запросу. Кого искать — решает токен: "
            "служебный видит только публичные документы, персональный — свои и публичные. "
            "Пустой results при 200 означает «не нашлось»; 503 — поиск недоступен, и "
            "потребитель обязан сказать об этом, а не отвечать по памяти модели."
        ),
        request=RetrieveRequestSerializer,
        responses={
            200: RetrieveResponseSerializer,
            400: OpenApiResponse(description="Ошибка потребителя: запрос вне границ контракта"),
            503: OpenApiResponse(description="Поиск недоступен: Elasticsearch или индекс отрывков"),
        },
    )
    def post(self, request):
        # Идентификатор пользователя в запросе запрещён контрактом (§2): иначе
        # ошибка в любом потребителе открывала бы чужие документы. Отвечаем
        # явной ошибкой, а не молча игнорируем поле.
        if "user_id" in request.data or "user" in request.data:
            return Response(
                {"error": "Кого искать — решает токен; идентификатор пользователя в запросе не принимается"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        query = (request.data.get("query") or "").strip()
        if not MIN_QUERY_LENGTH <= len(query) <= MAX_QUERY_LENGTH:
            return Response(
                {"error": f"query: от {MIN_QUERY_LENGTH} до {MAX_QUERY_LENGTH} знаков"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            limit = int(request.data.get("limit", DEFAULT_LIMIT))
        except (TypeError, ValueError):
            return Response({"error": "limit: целое число"}, status=status.HTTP_400_BAD_REQUEST)
        if limit < 1:
            return Response({"error": "limit: не меньше единицы"}, status=status.HTTP_400_BAD_REQUEST)
        limit = min(limit, MAX_LIMIT)

        rubrics = request.data.get("rubrics") or []
        if not isinstance(rubrics, list) or any(not isinstance(rubric, str) for rubric in rubrics):
            return Response({"error": "rubrics: список строк"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            hits, source = self._hybrid_hits(request.auth, query, rubrics, limit)
        except NotFoundError:
            # Индекс не создан — это не «ничего не нашлось», а неполадка:
            # пустой ответ читался бы как «материала нет».
            logger.error("Индекс кусков %s не создан", CHUNKS_INDEX)
            return Response(
                {"error": "Индекс отрывков не создан: нужен пересбор индекса"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except TransportError as exc:
            logger.warning("Поиск отрывков недоступен: %s", exc)
            return Response(
                {"error": "Поиск недоступен"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        results = self._collect(hits, limit)
        logger.info("Поиск отрывков: «%s» → %s результатов, источник %s", query, len(results), source)
        # `source` — чем именно нашли. Контракт (§3) требует говорить это
        # честно: «vector» значит, что полнотекст не дал ничего, «fulltext» —
        # что вектора недоступны.
        return Response({"results": results, "source": source})

    @staticmethod
    def _visible_clauses(api_token) -> list:
        """Кому что видно — следствие токена, а не параметр запроса (§2)."""
        clauses = [Q("term", is_public=True)]
        if api_token.scope == ApiToken.SCOPE_PERSONAL and api_token.user_id:
            clauses.append(Q("term", user_id=api_token.user_id))
        return clauses

    @staticmethod
    def _visible_body(api_token) -> dict:
        """То же самое словарём: внутрь `knn` объект DSL не положить."""
        return {
            "bool": {
                "should": [clause.to_dict() for clause in RetrieveView._visible_clauses(api_token)],
                "minimum_should_match": 1,
            }
        }

    @staticmethod
    def _build_search(api_token, query, rubrics, limit):
        """Полнотекстовый запрос к индексу кусков."""
        search = Search(index=CHUNKS_INDEX)

        search = search.query(
            "multi_match",
            query=query,
            # Текст весомее заголовка: заголовок часто повторяет рубрику.
            fields=["text^3", "title^2"],
            # `and` и 70 % — **намеренно строго**, и это замерено: мягкий
            # вариант (operator=or, 50 %) на маленьком корпусе выглядел
            # лучше, а на 329 кусках не отличался от базового. Строгий
            # полнотекст лучше держит точные совпадения, а промахи по
            # перефразированным закрывает векторная половина.
            operator="and",
            fuzziness="AUTO",
            minimum_should_match="70%",
        )

        search = search.query("bool", should=RetrieveView._visible_clauses(api_token), minimum_should_match=1)

        if rubrics:
            search = search.query(
                "bool",
                should=[Q("match_phrase", rubrics=rubric) for rubric in rubrics],
                minimum_should_match=1,
            )

        if VECTOR_MIN_SCORE is not None:
            search = search.extra(min_score=VECTOR_MIN_SCORE)
        else:
            # Полнотекстовая половина может не найти ничего — тогда гибрид
            # обязан опереться на вектора, а не вернуть пустоту.
            pass

        return search[: max(limit * FETCH_FACTOR, limit)]

    @staticmethod
    def _knn_body(api_token, query, size, rubrics=None) -> dict:
        """Векторный запрос телом, а не объектом DSL.

        Почему так, а не `search.query("knn", ...)`: в замке клиент 7.17, и
        `knn` как clause он не знает — отвечает `UnknownDslObject`. Параметр
        верхнего уровня появился в 8.x и через `.extra()` недоступен.

        **Фильтр видимости обязан быть ВНУТРИ `knn`.** Это проверено на
        живом индексе: с top-level фильтром запрос возвращает чужие куски,
        потому что `knn` верхнего уровня `query` не учитывает. Для проекта,
        где изоляция документов держится на ADR-0001, это прямой доступ к
        чужому, а не недочёт.

        По той же причине **рубрики фильтруются внутри `knn`**: снаружи
        векторная половина их не увидит и вернёт чужие рубрики, а ответ
        честно назовёт себя `hybrid`.

        `_source` обязан включать `text`: отрывок без текста потребителю
        бесполезен — по нему нельзя ответить. Раньше векторные попадания
        приходили с `"text": null`, а слияние по рангам брало текстовую
        версию только при совпадении.
        """
        from documents.services.embedding_service import embed_query

        filter_body = RetrieveView._visible_body(api_token)
        if rubrics:
            filter_body = {
                "bool": {
                    "filter": [
                        filter_body,
                        {
                            "bool": {
                                # Та же семантика, что у полнотекстовой
                                # половины: «хотя бы одна из рубрик».
                                "should": [{"match_phrase": {"rubrics": rubric}} for rubric in rubrics],
                                "minimum_should_match": 1,
                            }
                        },
                    ]
                }
            }

        return {
            "knn": {
                "field": VECTOR_FIELD,
                "query_vector": embed_query(query),
                "k": size,
                "num_candidates": max(size * 10, 100),
                "filter": filter_body,
            },
            "_source": [
                "document_id",
                "chunk_index",
                "chunk_total",
                "document_version",
                "title",
                "text",
                "rubrics",
                "is_public",
                "user_id",
            ],
            "size": size,
        }

    @staticmethod
    def _hit_to_dict(hit) -> dict:
        """Один отрывок в общем виде — чтобы слияние не знало, откуда он."""
        source = hit if isinstance(hit, dict) else hit.to_dict()
        source = source.get("_source", source)
        return {
            "document_id": source.get("document_id"),
            "chunk_index": source.get("chunk_index"),
            "chunk_total": source.get("chunk_total"),
            "document_version": source.get("document_version"),
            "title": source.get("title"),
            "text": source.get("text"),
            "rubrics": list(source.get("rubrics") or []),
            "is_public": bool(source.get("is_public")),
            "user_id": source.get("user_id"),
        }

    @staticmethod
    def _fuse_rrf(*ranked_lists, k: int = RRF_K) -> list:
        """Слить выдачи по рангам (Reciprocal Rank Fusion).

        Почему по рангам, а не по очкам: BM25 и косинус живут на **разных
        шкалах**, и складывать их напрямую — складывать метры с
        килограммами. RRF складывает `1 / (k + место)`, то есть **места**,
        и нормировка не нужна.

        Замер этого и требует. На 329 кусках:
        `hybrid_rrf_strict` дал 13/15 против 10/15 у одного полнотекста,
        а слияние по очкам (`hybrid_weighted`) — 9/15.
        """
        scores: dict = {}
        items: dict = {}
        for ranked in ranked_lists:
            for rank, item in enumerate(ranked, start=1):
                key = (item["document_id"], item["chunk_index"])
                scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
                items.setdefault(key, item)

        order = sorted(scores.items(), key=lambda kv: -kv[1])
        return [items[key] for key, _ in order]

    @staticmethod
    def _hybrid_hits(api_token, query, rubrics, limit) -> tuple[list, str]:
        """Полнотекст плюс вектора. Возвращает отрывки и честный источник."""
        size = max(limit * FETCH_FACTOR, limit)

        text_hits: list = []
        try:
            response = RetrieveView._build_search(api_token, query, rubrics, limit).execute()
            text_hits = [RetrieveView._hit_to_dict(hit) for hit in response]
        except (NotFoundError, TransportError):
            # Индекс или связь недоступны — это не «не нашлось», и решает
            # вызывающий. Векторную половину всё равно пробуем: вдруг жива.
            raise

        knn_hits: list = []
        try:
            body = RetrieveView._knn_body(api_token, query, size, rubrics)
            raw = connections.get_connection().search(index=CHUNKS_INDEX, body=body)
            knn_hits = [RetrieveView._hit_to_dict(hit) for hit in raw["hits"]["hits"]]
        except Exception as exc:  # noqa: BLE001
            # Вектора — **дополнение**, а не замена. Их отказ не должен
            # ломать поиск: без них работает полнотекст, и об этом честно
            # сообщается в `source`.
            logger.warning("Векторная половина недоступна, отвечаю полнотекстом: %s", exc)
            return text_hits, "fulltext"

        if not knn_hits:
            return text_hits, "fulltext"
        if not text_hits:
            return knn_hits, "vector"

        return RetrieveView._fuse_rrf(text_hits, knn_hits), "hybrid"

    @staticmethod
    def _collect(hits, limit) -> list:
        """Собрать ответ, не давая одному документу занять всю выдачу.

        На входе — словари: после слияния отрывки приходят из двух разных
        источников, и у них уже нет ни `meta.score`, ни атрибутов объекта
        DSL. Приводим к общему виду **до** слияния, чтобы здесь не знать,
        откуда что пришло.
        """
        results = []
        per_document = Counter()

        for hit in hits:
            document_id = hit["document_id"]
            if per_document[document_id] >= MAX_CHUNKS_PER_DOCUMENT:
                continue
            per_document[document_id] += 1

            results.append(
                {
                    "document_id": document_id,
                    "chunk_index": hit["chunk_index"],
                    "chunk_total": hit["chunk_total"],
                    # Ответ на §8.3 контракта: по отпечатку видно, что документ
                    # изменился и отрывок мог устареть.
                    "document_version": hit.get("document_version"),
                    "title": hit.get("title"),
                    "text": hit.get("text"),
                    # Очки намеренно не отдаём: после слияния по рангам это
                    # уже не BM25 и не косинус, а сумма обратных мест. Шкалы
                    # разных источников несравнимы (docs/ARCHITECTURE.md), и
                    # число, которое ничего не значит, хуже его отсутствия.
                    "score": None,
                    "rubrics": hit.get("rubrics") or [],
                    "is_public": bool(hit.get("is_public")),
                }
            )
            if len(results) >= limit:
                break

        return results
