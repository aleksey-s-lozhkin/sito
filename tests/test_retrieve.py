"""Поиск отрывков для потребителей — контракт docs/SEARCH-CONTRACT.md.

Проверяется форма границы: кого искать решает токен, порог и ограничения — на
стороне поиска, пустой результат это не ошибка, а отказ Elasticsearch — 503.
"""

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from elasticsearch.exceptions import NotFoundError, TransportError
from elasticsearch_dsl import Search
from elasticsearch_dsl.utils import AttrDict
from rest_framework.test import APIClient

from documents.models import ApiToken, Document
from documents.services.chunk_service import build_chunk_payloads
from documents.views.views_retrieve import RetrieveView

User = get_user_model()
URL = "/api/v1/search/retrieve"


class FakeHit(AttrDict):
    """Хит Elasticsearch: поля из _source и оценка в meta."""

    def __init__(self, score=1.0, **source):
        super().__init__(source)
        self["meta"] = AttrDict({"score": score})


def hit(document_id, chunk_index=0, score=1.0, **extra):
    source = {
        "document_id": document_id,
        "chunk_index": chunk_index,
        "chunk_total": 5,
        "document_version": "abc123",
        "title": "Документ",
        "text": f"отрывок {chunk_index}",
        "rubrics": ["devops"],
        "is_public": True,
    }
    source.update(extra)
    return FakeHit(score=score, **source)


def stub_search(hits, error=None):
    """Подменить выполнение запроса: Elasticsearch в тестах нет."""

    def fake_execute(self):
        if error is not None:
            raise error
        return list(hits)

    return mock.patch.object(Search, "execute", new=fake_execute)


class ApiTokenModelTest(TestCase):
    def test_issue_returns_value_once_and_stores_only_hash(self):
        token, raw = ApiToken.issue(name="semen", scope=ApiToken.SCOPE_SERVICE)

        self.assertTrue(raw.startswith("ds_s_"))
        self.assertEqual(token.key_hash, ApiToken.hash_value(raw))
        self.assertNotIn(raw, token.key_hash)
        self.assertTrue(token.matches(raw))
        self.assertFalse(token.matches("ds_s_другой"))

    def test_personal_token_requires_user(self):
        token = ApiToken(name="lapot", scope=ApiToken.SCOPE_PERSONAL)
        with self.assertRaises(ValidationError):
            token.full_clean()


class RetrieveAuthTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com", password="pass12345", is_active=True, is_email_verified=True
        )
        self.client = APIClient()

    def test_without_token_is_unauthorized(self):
        response = self.client.post(URL, {"query": "договор"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_invalid_token_is_unauthorized(self):
        # Значение только из ASCII: HTTP-заголовок не переносит кириллицу.
        self.client.credentials(HTTP_AUTHORIZATION="Bearer ds_s_no-such-token-value")
        response = self.client.post(URL, {"query": "договор"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_revoked_token_is_unauthorized(self):
        token, raw = ApiToken.issue("semen")
        token.is_active = False
        token.save(update_fields=["is_active"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        response = self.client.post(URL, {"query": "договор"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_user_id_in_request_is_refused(self):
        """Контракт §2: идентификатор пользователя в запросе запрещён."""
        _, raw = ApiToken.issue("semen")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        with stub_search([]):
            response = self.client.post(URL, {"query": "договор", "user_id": 1}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertIn("решает токен", response.data["error"])


class RetrieveVisibilityTest(TestCase):
    """Кого видит токен: служебный — только публичные, персональный — свои и публичные."""

    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.com", password="pass12345")
        self.other = User.objects.create_user(email="other@example.com", password="pass12345")
        self.service_token, self.service_raw = ApiToken.issue("semen", scope=ApiToken.SCOPE_SERVICE)
        self.personal_token, self.personal_raw = ApiToken.issue("lapot", scope=ApiToken.SCOPE_PERSONAL, user=self.owner)
        self.client = APIClient()

    @staticmethod
    def _body(api_token):
        from documents.views.views_retrieve import RetrieveView

        search = RetrieveView._build_search(api_token, "договор", [], 10)
        return search.to_dict()

    def test_service_token_sees_only_public(self):
        body = str(self._body(self.service_token))
        self.assertIn("is_public", body)
        self.assertNotIn("user_id", body)

    def test_personal_token_sees_own_and_public(self):
        body = str(self._body(self.personal_token))
        self.assertIn("is_public", body)
        self.assertIn("user_id", body)


class RetrieveContractTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="owner@example.com", password="pass12345")
        self.token, self.raw = ApiToken.issue("semen")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw}")

    def test_response_shape(self):
        with stub_search([hit(42, chunk_index=3, score=0.87)]):
            response = self.client.post(URL, {"query": "как деплоить"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["source"], "fulltext")
        result = response.data["results"][0]
        self.assertEqual(
            set(result),
            {
                "document_id",
                "chunk_index",
                "chunk_total",
                "document_version",
                "title",
                "text",
                "score",
                "rubrics",
                "is_public",
            },
        )
        self.assertEqual(result["document_id"], 42)
        self.assertEqual(result["chunk_index"], 3)
        self.assertEqual(result["text"], "отрывок 3")

    def test_empty_result_is_200_not_error(self):
        with stub_search([]):
            response = self.client.post(URL, {"query": "нет такого"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"results": [], "source": "fulltext"})

    def test_short_query_is_refused(self):
        with stub_search([]):
            response = self.client.post(URL, {"query": "ab"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_too_long_query_is_refused(self):
        with stub_search([]):
            response = self.client.post(URL, {"query": "я" * 1001}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_limit_is_capped_not_refused(self):
        with stub_search([hit(1, score=1.0)]):
            response = self.client.post(URL, {"query": "договор", "limit": 500}, format="json")
        self.assertEqual(response.status_code, 200)

    def test_rubrics_must_be_list_of_strings(self):
        with stub_search([]):
            response = self.client.post(URL, {"query": "договор", "rubrics": "devops"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_no_more_than_three_chunks_per_document(self):
        hits = [hit(7, chunk_index=index, score=10 - index) for index in range(10)]
        with stub_search(hits):
            response = self.client.post(URL, {"query": "договор", "limit": 10}, format="json")

        document_ids = [result["document_id"] for result in response.data["results"]]
        self.assertEqual(document_ids, [7, 7, 7])

    def test_elasticsearch_outage_is_503(self):
        with stub_search([], error=TransportError(503, "cluster_block_exception")):
            response = self.client.post(URL, {"query": "договор"}, format="json")

        self.assertEqual(response.status_code, 503)
        self.assertIn("недоступен", response.data["error"])

    def test_missing_index_is_503_not_empty(self):
        """Пустой ответ читался бы как «материала нет», а это неполадка."""
        with stub_search([], error=NotFoundError(404, "index_not_found_exception")):
            response = self.client.post(URL, {"query": "договор"}, format="json")

        self.assertEqual(response.status_code, 503)
        self.assertIn("не создан", response.data["error"])


class ChunkPayloadTest(TestCase):
    """Сборка кусков — чистая функция, проверяется без Elasticsearch."""

    def setUp(self):
        self.user = User.objects.create_user(email="owner@example.com", password="pass12345")

    def test_payload_carries_place_and_version(self):
        document = Document.objects.create(
            user=self.user,
            rubrics=["devops"],
            text="Первый абзац про деплой.\n\n" + "Второй абзац. " * 200,
            is_public=True,
        )

        payloads = build_chunk_payloads(document)

        self.assertGreater(len(payloads), 1)
        self.assertEqual(payloads[0]["chunk_index"], 0)
        self.assertEqual(payloads[0]["chunk_total"], len(payloads))
        self.assertEqual(payloads[0]["document_id"], document.pk)
        self.assertEqual(payloads[0]["rubrics"], ["devops"])
        self.assertTrue(payloads[0]["is_public"])
        self.assertTrue(payloads[0]["document_version"])
        self.assertEqual({payload["_id"] for payload in payloads}, {p["_id"] for p in payloads})

    def test_empty_document_gives_no_chunks(self):
        document = Document.objects.create(user=self.user, text="")
        self.assertEqual(build_chunk_payloads(document), [])

    def test_version_changes_with_text(self):
        document = Document.objects.create(user=self.user, text="Первый текст")
        first = build_chunk_payloads(document)[0]["document_version"]
        document.text = "Другой текст"
        document.save()
        second = build_chunk_payloads(document)[0]["document_version"]

        self.assertNotEqual(first, second)


class VisibilityFilterPlacementTest(TestCase):
    """Фильтр видимости обязан лежать **внутри** `knn`.

    Проверено на живом индексе: `knn` верхнего уровня **не учитывает**
    `query`, и запрос с фильтром снаружи возвращает **чужие куски**. Для
    проекта, где изоляция документов держится на ADR-0001, это прямой
    доступ к чужому.

    До этого теста инвариант держался **только на комментарии**: тесты
    подменяли `Search.execute`, векторная половина всегда падала, и ни одна
    проверка не смотрела на то, где стоит фильтр. Следующая правка
    `_knn_body` вернула бы чужие документы, и **ни один тест не упал бы**.
    """

    def setUp(self):
        # Вектор подменяем: тест про то, **где стоит фильтр**, а не про
        # модель. Без подмены `_knn_body` идёт в Ollama за эмбеддингом —
        # то есть проверка расположения фильтра зависела бы от того,
        # поднята ли модель на видеокарте.
        patcher = mock.patch(
            "documents.services.embedding_service.embed_query",
            return_value=[0.0] * 1024,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        user_model = get_user_model()
        self.owner = user_model.objects.create_user(email="owner@example.com", password="proverka-lapot-2026")
        # Через `issue`, а не `create`: значение токена придумывает он, и
        # без него `key_hash` у двух токенов совпадает — база справедливо
        # ругается на уникальность.
        self.token, _ = ApiToken.issue(name="lapot", scope=ApiToken.SCOPE_PERSONAL, user=self.owner)

    def test_the_filter_is_inside_the_knn_clause(self):
        """Фильтр лежит внутри `knn` — иначе он не действует вовсе."""
        body = RetrieveView._knn_body(self.token, "контейнеризация", 5)

        self.assertIn("knn", body)
        self.assertIn(
            "filter",
            body["knn"],
            "фильтра нет внутри knn — запрос вернёт чужие документы",
        )

    def test_the_filter_is_not_at_the_top_level(self):
        """Фильтра на верхнем уровне быть не должно: там он бесполезен.

        Именно на это ловятся: фильтр выглядит правильным, лежит рядом с
        `knn` — и **не применяется**, потому что `knn` верхнего уровня
        `query` не учитывает.
        """
        body = RetrieveView._knn_body(self.token, "контейнеризация", 5)

        self.assertNotIn("filter", body, "фильтр снаружи knn не действует")
        self.assertNotIn("query", body, "запрос снаружи knn не действует")

    def test_the_filter_restricts_to_the_person(self):
        """Персональный токен видит **своё и публичное**, а не всё.

        Проверяется не форма фильтра, а его смысл: в нём обязано быть
        ограничение по пользователю. Без него любой персональный токен
        видел бы все документы всех.
        """
        body = RetrieveView._knn_body(self.token, "контейнеризация", 5)
        clauses = body["knn"]["filter"]["bool"]["should"]
        as_text = json.dumps(clauses, ensure_ascii=False)

        self.assertIn("is_public", as_text, "нет условия на публичность")
        self.assertIn(
            str(self.owner.pk),
            as_text,
            "в фильтре нет ограничения по пользователю — видно чужое",
        )
        self.assertEqual(body["knn"]["filter"]["bool"]["minimum_should_match"], 1)

    def test_a_service_token_still_sees_only_public(self):
        """Служебный токен — только публичное.

        Бармен спрашивает корпус, и у него нет пользователя. Право на
        личные документы ему не выдаётся ни при каких условиях.
        """
        service_token, _ = ApiToken.issue(name="semen", scope=ApiToken.SCOPE_SERVICE)
        body = RetrieveView._knn_body(service_token, "контейнеризация", 5)
        as_text = json.dumps(body["knn"]["filter"], ensure_ascii=False)

        self.assertIn("is_public", as_text)
        self.assertNotIn("user_id", as_text, "служебному токену выдали личное")
