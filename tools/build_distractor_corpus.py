#!/usr/bin/env python
"""Корпус с отвлекающими кусками: проверка поиска на масштабе.

## Зачем

Живой корпус — 28 кусков. На нём видно направление, но не видно, что будет на
тысяче: поиск может находить нужное среди двадцати семи кусков и разваливаться
среди тысячи похожих. Этот скрипт собирает **второй, искусственно раздутый
корпус**: к настоящим кускам добавляется несколько сотен **настоящих, но
других** текстов про Python, и те же 15 вопросов должны по-прежнему находить
правильные ответы (docs/RAG-EXPERIMENT.md, «Проверка на масштабе»).

## Почему текст настоящий, а не сгенерированный моделью

Требование к отвлечениям — **правдоподобность**. Случайный текст сделает
различие слишком лёгким, и замер соврёт в хорошую сторону.

Соблазн взять `qwen3:8b` понятен: это одна команда и никакой возни с разбором
HTML. Но у генерации два изъяна, и оба бьют именно по честности замера:

1. **Тот же источник, что и у вопросов.** Если вопросы писались с оглядкой на
   модель, а отвлечения — той же моделью, то её словарь и её обороты окажутся
   по обе стороны замера. Часть «попаданий» будет попаданиями по стилю, а не
   по смыслу.
2. **Модель отвечает на вопросы вместо нас.** Попроси её написать про строки и
   словари — она напишет и `+`, и `.strip()`, и `.items()`, потому что «так
   принято объяснять». Отвлечения превратятся в ответы, и замер провалится не
   потому, что поиск плох, а потому, что корпус подсунул правильные ответы в
   чужие документы.

Поэтому берём **настоящие русскоязычные тексты про Python** (учебник code.mu и
статьи ru.wikipedia.org) и нарезаем их **тем же нарезчиком, что и прод**
(`documents.chunking.split_into_chunks`, цель 1800 знаков) — тогда куски
отвлечений неотличимы от настоящих по размеру и форме.

## Что получается на выходе

Индекс `chunks_experiment` с тем же маппингом и **тем же русским анализатором**,
что у живого `chunks`:

* настоящие 28 кусков (копия, только чтение из `chunks`);
* несколько сотен отвлечённых кусков из чужих документов.

Живой индекс `chunks` скрипт **не меняет**: он только читает из него маппинг и
тексты настоящих кусков. Запись идёт исключительно в `chunks_experiment`, и
имя индекса защищено проверкой (см. `guard_index_name`).

## Как запускать

Одной командой, из корня репозитория:

    python tools/build_distractor_corpus.py

Elasticsearch закрыт в docker-сети `infra`, локально его не видно. Скрипт сам
поднимает ssh-туннель до хоста и закрывает его на выходе; внутри контейнера
(`docker exec -w /app -e PYTHONPATH=/app diploma-web ...`) туннель не нужен —
там ES виден напрямую, скрипт это поймёт по успешному пингу.

Полезные режимы:

    # только собрать корпус и посмотреть статистику, в Elasticsearch не писать
    python tools/build_distractor_corpus.py --no-write-index --preview 3

    # только отвлечения, без настоящих кусков (для проверки «по одному корпусу отвлечений»)
    python tools/build_distractor_corpus.py --no-real

    # без векторов (быстро; но тогда knn-варианты на этом индексе не померить)
    python tools/build_distractor_corpus.py --no-embed

    # снять с отвлечений лишнюю тему и прогнать замер заново, не правя код
    python tools/build_distractor_corpus.py --exclude-slug /supreme/functions/

Сколько кусков собирать — `--target-chunks` (по умолчанию 300), откуда брать
текст — `--max-pages`, `--wikipedia/--no-wikipedia`.

Готовый индекс предназначен для замера вариантов поиска на масштабе:

    docker exec -w /app -e PYTHONPATH=/app -e COMPARE_INDEX=chunks_experiment \
        diploma-web python /tmp/compare_variants.py

## Что скрипт проверяет сам

1. **15 вопросов по одному корпусу отвлечений.** Правильные документы
   находиться не должны: если находятся — отвлечения плохие.
2. **Утечка текста.** Ни один отвлечённый кусок не должен делить с настоящими
   длинный дословный фрагмент: значит, он не переписан из них и не отвечает
   чужими словами на тот же вопрос. Нарушители выбрасываются.
3. **Совпадение имён.** Заголовок отвлечённого документа не должен содержать
   имени настоящего: иначе замер зачтёт чужой кусок как правильный ответ.
4. **Конкурентность.** Сколько отвлечений вообще откликается на каждый вопрос и
   с каким очком. Ноль откликов означал бы, что корпус слишком лёгкий.
5. **Те же 15 вопросов по объединённому корпусу** — та метрика, ради которой всё
   и делается: дословные и перефразированные.

Всё найденное печатается в конце, включая машиночитаемую сводку.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import re
import socket
import statistics
import subprocess
import sys
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

# --- Окружение -------------------------------------------------------------
#
# Django настраиваем до всего остального: нарезчик и сервис эмбеддингов живут в
# проекте, а скрипт — нет. Корень репозитория кладём в sys.path явно: при
# запуске `python tools/...` в sys.path попадает только `tools/`, и `config`
# оттуда не импортируется.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from elasticsearch import Elasticsearch  # noqa: E402
from elasticsearch.helpers import bulk  # noqa: E402
from elasticsearch_dsl.connections import connections  # noqa: E402

from documents.chunking import split_into_chunks  # noqa: E402
from documents.models import ApiToken  # noqa: E402
from documents.views import views_retrieve  # noqa: E402

# Клиент Elasticsearch печатает в журнал каждый запрос («POST … [status:200]»).
# В отчёте это шум: десятки строк между дельными сообщениями. Сам скрипт
# журналом не пользуется — он печатает ход работы через `log()`.
logging.getLogger("elasticsearch").setLevel(logging.WARNING)
logging.getLogger("elasticsearch_dsl").setLevel(logging.WARNING)

#: Живой индекс. Только чтение: замер не имеет права менять прод.
LIVE_INDEX = "chunks"

#: Индекс замера. Отдельный, чтобы провал эксперимента не задел живой поиск.
DEFAULT_EXPERIMENT_INDEX = "chunks_experiment"

#: С какого номера начинаются отвлечённые документы.
#:
#: Настоящие документы нумеруются базой по порядку (1, 2, 3…), и рано или
#: поздно они дойдут до миллиона. Разрыв в десять миллионов оставляет место
#: расти, а проверка «правильные документы не должны находиться» получает
#: дешёвый способ отличить свои куски от чужих: фильтр по `document_id`.
DISTRACTOR_ID_BASE = 10_000_000

#: Владелец отвлечённых кусков. Настоящие куски принадлежат первому
#: пользователю (`user_id: 1`, `is_public: true`) — отвлечения делаем такими же,
#: иначе они были бы видны не тем токеном, и замер сравнивал бы разные вещи.
REAL_USER_ID = 1

#: Имя поля вектора по умолчанию. В проекте это `dense_vector`
#: (`documents/documents.py`, `ChunkIndex`), и замер ищет ровно по нему.
DEFAULT_VECTOR_FIELD = "dense_vector"

#: Размерность вектора `nomic-embed-text`. Проверяется сервисом эмбеддингов;
#: здесь только значение для маппинга, если сервис недоступен.
FALLBACK_DIMS = 768

#: Сколько знаков скачанного текста приходится в среднем на один готовый кусок.
#: Замер на выборке из 150 страниц: страница учебника даёт около 1100 знаков, а
#: кусков получается 0,63 на страницу, то есть кусок стоит примерно 1750 знаков
#: сырья. Часть текста уходит в короткие огрызки, которые отсеиваются по
#: минимальной длине, и этот запас нужно учитывать при остановке скачивания.
CHARS_PER_CHUNK = 1750

#: Заголовок User-Agent только латиницей: HTTP-заголовки кодируются latin-1, и
#: русские буквы в них роняют запрос ещё до отправки.
USER_AGENT = "SitoRagExperiment/1.0 (+local retrieval measurement; one page at a time)"

# --- Источники -------------------------------------------------------------
#
# Как выбирались. Нужны **настоящие русскоязычные тексты про Python** на темы
# рядом с нашими вопросами, но про другое. Отвечает требованиям учебник
# code.mu: он написан человеком, на русском, разбит на короткие уроки, и в нём
# есть ровно те разделы, которые нужны, — срезы, включения, исключения,
# окружения, модули, регулярные выражения, файлы, даты, классы. Плюс несколько
# статей Википедии на темы, которых в учебнике нет (декораторы, asyncio,
# тестирование).

#: Страницы-оглавления разделов. Из них вытаскиваются ссылки на уроки: так
#: список источников не надо переписывать руками (шестьсот ссылок в коде —
#: это не поддержка, а свалка).
CODE_MU_INDEXES = [
    "https://code.mu/ru/python/book/prime/",
    "https://code.mu/ru/python/book/supreme/",
    "https://code.mu/ru/python/book/oop/",
    "https://code.mu/ru/python/manual/",
]

#: Префиксы, внутри которых лежат уроки (сами оглавления сюда не попадают:
#: у них глубина меньше).
CODE_MU_PREFIXES = [
    "/ru/python/book/prime/",
    "/ru/python/book/supreme/",
    "/ru/python/book/oop/",
    "/ru/python/manual/",
]

#: Что **исключаем** из отвлечений и почему.
#:
#: Отвлечение обязано быть похожим по теме, но **не содержать ответа**.
#: Поэтому выбрасываются уроки, которые для наших пятнадцати вопросов
#: являются каноническим ответом: про длину строки, про конкатенацию, про
#: `lambda`, про `.items()`, про `strip()` и так далее. Остальное — можно:
#: текст про срезы или регулярные выражения ни на один наш вопрос не отвечает,
#: но ищется по тем же словам («строка», «список», «значение»), а именно это и
#: создаёт трудность.
EXCLUDE_SLUGS = (
    # «конкатенация строк», «как склеить две строки» — канонические ответы.
    "strings-concatenation",
    "string-concatenation",
    "shortened-operations",  # += для строк — тоже склейка
    "/manual/string/join/",
    "string-merging",
    # «как определить длину строки», «сколько символов в слове».
    "/length/",
    "string-length",
    "/manual/iter/len/",
    # «переменные типа bool».
    "boolean-values",
    # «лямбда-функции», «короткая функция без имени»: страницы, где lambda
    # показывают как пример (map/filter/callbacks), содержат ответ не хуже.
    "lambda",
    "/functions/map/",
    "/functions/filter/",
    "/functions/callbacks/",
    "/manual/iter/map/",
    "/manual/iter/filter/",
    # «как объявить функцию def», «как вернуть значение из функции»: весь
    # раздел «функции» базового учебника — это учебник `def` и `return`.
    "/book/prime/functions/",
    # «перебор значений словаря», «ключи и значения».
    "key-value-getting",
    "all-values-getting",
    "keys-getting",
    "dict-items",
    "dict-keys",
    "dict-values",
    "/manual/dict/items/",
    "/manual/dict/values/",
    "/manual/dict/keys/",
    # «синтаксис цикла for in», «повторить действие несколько раз» — весь
    # раздел циклов.
    "/book/prime/loops/",
    # «убрать пробелы по краям строки».
    "/manual/string/strip/",
    "/manual/string/lstrip/",
    "/manual/string/rstrip/",
    # «создание множества»: страницы про то, как множество устроено и как его
    # создать. Операции над множествами остаются — они про другое.
    "/sets/intro/",
    "converting-to-set",
    "function-set-creating",
    # Справочник типов описывает типы целиком — это близко к настоящим
    # документам «Типы данных», «Словари», «Строки» и легко отвечает на
    # вопросы про них.
    "/manual/types/",
)

#: Темы, ради которых всё затевается. Порядок — приоритет: страницы из этих
#: разделов берутся первыми (по кругу, см. `interleave_by_topic`), чтобы
#: корпус не оказался целиком про регулярные выражения.
TOPIC_PRIORITY = (
    "comprehensions",  # включения и генераторы
    "slices",  # срезы
    "exceptions",  # исключения
    "environment",  # виртуальные окружения
    "modules",  # модули и импорт
    "oop",  # классы
    "regular",  # регулярные выражения
    "fs",  # файлы
    "time",  # даты и время
    "set",  # множества
    "tuples",  # кортежи
    "iter",  # iterators: map/zip/sorted
    "database",  # базы данных
    "conditions",  # условия
    "basis",  # основы синтаксиса
    "list",  # списки
    "dict",  # словари
    "string",  # строки
    "inbuilt",  # встроенные функции
    "multi",  # вложенные коллекции
    "math",
    "random",
)

#: Человеческое имя рубрики. В живом корпусе рубрика — это тема документа
#: («Типы данных», «Основы синтаксиса»); отвлечения подписываем так же, иначе
#: они выглядят чужеродно для карточки поиска.
TOPIC_TITLES = {
    "comprehensions": "Генераторы и включения",
    "slices": "Срезы",
    "exceptions": "Исключения",
    "environment": "Виртуальные окружения",
    "modules": "Модули и пакеты",
    "oop": "Классы и объекты",
    "regular": "Регулярные выражения",
    "fs": "Работа с файлами",
    "time": "Даты и время",
    "set": "Операции над множествами",
    "tuples": "Кортежи",
    "iter": "Итераторы и встроенные функции",
    "database": "Базы данных",
    "conditions": "Условия",
    "basis": "Основы синтаксиса",
    "list": "Списки",
    "dict": "Словари",
    "string": "Работа со строками",
    "inbuilt": "Встроенные функции",
    "multi": "Вложенные коллекции",
    "math": "Математика",
    "random": "Случайные числа",
    "wikipedia": "Теория программирования",
}

#: Темы, которых в учебнике нет: генераторы, асинхронность, тестирование.
#: Формат: (что ищем, поисковый запрос, запасные заголовки статей).
#:
#: Запасной список нужен потому, что на запросы про асинхронность и генераторы
#: поиск Википедии первой отдаёт общую статью «Python», а она уже взята для
#: темы декораторов. Тогда тема спрашивается по именам — «Асинхронный
#: ввод-вывод», «Сопрограмма»: это статьи по существу, а не то, что оказалось
#: вторым в выдаче.
WIKI_TOPICS = (
    ("декораторы", "декоратор Python", ("Декоратор (шаблон проектирования)",)),
    (
        "генераторы",
        "генератор программирование Python",
        ("Генератор (программирование)", "Итератор", "Ленивые вычисления"),
    ),
    ("asyncio", "асинхронное программирование Python", ("Асинхронный ввод-вывод", "Сопрограмма", "Asyncio")),
    ("тестирование", "модульное тестирование", ("Модульное тестирование",)),
    ("исключения", "обработка исключений программирование", ("Обработка исключений",)),
    ("окружения", "виртуальное окружение Python", ("Виртуальная среда Python", "Pip (менеджер пакетов)")),
    ("классы", "абстрактный класс", ("Абстрактный класс",)),
    ("регулярные выражения", "регулярное выражение", ("Регулярные выражения",)),
)

WIKI_API = "https://ru.wikipedia.org/w/api.php"

#: Пауза между запросами к Википедии. Её ограничение на частоту жёстче, чем у
#: учебника: на первом прогоне половина тем получила `429 Too Many Requests`, и
#: корпус остался без генераторов, asyncio и тестирования.
WIKI_DELAY = 1.5

#: Заголовки разделов Википедии, с которых начинается «мусорная» часть статьи
#: (ссылки, литература, примечания). Дальше текст не берём: это не материал.
WIKI_TAIL_HEADINGS = (
    "См. также",
    "Примечания",
    "Литература",
    "Ссылки",
    "Источники",
    "Комментарии",
)


# --- Мелкие утилиты --------------------------------------------------------


def log(message: str) -> None:
    """Сообщение о ходе работы. Печатаем сразу: сбор идёт минутами."""
    print(message, flush=True)


def fail(message: str) -> None:
    """Ошибка, после которой продолжать нельзя."""
    print(f"ОШИБКА: {message}", file=sys.stderr, flush=True)
    raise SystemExit(2)


def ssl_context():
    """Контекст для HTTPS.

    Штатный питон на macOS не имеет своего хранилища корневых сертификатов, и
    обычный `urlopen` падает на проверке сертификата. `certifi` уже стоит в
    зависимостях проекта — берём корни оттуда, а не отключаем проверку.
    """
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 — certifi может отсутствовать в чужом окружении
        return ssl.create_default_context()


#: Контекст считается один раз: создание на каждый запрос — лишняя работа.
_SSL = ssl_context()


def http_get(url: str, *, timeout: int = 25, retries: int = 2) -> str:
    """Скачать страницу. Сеть — не повод падать: одна страница из сотен."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ru"})
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=_SSL) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, "replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"не удалось скачать {url}: {last}")


