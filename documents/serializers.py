import re

from rest_framework import serializers

from .constants import MAX_TEXT_LENGTH, DocumentValidationError, normalize_rubrics
from .models import Document, SearchHistory


class DocumentSerializer(serializers.ModelSerializer):
    """Сериализатор для документа (чтение)"""

    user_email = serializers.EmailField(source="user.email", read_only=True)
    user_id = serializers.IntegerField(source="user.id", read_only=True)

    class Meta:
        model = Document
        fields = [
            "id",
            "rubrics",
            "text",
            "created_date",
            "is_public",
            # Поля загруженного файла раньше не отдавались: клиент не мог узнать,
            # из какого файла получен документ и как его скачать.
            "file",
            "file_name",
            "file_type",
            "text_source",
            "user_email",
            "user_id",
        ]
        read_only_fields = [
            "id",
            "created_date",
            "file",
            "file_name",
            "file_type",
            "text_source",
            "user_email",
            "user_id",
        ]


class RubricField(serializers.CharField):
    """Одна рубрика — строка.

    CharField сам приводит числа к строке, и [1, 2, 3] прошло бы как
    ["1", "2", "3"]; в базе рубрики — список строк, поэтому лучше отказать.
    """

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class RubricsField(serializers.ListField):
    """Рубрики: список строк или строка через запятую.

    Поле объявлено явно, потому что rubrics в модели — JSONField: без этого в
    схеме API он описывался как «любой JSON», хотя принимает только список
    строк. Строка через запятую поддержана намеренно: так рубрики приходят из
    web-формы.
    """

    child = RubricField()
    default = list
    required = False

    def to_internal_value(self, data):
        if isinstance(data, str):
            data = [rubric.strip() for rubric in data.split(",") if rubric.strip()]
        return super().to_internal_value(data)


class DocumentCreateUpdateSerializer(serializers.ModelSerializer):
    """Сериализатор для создания и обновления документа"""

    rubrics = RubricsField()

    class Meta:
        model = Document
        fields = ["rubrics", "text", "is_public"]

    def validate_text(self, value):
        """Валидация текста: длина и удаление control characters"""
        if not value:
            return value

        # Проверка максимальной длины
        if len(value) > MAX_TEXT_LENGTH:
            raise serializers.ValidationError(f"Текст слишком длинный. Максимум {MAX_TEXT_LENGTH} символов.")

        # Базовая очистка: удаляем control characters (оставляем \n, \r, \t)
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)

        return value

    def validate_rubrics(self, value):
        """Валидация рубрик общими правилами из documents/constants.py"""
        try:
            return normalize_rubrics(value)
        except DocumentValidationError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class SearchHistorySerializer(serializers.ModelSerializer):
    """Сериализатор для истории поиска (только чтение)"""

    class Meta:
        model = SearchHistory
        fields = ["id", "query", "results_count", "created_at"]
        read_only_fields = ["id", "created_at"]


# --- Поиск отрывков для потребителей (docs/SEARCH-CONTRACT.md) -------------
#
# Форма описана сериализаторами не ради валидации — её проверяет само
# представление, — а ради схемы: потребитель должен видеть границу в
# сгенерированном описании API, а не вычитывать её из кода.


class RetrieveRequestSerializer(serializers.Serializer):
    """Запрос поиска отрывков (контракт, §2)."""

    query = serializers.CharField(
        min_length=3,
        max_length=1000,
        help_text="Текст запроса, от 3 до 1000 знаков",
    )
    limit = serializers.IntegerField(
        required=False,
        default=10,
        min_value=1,
        max_value=50,
        help_text="Сколько отрывков вернуть: по умолчанию 10, максимум 50",
    )
    rubrics = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="Сузить поиск до рубрик",
    )


class RetrieveResultSerializer(serializers.Serializer):
    """Один отрывок (контракт, §3)."""

    document_id = serializers.IntegerField()
    chunk_index = serializers.IntegerField()
    chunk_total = serializers.IntegerField()
    document_version = serializers.CharField(help_text="Отпечаток содержимого: видно, что документ изменился")
    title = serializers.CharField()
    text = serializers.CharField(help_text="Отрывок целиком, с перехлёстом")
    score = serializers.FloatField(
        allow_null=True,
        help_text=(
            "Всегда null. Раньше здесь была шкала BM25, но с гибридным поиском "
            "выдача сливается по рангам (RRF): это сумма обратных мест, а не "
            "BM25 и не косинус. Число, которое ничего не значит, хуже его "
            "отсутствия — поэтому его нет."
        ),
    )
    rubrics = serializers.ListField(child=serializers.CharField())
    is_public = serializers.BooleanField(help_text="Можно ли пересказывать это всем")


class RetrieveResponseSerializer(serializers.Serializer):
    """Ответ поиска отрывков."""

    results = RetrieveResultSerializer(many=True)
    source = serializers.ChoiceField(
        choices=["hybrid", "fulltext", "vector"],
        help_text="Чем именно нашли: потребитель должен знать, что векторный поиск деградировал",
    )
