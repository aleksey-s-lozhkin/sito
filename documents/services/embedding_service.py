"""Эмбеддинги текста через `bge-m3` на Ollama.

Зачем: полнотекстовый поиск не находит перефразированный вопрос — слова
другие, смысл тот же («как склеить две строки» против «конкатенация строк»).
На живом корпусе это 2/7 (docs/RAG-EXPERIMENT.md, раздел «Что измеряем»).
Вектор переводит вопрос и кусок в одно пространство, где близость считается
по смыслу, а не по совпадению словоформ.

**Почему `bge-m3`, а не `nomic-embed-text`, хотя та меньше.** Здесь была
ошибка, и она стоила выводов. `nomic-embed-text` — англоязычная модель, а
корпус и вопросы русские: она почти не различала русские синонимы
(«цикл for» ↔ «повторить действие несколько раз» давало 0,632 против 0,607
у полностью несвязанной пары). На `bge-m3`, многоязычной, тот же гибрид
дал 13/15 против 8/15.

**Переменной была модель, а не подход.** Отсюда правило: прежде чем
заключать «вектора не работают», убедись, что модель понимает язык корпуса.

Размерность 1024 — **проверена запросом** к `/api/embed`, а не взята из
описания модели. Обе величины (модель и размерность) задаются окружением:
`OLLAMA_EMBED_MODEL` и `OLLAMA_EMBED_DIMS` в `config/settings.py`.

Почему префиксы. Модель обучалась с указанием роли текста, и без указания
роли качество поиска падает: кусок документа и вопрос живут в разных
подобластях пространства, и сравнивать их надо в правильных подобластях.
Поэтому здесь **две** функции для двух ролей, а не одна общая: `search_document:`
для кусков, `search_query:` для вопроса. Это же требование важно соблюсти на
стороне замера — иначе вектор вопроса окажется в чужой подобласти.

HTTP — стандартной библиотекой, как в `summary_service.py`: тянуть ради двух
запросов зависимость и пересобирать замок не стоит. По той же причине здесь
нет клиента Ollama и нет новых настроек в `config/settings.py` — адрес берётся
из уже существующего `OLLAMA_URL`, остальное имеет значения по умолчанию и
читается через `getattr` (набор настроек зависит от окружения).

В отличие от описания документа (`summary_service`), здесь ошибка **не
глотается**: молча пропущенный кусок означает дырку в векторном поиске,
которую потом не видно. Наружу уходит `EmbeddingError` с причиной.
"""

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

#: Модель эмбеддингов. Уже лежит на хосте Ollama (`ollama list`), скачивать
#: при первом запросе ничего не нужно.
DEFAULT_MODEL = "bge-m3"
#: Размерность `bge-m3`. Проверяется на каждом ответе модели:
#: смена модели обязана падать громко, а не писать в индекс вектор другой
#: длины.
DEFAULT_DIMS = 1024
#: Таймаут одного HTTP-запроса. Первый запрос включает загрузку модели в
#: память, поэтому с запасом; дальше батч отвечает за доли секунды.
DEFAULT_TIMEOUT = 120
#: Сколько текстов за один HTTP-запрос. Замер на живом корпусе: батч из 16
#: кусков по ~1700 знаков отвечает быстрее, чем 16 отдельных запросов, и не
#: упирается в контекст модели.
DEFAULT_BATCH_SIZE = 16

#: Префиксы ролей. Вынесены в константы, чтобы замер брал ровно те же строки,
#: что и индексация: разойдись они — и косинус потеряет смысл.
DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


class EmbeddingError(RuntimeError):
    """Модель эмбеддингов недоступна или ответила не тем, что мы ждали."""


def _setting(name, default):
    return getattr(settings, name, default)


def is_enabled() -> bool:
    """Настроена ли модель. Пустой адрес — выключено (как у описаний)."""
    return bool(_setting("OLLAMA_URL", ""))


def model_name() -> str:
    """Имя модели эмбеддингов. Меняется настройкой, а не правкой кода."""
    return _setting("OLLAMA_EMBED_MODEL", DEFAULT_MODEL)


def dimension() -> int:
    """Ожидаемая длина вектора. По ней создаётся поле в маппинге индекса."""
    return int(_setting("OLLAMA_EMBED_DIMS", DEFAULT_DIMS))