def http_get_json(url: str, params: dict) -> dict:
    """Запрос к API и разбор JSON."""
    query = urllib.parse.urlencode(params)
    return json.loads(http_get(f"{url}?{query}"))


def tidy_text(text: str) -> str:
    """Привести текст к виду, пригодному для нарезки.

    Нарезчик режет по пустым строкам (абзацам). Если оставить вёрстку как
    есть, абзацем станет каждая строка HTML, и куски получатся рваными.
    Поэтому схлопываем пробелы внутри строки и оставляем пустую строку только
    между смысловыми блоками.
    """
    lines = [" ".join(line.split()) for line in (text or "").replace("\r", "").split("\n")]
    out: list[str] = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out).strip()


def word_ngrams(text: str, size: int) -> set[str]:
    """Множество дословных n-грамм по словам.

    Служит для проверки утечки: если отвлечение делит с настоящим куском
    длинную дословную последовательность слов, оно не «похоже», а **то же
    самое** — и ответ в нём есть.
    """
    if size <= 0:
        return set()
    words = re.findall(r"[0-9a-zA-Zа-яА-ЯёЁ]+", (text or "").lower())
    if len(words) < size:
        return set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


# --- Разбор HTML -----------------------------------------------------------


class MainTextParser(HTMLParser):
    """Текст урока из `<main>`.

    Почему руками, а не BeautifulSoup: разбор нужен один, правила простые, а
    новая зависимость в проекте ради него — лишняя. Что делаем:

    * до `<main>` ничего не берём — там меню, хлебные крошки и навигация по
      курсу, они есть на каждой странице и в корпусе превратились бы в
      одинаковые куски-дубликаты;
    * внутри пропускаем `<script>`, `<style>`, `<nav>`, формы и иконки;
    * блочные теги превращаем в перевод строки, чтобы абзацы остались абзацами.

    Если `<main>` на странице нет (вёрстка поменялась), парсер работает по
    всему документу, отбрасывая шапку, подвал и навигацию.
    """

    SKIP_TAGS = {"script", "style", "nav", "noscript", "svg", "form", "button", "select", "iframe"}
    BLOCK_TAGS = {
        "p",
        "div",
        "li",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "br",
        "tr",
        "table",
        "pre",
        "section",
        "article",
        "blockquote",
        "hr",
        "dt",
        "dd",
        "header",
        "footer",
        "aside",
    }

    def __init__(self, *, require_main: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self.require_main = require_main
        # Если `<main>` на странице нет, берём весь документ: пропуск целой
        # страницы хуже, чем лишний текст из шапки.
        self.allow_all = not require_main
        self.saw_main = not require_main
        self.main_depth = 0
        self.skip_depth = 0
        self.h1_depth = 0
        self.title_parts: list[str] = []
        self.parts: list[str] = []

    # Глубина `<main>` считается, а не флагуется: вложенный `<main>` в
    # валидном HTML невозможен, но счётчик дешевле и не врёт на битой вёрстке.
    def handle_starttag(self, tag, attrs):  # noqa: D102 — интерфейс stdlib
        if tag == "main":
            self.main_depth += 1
            self.saw_main = True
            return
        if not self._inside():
            return
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if tag == "h1" and not self.title_parts:
            self.h1_depth += 1
        if tag in self.BLOCK_TAGS:
            self._block_break()

    def handle_startendtag(self, tag, attrs):  # noqa: D102
        if self._inside() and not self.skip_depth and tag in self.BLOCK_TAGS:
            self._block_break()

    def handle_endtag(self, tag):  # noqa: D102
        if tag == "main":
            self.main_depth = max(0, self.main_depth - 1)
            return
        if not self._inside():
            return
        if tag in self.SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag == "h1" and self.h1_depth:
            self.h1_depth -= 1
        if tag in self.BLOCK_TAGS:
            self._block_break()

    def handle_data(self, data):  # noqa: D102
        if not self._inside() or self.skip_depth:
            return
        if self.h1_depth:
            self.title_parts.append(data)
        self.parts.append(data)

    def _inside(self) -> bool:
        return self.allow_all or self.main_depth > 0

    def _block_break(self) -> None:
        """Граница блока — пустая строка, а не одиночный перевод строки.

        Нарезчик режет текст по пустым строкам: абзац, заголовок и пример кода
        должны остаться целыми. В HTML внутри абзаца строка переносится
        одиночным переводом, и если считать границей его, урок распадётся на
        сотню «абзацев» по строке, а куски нарежутся по предложениям — не так,
        как нарезаются настоящие документы.
        """
        self.parts.append("\n\n")

    def title(self) -> str:
        """Заголовок урока. Пустая строка, если `<h1>` не нашёлся."""
        return " ".join("".join(self.title_parts).split())

    def text(self) -> str:
        """Текст урока, готовый к нарезке."""
        return tidy_text("".join(self.parts))


def html_to_source(html: str) -> tuple[str, str]:
    """Вернуть (заголовок, текст) страницы учебника."""
    require_main = "<main" in html.lower()
    parser = MainTextParser(require_main=require_main)
    parser.feed(html)
    parser.close()
    return parser.title(), parser.text()


# --- Источники: учебник ----------------------------------------------------


@dataclass
class Source:
    """Страница-источник: то, из чего получатся отвлечённые куски."""

    url: str
    title: str
    topic: str
    text: str
    origin: str = "code.mu"

    @property
    def rubric(self) -> str:
        return TOPIC_TITLES.get(self.topic, "Python")


def topic_of(url: str) -> str:
    """Тема урока — из адреса. Адрес устойчивее заголовка: заголовок правится."""
    path = urllib.parse.urlparse(url).path.strip("/").split("/")
    # /ru/python/book/<book>/<topic>/...
    if len(path) >= 5 and path[2] == "book":
        return path[4]
    # /ru/python/manual/<topic>/...
    if len(path) >= 4 and path[2] == "manual":
        return path[3]
    return "python"


def is_excluded(url: str, extra: tuple = ()) -> bool:
    """Отвечает ли страница на один из наших вопросов (и потому не годится).

    `extra` — то, что можно добавить флагом `--exclude-slug`: если после замера
    видно, что какая-то тема слишком уж отвечает на вопрос, её снимают и
    прогоняют замер снова, **не правя код**. Это важно: правило должно
    проверяться замером, а не переписываться руками под результат.
    """
    lowered = url.lower()
    return any(slug in lowered for slug in (*EXCLUDE_SLUGS, *extra))


def discover_code_mu(
    max_pages: int, delay: float, target_chars: int, stats: dict, extra_excludes: tuple = ()
) -> list[Source]:
    """Ссылки на уроки из оглавлений. Страницы ещё не скачаны."""
    links: dict[str, str] = {}
    for index_url in CODE_MU_INDEXES:
        try:
            html = http_get(index_url)
        except RuntimeError as exc:
            log(f"  оглавление недоступно: {exc}")
            stats["failures"] += 1
            continue
        for href in re.findall(r'href="([^"]+)"', html):
            if not href.startswith("/ru/python/"):
                continue
            if not any(href.startswith(prefix) for prefix in CODE_MU_PREFIXES):
                continue
            # Сам раздел-оглавление в список уроков не берём.
            if any(urllib.parse.urlparse(prefix).path == href for prefix in CODE_MU_PREFIXES):
                continue
            if href.count("/") < 5:
                continue
            links[urllib.parse.urljoin(index_url, href)] = index_url
        time.sleep(delay)

    urls = [url for url in sorted(links) if not is_excluded(url, extra_excludes)]
    stats["excluded_pages"] = len(links) - len(urls)
    log(f"  уроков найдено: {len(links)}, после исключений: {len(urls)}")

    # Порядок обхода. Группировать по темам нельзя: раздел про классы — это
    # 175 уроков, и при цели в несколько сотен кусков корпус оказался бы
    # целиком про классы. Идём по кругу: по одному уроку из каждой темы, потом
    # по второму и так далее.
    by_topic: dict[str, list[str]] = {}
    for url in urls:
        by_topic.setdefault(topic_of(url), []).append(url)
    for bucket in by_topic.values():
        bucket.sort()

    ordered: list[str] = []
    topics = sorted(by_topic, key=topic_rank)
    if topics:
        for depth in range(max(len(bucket) for bucket in by_topic.values())):
            for topic in topics:
                if depth < len(by_topic[topic]):
                    ordered.append(by_topic[topic][depth])

    # Скачиваем не всё: цели в несколько сотен кусков хватает с запасом.
    ordered = ordered[: max(1, max_pages)]
    sources: list[Source] = []
    fetched_chars = 0
    for position, url in enumerate(ordered, start=1):
        try:
            title, text = html_to_source(http_get(url))
        except RuntimeError as exc:
            log(f"  пропуск {url}: {exc}")
            stats["failures"] += 1
            continue
        if len(text) < 400:
            # Обрывок вёрстки вместо урока — в корпус его пускать незачем.
            stats["skipped_short_source"] += 1
            continue
        sources.append(
            Source(url=url, title=title or url.rstrip("/").rsplit("/", 1)[-1], topic=topic_of(url), text=text)
        )
        fetched_chars += len(text)
        if position % 50 == 0:
            log(f"  скачано {position} из {len(ordered)}")
        # Останавливаемся, когда текста заведомо хватает на цель. Нарезка бывает
        # расточительной (короткие огрызки кусков отсеиваются), поэтому запас
        # берём с коэффициентом: качать шестьсот страниц ради трёхсот кусков,
        # когда хватит четырёхсот, — лишняя нагрузка на чужой сайт.
        if fetched_chars >= target_chars:
            log(f"  текста набрано достаточно ({fetched_chars} знаков), останавливаюсь на {position} страницах")
            break
        time.sleep(delay)
    return sources


def topic_rank(topic: str) -> tuple[int, str]:
    """Место темы в очереди. Незнакомая тема — в конец, но не выбрасывается."""
    try:
        return (TOPIC_PRIORITY.index(topic), topic)
    except ValueError:
        return (len(TOPIC_PRIORITY), topic)


def bundle_by_topic(pages: list[Source], *, bundle_chars: int, max_pages_per_bundle: int) -> list[Source]:
    """Склеить уроки одной темы в «конспект» — документ размером с настоящий.

    Зачем склеивать, а не брать урок как документ. Нарезчик режет по 1800
    знаков: короткий урок даёт один полноценный кусок и огрызок на несколько
    сотен знаков, который потом отсеивается по минимальной длине. Половина
    скачанного текста уходила бы в мусор, а куски всё равно были бы мельче
    настоящих. Конспект на три-четыре урока даёт ровно те же 1500–1800 знаков,
    что и настоящие PDF, и меньше запросов к сайту.

    Это ещё и ближе к живому корпусу: там документы — это «Конспект. Базовые
    функции», сшитые из нескольких разделов, а не отдельные абзацы.
    """
    buckets: dict[str, list[Source]] = {}
    for page in pages:
        buckets.setdefault(page.topic, []).append(page)

    bundles: dict[str, list[Source]] = {}
    for topic, bucket in buckets.items():
        ready: list[Source] = []
        current: list[Source] = []
        size = 0

        def flush(topic: str = topic, ready: list = ready) -> None:
            """Закрыть накопленный конспект и отдать его в `ready`.

            `topic` и `ready` — **аргументы со значением по умолчанию**, а
            не свободные переменные. Так требует `flake8-bugbear` (B023):
            замыкание, читающее переменную цикла, при отложенном вызове
            увидит **последнее** её значение, а не то, что было при
            создании функции. Здесь вызов немедленный и ошибки не было, но
            привязка делает это явным и защищает от будущей правки.

            `ready` к тому же **изменяемый список** — без привязки было бы
            неочевидно, что именно в него пишут.
            """
            """Собрать накопленные уроки в один документ."""
            nonlocal current, size
            if not current:
                return
            first = current[0]
            ready.append(
                Source(
                    url=first.url,
                    # Заголовок в стиле живого корпуса («Конспект. …») и
                    # обязательно с заголовком первого урока: если тот совпадёт
                    # с именем настоящего документа, документ отсеет проверка
                    # совпадений имён.
                    title=f"Конспект: {first.title}",
                    topic=topic,
                    text="\n\n".join(page.text for page in current),
                    origin=first.origin,
                )
            )
            current = []
            size = 0

        for page in bucket:
            current.append(page)
            size += len(page.text) + 2
            if size >= bundle_chars or len(current) >= max_pages_per_bundle:
                flush()
        flush()
        bundles[topic] = ready

    # Обратно по кругу между темами: иначе корпус начнётся с сотни конспектов
    # про классы, и «несколько сотен похожих документов» превратятся в
    # «несколько сотен документов про классы».
    ordered: list[Source] = []
    topics = sorted(bundles, key=topic_rank)
    if topics:
        for depth in range(max(len(bucket) for bucket in bundles.values())):
            for topic in topics:
                if depth < len(bundles[topic]):
                    ordered.append(bundles[topic][depth])
    return ordered


# --- Источники: Википедия --------------------------------------------------


def wikipedia_sources(delay: float, stats: dict) -> list[Source]:
    """Статьи Википедии по темам, которых нет в учебнике.

    Текст берём через `prop=extracts&explaintext` — это уже готовый чистый
    текст без разметки, то есть разбирать HTML не нужно вовсе.

    Почему **один запрос на тему**, а не «поиск, потом текст». На первом
    прогоне Википедия ответила `429 Too Many Requests` на половину тем, и
    корпус остался без генераторов, asyncio и тестирования — то есть без тех
    самых тем, ради которых её и подключали. `generator=search` отдаёт статью
    и её текст одним ответом, и запросов становится вдвое меньше. Плюс пауза
    между запросами больше, чем к учебнику: у Википедии ограничение жёстче.
    """
    sources: list[Source] = []
    seen_titles: set[str] = set()

    for label, query, fallbacks in WIKI_TOPICS:
        pages = _wiki_pages(
            {
                "action": "query",
                "generator": "search",
                "gsrsearch": query,
                # Только первый результат: поиск Википедии ранжирует статьи
                # по релевантности, и брать «первую невзятую» из выдачи нельзя
                # — на запрос про asyncio в выдаче оказывается Node.js, а на
                # запрос про окружения Home Assistant. Порядок в ответе API —
                # это порядок по идентификаторам статей, а не по релевантности.
                "gsrlimit": 1,
                "prop": "extracts",
                "explaintext": 1,
                "exlimit": "max",
                "redirects": 1,
                "format": "json",
            },
            label,
            stats,
        )
        chosen = _pick_wiki_page(pages, seen_titles)

        if chosen is None:
            # Либо та же статья уже взята (общая «Python» вылезает на любой
            # запрос про Python), либо текст слишком короткий. Тогда
            # спрашиваем статьи по именам — по одному запросу на имя, чтобы
            # порядок запасного списка соблюдался, а не порядок идентификаторов.
            for title in fallbacks:
                chosen = _pick_wiki_page(
                    _wiki_pages(
                        {
                            "action": "query",
                            "prop": "extracts",
                            "explaintext": 1,
                            "redirects": 1,
                            "format": "json",
                            "titles": title,
                        },
                        label,
                        stats,
                    ),
                    seen_titles,
                )
                if chosen is not None:
                    break

        if chosen is None:
            log(f"  википедия, «{label}»: подходящей статьи не нашлось")
            continue

        title, text = chosen
        seen_titles.add(title)
        log(f"  википедия, «{label}» → «{title}» ({len(text)} знаков)")
        sources.append(
            Source(
                url=f"https://ru.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
                title=title,
                topic="wikipedia",
                text=tidy_text(text),
                origin="ru.wikipedia",
            )
        )
        time.sleep(max(delay, WIKI_DELAY))
    return sources


def _wiki_pages(params: dict, label: str, stats: dict) -> dict:
    """Один запрос к API Википедии с повторами на 429.

    Ограничение на частоту у Википедии жёсткое, и 429 приходит не всегда с
    первого раза. Повтор с нарастающей паузой дешевле, чем тема без источника;
    исчерпали повторы — считаем это сбоем и идём дальше, корпус из-за одной
    статьи падать не должен.
    """
    for attempt in range(3):
        try:
            payload = http_get_json(WIKI_API, params)
            return payload.get("query", {}).get("pages", {}) or {}
        except (RuntimeError, ValueError) as exc:
            wait = 5 * (attempt + 1)
            log(f"  википедия, «{label}»: попытка {attempt + 1} не удалась ({exc}), пауза {wait} с")
            time.sleep(wait)
    stats["failures"] += 1
    return {}


def _pick_wiki_page(pages: dict, seen_titles: set) -> tuple[str, str] | None:
    """Первая статья из выдачи, которая ещё не взята и годится по размеру.

    Статьи короче 1500 знаков не берём: из них не выйдет даже одного куска
    нормального размера, а в корпусе появится огрызок вроде страницы значений.
    """
    for value in pages.values():
        title = value.get("title") or ""
        text = trim_wikipedia_tail(value.get("extract") or "")
        if not title or title in seen_titles or len(text) < 1500:
            continue
        return title, text
    return None


def trim_wikipedia_tail(text: str) -> str:
    """Отрезать у статьи хвост со ссылками и литературой.

    Это не материал документа, а служебная часть. Если её оставить, в корпус
    попадут куски из одних названий книг и внешних ссылок — формально похожие
    на текст, а по сути мусор.
    """
    cut = len(text)
    for heading in WIKI_TAIL_HEADINGS:
        found = text.find(f"\n== {heading} ==")
        if found != -1:
            cut = min(cut, found)
    return text[:cut]


# --- Сборка корпуса --------------------------------------------------------


@dataclass
class BuiltCorpus:
    """Отвлечённые куски, готовые к записи в индекс."""

    payloads: list[dict] = field(default_factory=list)
    documents: int = 0
    dropped_short: int = 0
    dropped_leak: int = 0
    dropped_title_collision: int = 0
    #: Заголовки выброшенных документов — в отчёте видно, какие именно имена
    #: совпали, чтобы правило можно было проверить глазами, а не на веру.
    collision_titles: list = field(default_factory=list)
    dropped_cap: int = 0
    origins: dict = field(default_factory=dict)


def load_expected_names(questions: list) -> list[str]:
    """Имена настоящих документов, которые замер считает правильным ответом.

    Берём их из самих вопросов (`tools/measure_retrieve.py`) — это
    единственный источник правды, дублировать список нельзя: разойдётся.
    """
    return sorted({expected for _, expected, _ in questions}, key=len, reverse=True)


def build_corpus(sources: list[Source], args, real_ngrams: set[str], expected_names: list[str]) -> BuiltCorpus:
    """Нарезать страницы на куски и отсеять всё, что делает замер нечестным."""
    built = BuiltCorpus()
    document_id = DISTRACTOR_ID_BASE

    for source in sources:
        if len(built.payloads) >= args.target_chunks:
            break

        # Заголовок отвлечённого документа не должен содержать имени
        # настоящего: замер ищет правильный ответ как подстроку в заголовке, и
        # чужой кусок с подходящим именем был бы зачтён за правильный. Это
        # ровно тот случай, когда «нашлось» означало бы ошибку корпуса, а не
        # успех поиска.
        title_key = source.title.lower()
        if any(name.lower() in title_key for name in expected_names):
            built.dropped_title_collision += 1
            built.collision_titles.append(source.title)
            continue

        chunks = split_into_chunks(source.text)
        kept: list[dict] = []
        for chunk in chunks:
            if len(chunk.text) < args.min_chunk_chars:
                built.dropped_short += 1
                continue
            if real_ngrams and (word_ngrams(chunk.text, args.leak_ngram) & real_ngrams):
                built.dropped_leak += 1
                continue
            kept.append({"text": chunk.text})

        if not kept:
            continue
        if len(kept) > args.max_chunks_per_source:
            # Длинная статья иначе займёт заметную долю корпуса, и он
            # перестанет быть «несколько сотен похожих документов».
            built.dropped_cap += len(kept) - args.max_chunks_per_source
            kept = kept[: args.max_chunks_per_source]

        version = hashlib.sha256(source.text.encode("utf-8")).hexdigest()[:12]
        total = len(kept)
        for index, chunk in enumerate(kept):
            built.payloads.append(
                {
                    "_id": f"{document_id}:{index}",
                    "document_id": document_id,
                    "chunk_index": index,
                    "chunk_total": total,
                    "document_version": version,
                    "title": source.title,
                    "text": chunk["text"],
                    "rubrics": [source.rubric],
                    "is_public": True,
                    "user_id": REAL_USER_ID,
                }
            )
        document_id += 1
        built.documents += 1
        built.origins[source.origin] = built.origins.get(source.origin, 0) + 1

    return built


# --- Elasticsearch ---------------------------------------------------------


class SshTunnel:
    """Туннель до Elasticsearch через хост.

    Почему он вообще нужен: Elasticsearch живёт в docker-сети `infra` и наружу
    порт не отдаёт. С локальной машины его не видно; с хоста — видно, потому
    что хост держит интерфейс этой сети. Поэтому поднимаем ssh-перенаправление
    `локальный порт -> контейнер в infra` и работаем как с обычным ES.

    Внутри контейнера `diploma-web` туннель не нужен: там ES виден напрямую.
    Скрипт сначала пробует прямой адрес и поднимает туннель только если не
    получилось.
    """

    def __init__(self, ssh_host: str, ssh_port: int, remote: str, local_port: int, known_hosts: str | None) -> None:
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.remote = remote
        self.local_port = local_port
        self.known_hosts = known_hosts
        self.process: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"

    def __enter__(self) -> SshTunnel:
        command = [
            "ssh",
            "-N",
            "-p",
            str(self.ssh_port),
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
        ]
        if self.known_hosts and Path(self.known_hosts).exists():
            command += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        else:
            # Своего файла известных хостов у скрипта нет; не спрашиваем про
            # отпечаток, но и не отключаем проверку ключа хоста целиком.
            command += ["-o", "StrictHostKeyChecking=accept-new"]
        command += ["-L", f"{self.local_port}:{self.remote}", self.ssh_host]
        log(f"  поднимаю ssh-туннель 127.0.0.1:{self.local_port} -> {self.remote} через {self.ssh_host}")
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if not wait_for_http(self.url, attempts=20, delay=0.5):
            self.__exit__(None, None, None)
            fail("ssh-туннель не поднялся: Elasticsearch не отвечает")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None


def wait_for_http(url: str, *, attempts: int = 3, delay: float = 0.5, timeout: float = 5.0) -> bool:
    """Отвечает ли адрес. Нужен, чтобы дождаться туннеля и понять, нужен ли он."""
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/", timeout=timeout, context=_SSL) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if attempt < attempts - 1:
            time.sleep(delay)
    return False


def free_port(preferred: int) -> int:
    """Свободный локальный порт. Сначала пробуем привычный."""
    for candidate in (preferred, 0):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
                return probe.getsockname()[1]
            except OSError:
                continue
    fail("не нашлось свободного локального порта")


def connect_es(args) -> tuple[Elasticsearch, object]:
    """Подключиться к Elasticsearch. Возвращает (клиент, менеджер контекста).

    Менеджер контекста нужен, чтобы туннель жил до конца работы и закрылся.
    """
    es_url = args.es_url or urls_from_settings()
    if wait_for_http(es_url, attempts=1):
        log(f"Elasticsearch доступен напрямую: {es_url}")
        return make_client(es_url), contextlib.nullcontext()

    if args.es_url:
        # Адрес задан явно — туннель к нему не приделать, и подменять его
        # молча нельзя: пользователь ждёт именно этот адрес.
        fail(f"Elasticsearch по адресу {es_url} не отвечает")

    if not args.tunnel:
        fail(f"Elasticsearch недоступен ({es_url}), а туннель выключен (--no-tunnel)")

    remote_host, _, remote_port = args.es_docker.partition(":")
    tunnel = SshTunnel(
        ssh_host=args.ssh_host,
        ssh_port=args.ssh_port,
        remote=f"{remote_host}:{remote_port or 9200}",
        local_port=free_port(args.tunnel_port),
        known_hosts=args.known_hosts,
    )
    tunnel.__enter__()
    return make_client(tunnel.url), tunnel


def urls_from_settings() -> str:
    """Адрес Elasticsearch из настроек проекта — тот же, что у приложения."""
    config = getattr(settings, "ELASTICSEARCH_DSL", {}).get("default", {})
    hosts = config.get("hosts")
    if isinstance(hosts, str):
        return hosts
    if hosts:
        return str(hosts[0])
    return "http://localhost:9200"


def make_client(es_url: str) -> Elasticsearch:
    """Клиент Elasticsearch и заодно — соединение по умолчанию для DSL.

    `views_retrieve._build_search` строит `Search(index=...)` без указания
    соединения: он берёт «default» из реестра. Замер обязан мерить **его же
    кодом**, а не копией запроса (иначе копия разойдётся с оригиналом, и замер
    начнёт врать), поэтому подменяем соединение по умолчанию, а не переписываем
    запрос.
    """
    client = Elasticsearch(hosts=[es_url], timeout=60, max_retries=2, retry_on_timeout=True)
    connections.add_connection("default", client)
    return client


def guard_index_name(name: str) -> None:
    """Запретить писать в живой индекс.

    Ошибка в имени индекса стоила бы прода: замер затёр бы куски настоящих
    документов. Пусть лучше скрипт откажется работать.
    """
    if name == LIVE_INDEX:
        fail(f"имя индекса {name!r} — это живой индекс. Замер пишет только в {DEFAULT_EXPERIMENT_INDEX!r}")
    if not name.endswith("_experiment"):
        fail(f"имя индекса {name!r} не выглядит экспериментальным (ожидается суффикс _experiment)")


def drop_index(client: Elasticsearch, index: str) -> None:
    """Удалить индекс замера. Живого это не касается: имя проверено заранее."""
    if client.indices.exists(index=index):
        log(f"  индекс {index} уже есть — удаляю и собираю заново")
        client.indices.delete(index=index)


def create_index(client: Elasticsearch, index: str, *, with_embedding: bool, dims: int, vector_field: str) -> str:
    """Создать индекс с тем же маппингом и анализатором, что у живого.

    Почему копируем маппинг у живого индекса, а не описываем заново:
    анализатор `multilingual_analyzer` (русский стеммер + стоп-слова) — это
    половина качества полнотекста. Разойдись он с живым — и замер мерил бы
    другой поиск, а не тот, что в проде.
    """
    try:
        live = client.indices.get_mapping(index=LIVE_INDEX)
        properties = dict(next(iter(live.values()))["mappings"].get("properties", {}))
        source = f"маппинг скопирован у {LIVE_INDEX}"
    except Exception as exc:  # noqa: BLE001 — живой индекс может быть недоступен
        properties = {}
        source = f"живой маппинг недоступен ({type(exc).__name__}), беру поля из кода"

    # Поля куска: даже если живой индекс недоступен, индекс замера обязан
    # уметь всё, что умеет живой.
    properties.setdefault("document_id", {"type": "integer"})
    properties.setdefault("chunk_index", {"type": "integer"})
    properties.setdefault("chunk_total", {"type": "integer"})
    properties.setdefault("document_version", {"type": "keyword"})
    properties.setdefault("title", {"type": "text", "analyzer": "multilingual_analyzer"})
    properties.setdefault("text", {"type": "text", "analyzer": "multilingual_analyzer"})
    properties.setdefault("rubrics", {"type": "text", "analyzer": "standard"})
    properties.setdefault("is_public", {"type": "boolean"})
    properties.setdefault("user_id", {"type": "integer"})

    if with_embedding:
        # Вектор куска — в то же поле, что у живого индекса. Имя поля не
        # выдумываем: замер (`tools/compare_variants.py`, `VECTOR_FIELD`) ищет
        # строго по нему, и вектор, записанный в другое поле, для knn не
        # существует.
        properties[vector_field] = {"type": "dense_vector", "dims": dims, "index": True, "similarity": "cosine"}
    else:
        properties.pop(vector_field, None)

    body = {
        "settings": {
            "number_of_shards": 1,
            # Одна реплика на однонодовом кластере не размещается и держит
            # здоровье индекса жёлтым. Так же, как в проде.
            "number_of_replicas": 0,
            "analysis": analysis_settings(),
        },
        "mappings": {"properties": properties},
    }
    # `body` передаём позиционно: клиент 7.x ругается предупреждением на
    # `body=` как на устаревший аргумент, хотя других способов у него нет.
    client.indices.create(index, body)
    log(f"  индекс {index} создан ({source}), поле вектора: {vector_field if with_embedding else 'нет'}")
    return vector_field


def analysis_settings() -> dict:
    """Русский анализатор — как в живом индексе.

    Значение берём из кода (`documents.documents.MULTILINGUAL_ANALYSIS`): это
    тот же словарь, из которого живой индекс и создавался. Дублировать его
    здесь нельзя — разойдётся при первой же правке.
    """
    from documents.documents import MULTILINGUAL_ANALYSIS

    return MULTILINGUAL_ANALYSIS


def write_payloads(client: Elasticsearch, index: str, payloads: list[dict]) -> int:
    """Записать куски одним проходом."""
    if not payloads:
        return 0
    actions = [{**payload, "_index": index, "_op_type": "index"} for payload in payloads]
    written, errors = bulk(client, actions, chunk_size=200, request_timeout=120, raise_on_error=False)
    if errors:
        log(f"  ошибок записи: {len(errors)}; первая: {errors[0]}")
    client.indices.refresh(index=index)
    return written


def live_vector_field(client: Elasticsearch) -> str:
    """Имя поля вектора в живом индексе.

    Оно не выдумано здесь, а прочитано у живого маппинга: если поле
    переименуют, скрипт подхватит новое имя и всё равно запишет векторы туда,
    куда смотрит замер. Значение по умолчанию — то, что стоит в
    `documents/documents.py` (`ChunkIndex.dense_vector`).
    """
    try:
        live = client.indices.get_mapping(index=LIVE_INDEX)
        properties = next(iter(live.values()))["mappings"].get("properties", {})
    except Exception:  # noqa: BLE001 — без живого индекса работаем по значению из кода
        return DEFAULT_VECTOR_FIELD
    if DEFAULT_VECTOR_FIELD in properties:
        return DEFAULT_VECTOR_FIELD
    # Запасной вариант: прежнее имя поля из ранних набросков замера.
    if "embedding" in properties:
        return "embedding"
    return DEFAULT_VECTOR_FIELD


def copy_real_chunks(client: Elasticsearch, index: str, *, with_embedding: bool, vector_field: str) -> list[dict]:
    """Копия настоящих кусков для объединённого корпуса.

    Читаем живой индекс и пишем копию в индекс замера. Живой не меняется — это
    важно: замер не имеет права трогать прод.
    """
    response = client.search(index=LIVE_INDEX, query={"match_all": {}}, size=1000, request_timeout=60)
    payloads = []
    for hit in response["hits"]["hits"]:
        source = dict(hit["_source"])
        if not with_embedding:
            source.pop(vector_field, None)
        payloads.append({**source, "_id": hit["_id"]})
    log(f"  настоящих кусков скопировано: {len(payloads)}")
    return payloads


def load_real_chunks(client: Elasticsearch) -> list[dict]:
    """Настоящие куски: нужны для проверки утечки и для проверки поиска."""
    response = client.search(index=LIVE_INDEX, query={"match_all": {}}, size=1000, request_timeout=60)
    return [hit["_source"] for hit in response["hits"]["hits"]]


# --- Векторы ---------------------------------------------------------------


def embed_payloads(payloads: list[dict], args, vector_field: str) -> int:
    """Посчитать векторы для кусков.

    Через сервис проекта, а не своим HTTP-запросом: у модели есть роли
    (`search_document:` / `search_query:`), и куски обязаны векторизоваться с
    той же ролью, что в проде. Свой запрос рано или поздно разойдётся с
    сервисом, и knn-вариант провалится по нашей вине.

    Ошибку не глотаем: наполовину заполненный векторный поиск — это дырка,
    которую потом не видно. Лучше остановиться и попросить `--no-embed`.
    """
    from documents.services.embedding_service import embed_texts

    if args.ollama_url:
        # В .env адрес модели может быть не задан (локальная разработка), а
        # замеру нужна настоящая модель — подставляем адрес из аргументов.
        settings.OLLAMA_URL = args.ollama_url

    vectors = embed_texts([payload["text"] for payload in payloads])
    for payload, vector in zip(payloads, vectors):
        payload[vector_field] = vector
    return len(vectors)


def embedding_dims() -> int:
    """Размерность вектора из сервиса эмбеддингов."""
    try:
        from documents.services.embedding_service import dimension

        return dimension()
    except Exception:  # noqa: BLE001 — сервис может быть ещё не готов
        return FALLBACK_DIMS


# --- Проверка --------------------------------------------------------------


def load_questions() -> list:
    """Вопросы замера — из `tools/measure_retrieve.py`, без копии.

    Копия списка разошлась бы с оригиналом, и замер доказывал бы не то, что
    происходит на самом деле. Файл подгружаем по пути: `tools/` — не пакет.
    """
    path = REPO_ROOT / "tools" / "measure_retrieve.py"
    spec = importlib.util.spec_from_file_location("measure_retrieve", path)
    module = importlib.util.module_from_spec(spec)
    # Регистрируем модуль до выполнения: dataclass и другие механизмы ищут
    # определение класса в `sys.modules`, и без записи импорт падает.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return list(module.QUESTIONS)


@contextlib.contextmanager
def use_index(index: str):
    """Направить запросы вью на нужный индекс.

    Имя индекса вью держит в модульной константе и подставляет в `Search(...)`.
    Подменяем её на время вызова — так замер идёт **тем же кодом**, что прод.
    """
    previous = views_retrieve.CHUNKS_INDEX
    views_retrieve.CHUNKS_INDEX = index
    try:
        yield
    finally:
        views_retrieve.CHUNKS_INDEX = previous


def run_questions(index: str, *, distractor_only: bool) -> list[dict]:
    """Прогнать 15 вопросов по индексу. Ответ — строка на вопрос."""
    view = views_retrieve.RetrieveView()
    # Служебный токен видит только публичные документы, и в базе для этого
    # ничего не нужно: замер обязан работать без Postgres.
    token = types.SimpleNamespace(scope=ApiToken.SCOPE_SERVICE, user_id=None)
    rows = []

    with use_index(index):
        for query, expected, kind in load_questions():
            try:
                search = view._build_search(token, query, None, 20)
                if distractor_only:
                    # Только отвлечения: настоящие куски в объединённом индексе
                    # лежат с маленькими номерами, отвлечённые — с большими.
                    search = search.filter("range", document_id={"gte": DISTRACTOR_ID_BASE})
                hits = view._collect(search.execute(), 20)
            except Exception as exc:  # noqa: BLE001 — индекс может быть не создан
                rows.append(
                    {
                        "query": query,
                        "kind": kind,
                        "expected": expected,
                        "position": 0,
                        "found_total": -1,
                        "error": f"{type(exc).__name__}: {exc}",
                        "top": [],
                        "top_score": 0.0,
                    }
                )
                continue

            titles = [hit.get("title") or "" for hit in hits]
            position = 0
            for at, title in enumerate(titles, start=1):
                # Та же проверка, что в `measure_retrieve.py`: правильный
                # документ ищется по имени. Менять её нельзя — иначе результаты
                # замера нельзя будет сравнить с базовыми.
                if expected.lower() in str(title).lower():
                    position = at
                    break
            rows.append(
                {
                    "query": query,
                    "kind": kind,
                    "expected": expected,
                    "position": position,
                    "found_total": len(hits),
                    "error": None,
                    "top": titles[:3],
                    "top_score": round(float(hits[0]["score"]), 3) if hits else 0.0,
                }
            )
    return rows


def score(rows: list[dict]) -> dict:
    """Свести попадания в две строки: дословные и перефразированные."""
    result = {}
    for kind in ("literal", "paraphrase"):
        subset = [row for row in rows if row["kind"] == kind]
        result[kind] = sum(1 for row in subset if 0 < row["position"] <= 3)
        result[kind + "_total"] = len(subset)
    result["total"] = result["literal"] + result["paraphrase"]
    result["total_all"] = result["literal_total"] + result["paraphrase_total"]
    return result


def print_rows(rows: list[dict], *, show_top: bool = True) -> None:
    """Таблица вопросов: что нашлось и где."""
    print(f"  {'вопрос':<40} {'тип':<10} {'ждём':<22} {'место':>5} {'найдено':>7}")
    print("  " + "─" * 96)
    for row in rows:
        mark = str(row["position"]) if row["position"] else "—"
        if row["error"]:
            mark = "ошибка"
        print(
            f"  {row['query'][:38]:<40} {row['kind']:<10} {row['expected'][:20]:<22} {mark:>5} {row['found_total']:>7}"
        )
        if show_top and row["top"]:
            print(f"      топ: {' | '.join(title[:46] for title in row['top'])}")


# --- Аргументы -------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Собрать корпус с отвлекающими кусками и проверить поиск на масштабе",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--index", default=DEFAULT_EXPERIMENT_INDEX, help="индекс замера")
    parser.add_argument("--target-chunks", type=int, default=300, help="сколько отвлечённых кусков хотим")
    parser.add_argument("--max-pages", type=int, default=480, help="сколько страниц учебника скачать не больше")
    parser.add_argument(
        "--bundle-chars", type=int, default=9000, help="сколько знаков склеивать в один отвлечённый документ"
    )
    parser.add_argument(
        "--max-pages-per-bundle", type=int, default=8, help="уроков в одном отвлечённом документе не больше"
    )
    parser.add_argument("--max-chunks-per-source", type=int, default=8, help="кусков с одной страницы не больше")
    parser.add_argument("--min-chunk-chars", type=int, default=900, help="куски короче этого не берём")
    parser.add_argument(
        "--leak-ngram", type=int, default=12, help="длина дословной n-граммы для проверки утечки (0 — выключить)"
    )
    parser.add_argument("--delay", type=float, default=0.4, help="пауза между запросами к сайту, секунды")
    parser.add_argument(
        "--exclude-slug",
        action="append",
        default=[],
        metavar="ПОДСТРОКА",
        help="дополнительно исключить уроки, в адресе которых есть подстрока (можно повторять)",
    )
    parser.add_argument(
        "--wikipedia", action=argparse.BooleanOptionalAction, default=True, help="брать статьи Википедии"
    )
    parser.add_argument(
        "--real", action=argparse.BooleanOptionalAction, default=True, help="добавить в индекс настоящие куски"
    )
    parser.add_argument("--embed", action=argparse.BooleanOptionalAction, default=True, help="считать векторы")
    parser.add_argument(
        "--write-index",
        dest="write_index",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="писать в Elasticsearch",
    )
    parser.add_argument("--verify", action=argparse.BooleanOptionalAction, default=True, help="прогонять 15 вопросов")
    parser.add_argument("--preview", type=int, default=0, help="показать первые N собранных кусков")
    parser.add_argument("--ollama-url", default="http://192.168.0.78:11434", help="адрес Ollama для эмбеддингов")
    parser.add_argument(
        "--es-url", default="", help="адрес Elasticsearch (по умолчанию — из настроек, иначе ssh-туннель)"
    )
    parser.add_argument(
        "--tunnel", action=argparse.BooleanOptionalAction, default=True, help="поднимать ssh-туннель, если ES не виден"
    )
    parser.add_argument("--tunnel-port", type=int, default=19200, help="локальный порт туннеля")
    parser.add_argument("--ssh-host", default="alserloz@95.54.44.225", help="хост с docker-сетью infra")
    parser.add_argument("--ssh-port", type=int, default=2222, help="порт ssh")
    parser.add_argument("--es-docker", default="172.18.0.14:9200", help="адрес Elasticsearch внутри сети infra")
    parser.add_argument(
        "--known-hosts",
        default="/Users/asl/Documents/python_project/lapot/.tmp-ssh/known_hosts",
        help="файл известных хостов ssh",
    )
    return parser.parse_args(argv)


# --- main ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    guard_index_name(args.index)

    log("1. Источники")
    stats = {"failures": 0, "excluded_pages": 0, "skipped_short_source": 0}
    wiki = wikipedia_sources(args.delay, stats) if args.wikipedia else []
    log(f"  википедия: статей взято {len(wiki)}")

    # Википедия идёт **первой**. Это единственный источник тем, которых нет в
    # учебнике, — генераторы, асинхронность, тестирование. Если поставить её в
    # конец списка, сборка остановится на цели раньше, чем до неё дойдёт, и
    # корпус останется ровно без тех тем, ради которых она подключалась: на
    # одном из прогонов так и вышло — 302 куска, а слов «генератор» и «asyncio»
    # в корпусе ноль.
    #
    # Её будущие куски вычитаем из бюджета скачивания: иначе учебник наберёт
    # полную цель, и Википедия снова окажется за бортом.
    wiki_chunks = sum(min(args.max_chunks_per_source, max(1, len(source.text) // CHARS_PER_CHUNK)) for source in wiki)
    pages = discover_code_mu(
        args.max_pages,
        args.delay,
        max(1, args.target_chunks - wiki_chunks) * CHARS_PER_CHUNK,
        stats,
        tuple(args.exclude_slug),
    )
    log(f"  уроков скачано: {len(pages)}")
    sources = wiki + bundle_by_topic(
        pages,
        bundle_chars=args.bundle_chars,
        max_pages_per_bundle=args.max_pages_per_bundle,
    )
    log(f"  отвлечённых документов всего: {len(sources)}")
    if not sources:
        fail("не удалось собрать ни одной страницы-источника (нет сети?)")

    log("2. Настоящие куски (только чтение живого индекса)")
    client, tunnel = connect_es(args)
    try:
        real_chunks = load_real_chunks(client)
        if not real_chunks:
            log(f"  ВНИМАНИЕ: в {LIVE_INDEX} нет кусков — проверить не с чем")
        real_ngrams: set[str] = set()
        for chunk in real_chunks:
            real_ngrams |= word_ngrams(chunk.get("text") or "", args.leak_ngram)
        log(f"  кусков: {len(real_chunks)}, дословных {args.leak_ngram}-грамм: {len(real_ngrams)}")

        log("3. Нарезка отвлечений")
        questions = load_questions()
        expected_names = load_expected_names(questions)
        built = build_corpus(sources, args, real_ngrams, expected_names)
        if not built.payloads:
            fail("отвлечённых кусков не получилось: проверьте --min-chunk-chars")
        sizes = [len(payload["text"]) for payload in built.payloads]
        total_chars = sum(sizes)
        inside = sum(1 for size in sizes if 1500 <= size <= 1800)
        log(
            f"  кусков: {len(built.payloads)}, знаков: {total_chars}, "
            f"средний: {round(statistics.mean(sizes))}, медиана: {round(statistics.median(sizes))}"
        )
        log(
            f"  отброшено: коротких {built.dropped_short}, утечек {built.dropped_leak}, "
            f"совпадений имён {built.dropped_title_collision}, сверх лимита {built.dropped_cap}"
        )
        for title in built.collision_titles:
            log(f"    имя совпало с настоящим документом: {title}")

        if args.preview:
            log("  предпросмотр:")
            for payload in built.payloads[: args.preview]:
                print(f"  --- {payload['title']} [{payload['rubrics'][0]}] {len(payload['text'])} знаков")
                print("  " + payload["text"][:400].replace("\n", "\n  "))

        written = 0
        real_payloads: list[dict] = []
        vector_count = 0
        if args.write_index:
            with_embedding = args.embed
            dims = embedding_dims() if with_embedding else 0
            # Имя поля вектора — из живого маппинга: замер ищет строго по нему.
            vector_field = live_vector_field(client)
            if with_embedding:
                log(f"4. Векторы (nomic-embed-text, {dims} измерений, поле {vector_field})")
                try:
                    vector_count = embed_payloads(built.payloads, args, vector_field)
                    log(f"  векторов: {vector_count}")
                except Exception as exc:  # noqa: BLE001 — причину печатаем человеку
                    fail(
                        f"эмбеддинги не получились: {exc}\n"
                        "        Повторите с --no-embed, если вектора сейчас не нужны: "
                        "тогда индекс замера будет только полнотекстовым."
                    )
            log("5. Запись в Elasticsearch")
            drop_index(client, args.index)
            create_index(client, args.index, with_embedding=with_embedding, dims=dims, vector_field=vector_field)

            if args.real:
                real_payloads = copy_real_chunks(
                    client, args.index, with_embedding=with_embedding, vector_field=vector_field
                )
                if with_embedding and real_payloads:
                    missing = [payload for payload in real_payloads if vector_field not in payload]
                    if missing:
                        # Векторов у настоящих кусков может ещё не быть (шаг 1
                        # плана). Тогда считаем их здесь же: иначе knn-вариант
                        # находил бы только настоящие куски и замер на масштабе
                        # доказывал бы ерунду.
                        log(f"  у настоящих кусков нет векторов: считаю ({len(missing)})")
                        try:
                            embed_payloads(missing, args, vector_field)
                        except Exception as exc:  # noqa: BLE001
                            fail(f"не удалось посчитать векторы настоящих кусков: {exc}")

            written = write_payloads(client, args.index, built.payloads) + write_payloads(
                client, args.index, real_payloads
            )
            count = client.count(index=args.index)["count"]
            log(f"  в индексе {args.index}: {count} кусков")

        summary = {
            "index": args.index,
            "distractor_chunks": len(built.payloads),
            "distractor_chars": total_chars,
            "avg_chars": round(statistics.mean(sizes)),
            "median_chars": round(statistics.median(sizes)),
            "min_chars": min(sizes),
            "max_chars": max(sizes),
            "share_1500_1800": round(inside / len(sizes), 3),
            "real_chunks": len(real_chunks) if args.real else 0,
            "sources_by_origin": built.origins,
            "distractor_documents": built.documents,
            # Уникальность заголовков важна не для красоты: замер сравнивает
            # варианты слияния (`tools/compare_variants.py`, RRF и взвешенный)
            # **по заголовкам**, а не по кускам. Документы с одинаковым
            # заголовком там схлопнутся в один, и отвлечения перестанут быть
            # честной конкуренцией.
            "distractor_titles": len({payload["title"] for payload in built.payloads}),
            "pages_fetched": len(pages),
            "excluded_pages": stats["excluded_pages"],
            "failed_requests": stats["failures"],
            "dropped_short": built.dropped_short,
            "dropped_leak": built.dropped_leak,
            "dropped_title_collision": built.dropped_title_collision,
            "written": written,
            "vectors": vector_count,
        }

        if args.verify and args.write_index:
            log("6. Проверка: 15 вопросов по одному корпусу отвлечений")
            distractor_rows = run_questions(args.index, distractor_only=True)
            found = [row for row in distractor_rows if row["position"] > 0]
            answered = [row for row in distractor_rows if row["found_total"] > 0]
            print_rows(distractor_rows)
            print(f"  Правильный документ нашёлся: {len(found)} из {len(distractor_rows)} (требование: 0 или почти 0)")
            print(f"  Вопросов, на которые отвлечения вообще откликнулись: {len(answered)} из {len(distractor_rows)}")
            summary["verify_distractor_only_found_expected"] = len(found)
            summary["verify_distractor_only_answered"] = len(answered)
            summary["verify_distractor_only_rows"] = [
                {
                    "query": row["query"],
                    "found_total": row["found_total"],
                    "top_score": row["top_score"],
                    "top": row["top"],
                }
                for row in distractor_rows
            ]

            if args.real:
                log("7. Проверка: те же 15 вопросов по объединённому корпусу (как сейчас, bm25_and)")
                combined_rows = run_questions(args.index, distractor_only=False)
                print_rows(combined_rows)
                combined = score(combined_rows)
                print(
                    f"  дословные {combined['literal']}/{combined['literal_total']}, "
                    f"перефразированные {combined['paraphrase']}/{combined['paraphrase_total']}"
                )
                summary["verify_combined"] = combined
        elif args.verify:
            # Записи не было: показываем базовый прогон по живому корпусу —
            # с ним потом и сравнивают объединённый.
            log("6. Записи не было: базовый прогон по живому корпусу")
            base_rows = run_questions(LIVE_INDEX, distractor_only=False)
            print_rows(base_rows)
            summary["baseline_live"] = score(base_rows)

        log("8. Сводка")
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0
    finally:
        if hasattr(tunnel, "__exit__"):
            tunnel.__exit__(None, None, None)


if __name__ == "__main__":
    sys.exit(main())
