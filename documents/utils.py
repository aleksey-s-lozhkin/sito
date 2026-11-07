"""Извлечение и очистка текста документов."""

import logging
import re

import pdfplumber
from docx import Document as DocxDocument
from openpyxl import load_workbook

logger = logging.getLogger(__name__)


def clean_extracted_text(text):
    """Очищает извлечённый текст для улучшения поиска"""
    if not text:
        return ""

    # Заменяем NUL символы на пробелы
    text = text.replace("\x00", " ")

    # Убираем лишние пробелы (более одного подряд)
    text = re.sub(r"[ \t]{2,}", " ", text)

    # Нормализуем переносы строк (3+ переносов → 2)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Убираем пробелы в начале/конце строк
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)

    return text.strip()


def _rewind(source):
    """Возвращает указатель в начало, если source — файловый объект.

    Тот же объект дальше сохраняется Django как файл документа, поэтому оставлять
    его прочитанным нельзя: записался бы пустой файл.
    """
    seek = getattr(source, "seek", None)
    if callable(seek):
        try:
            seek(0)
        except (OSError, ValueError):
            logger.warning("Не удалось вернуть указатель файла в начало", exc_info=True)


def _read_text(source):
    """Читает txt из пути или из файлового объекта."""
    if hasattr(source, "read"):
        data = source.read()
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return data
    with open(source, encoding="utf-8", errors="replace") as f:
        return f.read()


def _extract_pdf_text(source):
    """Текст из PDF через pdfplumber.

    Раньше здесь был pypdf, и он портил текст: у него нет геометрии страницы,
    поэтому слова склеивались («первогопослеперечисленныесимволы») и рвались
    («з овые ф ункции»). pdfplumber собирает слова из глифов по расстоянию между
    ними, и на десяти документах корпуса мусорных слов стало 11,0 % против
    13,1 %, а найденных python-слов 325 против 299.
    """
    if hasattr(source, "seek"):
        source.seek(0)

    parts = []
    with pdfplumber.open(source) as pdf:
        for page in pdf.pages:
            parts.append(page.extract_text() or "")
    return "\n".join(parts)


def extract_text_from_file(source, file_type):
    """Извлекает текст из файла: PDF, DOCX, XLSX, TXT, MD, PY.

    ``source`` — путь к файлу или файловый объект. Работа с объектом позволяет
    извлечь текст из загруженного файла до его сохранения: раньше документ
    сохранялся дважды — сначала пустой, потом с текстом, прочитанным обратно с
    диска по пути MEDIA_ROOT.

    Для ``md`` и ``py`` текст берётся как есть (исходники шпаргалок): они дают
    идеальный текст, и извлечение из PDF для них не нужно.
    """
    text = ""

    try:
        if file_type == "pdf":
            text = _extract_pdf_text(source)

        elif file_type == "docx":
            doc = DocxDocument(source)
            for para in doc.paragraphs:
                text += para.text + "\n"
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        text += cell.text + " "
                    text += "\n"

        elif file_type == "xlsx":
            wb = load_workbook(source, data_only=True)
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    text += " ".join([str(cell) for cell in row if cell]) + "\n"

        elif file_type in ("txt", "md", "py"):
            # Исходники шпаргалок: если файл есть в текстовом виде, он даёт
            # идеальный текст, и вся возня с PDF не нужна.
            text = _read_text(source)

        else:
            logger.warning("Извлечение текста для типа %s не поддерживается", file_type)
            return ""

    except Exception as e:
        logger.error(f"Error extracting text from {source}: {e}", exc_info=True)
        return ""

    finally:
        _rewind(source)

    return clean_extracted_text(text)
