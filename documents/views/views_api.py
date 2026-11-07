import logging

from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from elasticsearch.exceptions import TransportError
from rest_framework import permissions, status, viewsets
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from documents.constants import DocumentValidationError, normalize_rubrics, parse_positive_int, validate_uploaded_file
from documents.models import Document, SearchHistory
from documents.rate_limit import RateLimiters
from documents.serializers import DocumentCreateUpdateSerializer, DocumentSerializer
from documents.services.search_service import SearchService
from documents.utils import extract_text_from_file

logger = logging.getLogger(__name__)


class SearchView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        tags=["search"],
        description="Полнотекстовый поиск по документам с фильтрацией",
        request={
            "application/json": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос", "example": "python разработка"},
                    "rubric": {"type": "string", "description": "Фильтр по рубрике", "example": "технологии"},
                    "privacy": {
                        "type": "string",
                        "enum": ["all", "public", "private"],
                        "description": "Тип доступа",
                        "default": "all",
                    },
                    "page": {"type": "integer", "description": "Номер страницы", "default": 1},
                },
                "required": ["query"],
            }
        },
        responses={
            200: OpenApiResponse(description="Успешный поиск"),
            400: OpenApiResponse(description="Query parameter 'query' required"),
            401: OpenApiResponse(description="Не авторизован"),
            429: OpenApiResponse(description="Too many requests (30 per minute)"),
        },
    )
    def post(self, request):
        user_id = request.user.id

        # Rate limiting
        limiter = RateLimiters.api_search()
        allowed, remaining, retry_after = limiter.check(f"user_{user_id}")

        if not allowed:
            logger.warning(f"Rate limit exceeded for search: user {user_id}")
            return Response(
                {"error": f"Too many requests. Please wait {retry_after} seconds."},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"X-RateLimit-Retry-After": str(retry_after)},
            )

        # Получаем параметры запроса
        query = request.data.get("query", "").strip()
        rubric = request.data.get("rubric", "")
        privacy = request.data.get("privacy", "all")
        page = parse_positive_int(request.data.get("page", 1))

        if not query:
            return Response({"error": "Query parameter 'query' required"}, status=status.HTTP_400_BAD_REQUEST)

        # Используем сервис поиска. Отказ Elasticsearch не должен выглядеть как
        # внутренняя ошибка: для клиента это временная недоступность сервиса.
        try:
            service = SearchService(request.user)
            search_response = service.search(
                query=query,
                rubric=rubric,
                privacy=privacy,
                page=page,
                save_history=True,
                with_highlights=False,
                with_truncation=True,
            )
        except TransportError as exc:
            logger.warning("Поиск недоступен (пользователь %s): %s", user_id, exc)
            return Response(
                {"error": "Поиск временно недоступен. Попробуйте позже."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        total_pages = search_response.total_pages

        return Response(
            {
                "count": search_response.total,
                "next": page + 1 if page < total_pages else None,
                "previous": page - 1 if page > 1 else None,
                "results": [r.to_dict() for r in search_response.results],
            },
            headers={
                "X-RateLimit-Limit": str(limiter.limit),
                "X-RateLimit-Remaining": str(remaining),
            },
        )


class DocumentViewSet(viewsets.ModelViewSet):
    serializer_class = DocumentSerializer
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [JSONParser, MultiPartParser]

    @extend_schema(
        tags=["documents"],
        description="Получить список всех документов пользователя",
        responses={200: DocumentSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        tags=["documents"],
        description="Создать новый документ (текст или файл)",
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {
                    "rubrics": {
                        "type": "string",
                        "description": "Рубрики через запятую",
                        "example": "технологии, python, django",
                    },
                    "text": {"type": "string", "description": "Текст документа (если без файла)"},
                    "is_public": {"type": "boolean", "description": "Публичный доступ", "default": False},
                    "file": {
                        "type": "string",
                        "format": "binary",
                        "description": "Файл (PDF, DOCX, XLSX, TXT, MD, PY)",
                    },
                },
            }
        },
        responses={201: DocumentSerializer()},
    )
    def create(self, request, *args, **kwargs):
        # Rate limiting
        limiter = RateLimiters.api_general()
        allowed, remaining, retry_after = limiter.check(f"user_{request.user.id}_create")

        if not allowed:
            logger.warning(f"Rate limit exceeded for document create: user {request.user.id}")
            from rest_framework.exceptions import Throttled

            raise Throttled(wait=retry_after)

        # Обработка рубрик: строка через запятую или список
        try:
            rubrics = normalize_rubrics(request.data.get("rubrics", []))
        except DocumentValidationError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # Обработка is_public
        is_public = request.data.get("is_public", False)
        if isinstance(is_public, str):
            is_public = is_public.lower() == "true"

        uploaded_file = request.FILES.get("file")

        if uploaded_file:
            try:
                file_type = validate_uploaded_file(uploaded_file)
            except DocumentValidationError as exc:
                logger.warning("Загрузка отклонена (пользователь %s): %s", request.user.id, exc)
                return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

            # Текст читается из загруженного файла: документ создаётся одним
            # запросом и не зависит от пути к файлу в MEDIA_ROOT.
            document = Document.objects.create(
                user=request.user,
                rubrics=rubrics,
                text=extract_text_from_file(uploaded_file, file_type),
                is_public=is_public,
                file=uploaded_file,
                file_name=uploaded_file.name,
                file_type=file_type,
                text_source="file",
            )
        else:
            if not (request.data.get("text") or "").strip():
                return Response(
                    {"error": "Укажите либо 'text', либо загрузите 'file'"}, status=status.HTTP_400_BAD_REQUEST
                )

            create_serializer = DocumentCreateUpdateSerializer(data={**request.data, "rubrics": rubrics})
            create_serializer.is_valid(raise_exception=True)

            document = Document.objects.create(
                user=request.user,
                rubrics=create_serializer.validated_data.get("rubrics", []),
                text=create_serializer.validated_data["text"],
                is_public=create_serializer.validated_data.get("is_public", False),
                file=None,
                file_name="",
                file_type="",
                text_source="manual",
            )

        return Response(DocumentSerializer(document).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=["documents"],
        description="Получить документ по ID",
        responses={200: DocumentSerializer()},
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=["documents"],
        description="Обновить документ полностью",
        request=DocumentCreateUpdateSerializer,
        responses={200: DocumentSerializer()},
    )
    def update(self, request, *args, **kwargs):
        # Rate limiting на обновление
        limiter = RateLimiters.api_general()
        allowed, remaining, retry_after = limiter.check(f"user_{request.user.id}_update")

        if not allowed:
            from rest_framework.exceptions import Throttled

            raise Throttled(wait=retry_after)

        return super().update(request, *args, **kwargs)

    @extend_schema(
        tags=["documents"],
        description="Частично обновить документ",
        request=DocumentCreateUpdateSerializer,
        responses={200: DocumentSerializer()},
    )
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    @extend_schema(
        tags=["documents"],
        description="Удалить документ",
        responses={204: OpenApiResponse(description="Документ удалён")},
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)

    def get_queryset(self):
        # select_related: сериализатор отдаёт email и id владельца, без этого
        # список из N документов даёт N дополнительных запросов.
        return Document.objects.filter(user=self.request.user).select_related("user").order_by("-created_date")

    def get_serializer_class(self):
        if self.action in ["create", "update", "partial_update"]:
            return DocumentCreateUpdateSerializer
        return DocumentSerializer

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class RubricsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        tags=["search"],
        description="Получить список всех рубрик из доступных пользователю документов",
        responses={
            200: OpenApiResponse(description="Список рубрик", response={"type": "array", "items": {"type": "string"}})
        },
    )
    def get(self, request):
        documents = Document.objects.filter(Q(user=request.user) | Q(is_public=True)).values_list("rubrics", flat=True)

        unique_rubrics = set()
        for rubrics_list in documents:
            for rubric in rubrics_list or []:
                unique_rubrics.add(rubric)

        return Response(sorted(unique_rubrics))


class SearchHistoryDeleteView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        tags=["search"],
        description="Удалить запись из истории поиска",
        parameters=[OpenApiParameter(name="pk", type=int, location="path", description="ID записи истории")],
        responses={
            204: OpenApiResponse(description="Удалено"),
            404: OpenApiResponse(description="Не найдено"),
        },
    )
    def delete(self, request, pk):
        try:
            history = SearchHistory.objects.get(pk=pk, user=request.user)
            history.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except SearchHistory.DoesNotExist:
            return Response({"error": "History entry not found"}, status=status.HTTP_404_NOT_FOUND)