def batch_size() -> int:
    """Размер батча. Ноль и меньше смысла не имеют — считаем единицей."""
    return max(1, int(_setting("OLLAMA_EMBED_BATCH_SIZE", DEFAULT_BATCH_SIZE)))


def _embed_batch(texts, model):
    """Один HTTP-запрос к Ollama на пачку текстов. Возвращает список векторов."""
    url = f"{_setting('OLLAMA_URL', '').rstrip('/')}/api/embed"
    payload = {"model": model, "input": list(texts)}

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(
            request, timeout=_setting("OLLAMA_EMBED_TIMEOUT_SECONDS", DEFAULT_TIMEOUT)
        ) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 404 — самая частая причина: модель не скачана на хост. Отвечаем
        # человеку тем, что ему делать, а не кодом ошибки.
        if exc.code == 404:
            raise EmbeddingError(
                f"Модель «{model}» не найдена на {_setting('OLLAMA_URL', '')}: "
                f"нужно `ollama pull {model}` на хосте Ollama"
            ) from exc
        raise EmbeddingError(f"Ollama ответила ошибкой {exc.code} на {url}") from exc
    # TimeoutError и URLError — подклассы OSError, перечислять их отдельно нечего.
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise EmbeddingError(
            f"Модель эмбеддингов недоступна по адресу {_setting('OLLAMA_URL', '') or '(OLLAMA_URL пуст)'}: {exc}"
        ) from exc

    embeddings = body.get("embeddings") or []
    if len(embeddings) != len(texts):
        # Модель ответила не на все тексты. Это либо другая модель, либо
        # другая версия API: продолжать нельзя, иначе вектора сдвинутся
        # относительно кусков и поиск будет находить не то.
        raise EmbeddingError(f"Модель «{model}» вернула {len(embeddings)} векторов на {len(texts)} текстов")
    return embeddings


def _check_dims(vectors) -> None:
    """Сверить длину вектора с ожидаемой. Молча писать чужую длину нельзя."""
    expected = dimension()
    for vector in vectors:
        if len(vector) != expected:
            raise EmbeddingError(
                f"Модель «{model_name()}» вернула вектор длины {len(vector)}, "
                f"а поле в индексе рассчитано на {expected}. Смена модели требует "
                f"нового поля и пересборки индекса, а не тихой записи."
            )


def embed_texts(texts, model=None, prefix=DOCUMENT_PREFIX) -> list:
    """Векторы для списка текстов.

    Порядок и длина результата совпадают со входом — на этом стоит запись в
    индекс батчами: кусок и его вектор нельзя перепутать местами.

    `prefix` выбирает роль текста (кусок или вопрос). По умолчанию роль
    куска: основная работа сервиса — индексировать куски.
    """
    prepared = [(text or "").strip() for text in texts]
    if not prepared:
        return []
    if not is_enabled():
        raise EmbeddingError("OLLAMA_URL не задан — эмбеддинги не настроены")

    model = model or model_name()
    vectors = []
    size = batch_size()
    for start in range(0, len(prepared), size):
        # Модель принимает список текстов одним запросом: это в разы меньше
        # накладных расходов, чем запрос на каждый кусок.
        batch = [prefix + text for text in prepared[start : start + size]]
        vectors.extend([[float(value) for value in vector] for vector in _embed_batch(batch, model)])

    _check_dims(vectors)
    return vectors


def embed_document(text):
    """Вектор куска. `None` для пустого текста: у пустоты нет смысла.

    Пустой кусок в корпусе — следствие неудачной нарезки; записывать ему
    вектор значит сделать вид, что он на что-то отвечает.
    """
    if not (text or "").strip():
        return None
    return embed_texts([text], prefix=DOCUMENT_PREFIX)[0]


def embed_query(text):
    """Вектор вопроса. Роль другая — и префикс другой.

    Замер (tools/compare_variants.py) обязан спрашивать вектор вопроса
    отсюда: посчитанный без префикса или с префиксом документа он ляжет в
    чужую подобласть, и вариант «только вектора» провалится по нашей вине,
    а не по своей.
    """
    if not (text or "").strip():
        raise EmbeddingError("Пустой запрос нечего векторизовать")
    return embed_texts([text], prefix=QUERY_PREFIX)[0]
