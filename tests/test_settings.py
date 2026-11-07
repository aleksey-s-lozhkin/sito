"""Настройки, которые обязаны читаться из окружения.

CORS_ALLOWED_ORIGINS в продакшене молча подменялся захардкоженным списком
доменов, и переменная из .env не действовала: кросс-доменный клиент получал
отказ, причину которого не видно ни в логах, ни в настройках.
"""

import importlib
import os
from unittest import mock

from django.test import Client, SimpleTestCase, TestCase, override_settings

from config import settings as settings_module


class CorsSettingsTest(SimpleTestCase):
    def test_allowed_origins_come_from_environment(self):
        with mock.patch.dict(
            os.environ,
            {"CORS_ALLOWED_ORIGINS": "https://consumer.example, https://second.example"},
        ):
            importlib.reload(settings_module)
            try:
                self.assertEqual(
                    settings_module.CORS_ALLOWED_ORIGINS,
                    ["https://consumer.example", "https://second.example"],
                )
            finally:
                # Возвращаем модуль в исходное состояние, чтобы не влиять на
                # остальные тесты.
                importlib.reload(settings_module)

    def test_production_exempts_health_from_https_redirect(self):
        """Проверка самой настройки: в проде редирект включён, health — в исключениях"""
        with mock.patch.dict(os.environ, {"DJANGO_DEBUG": "False"}):
            importlib.reload(settings_module)
            try:
                self.assertTrue(settings_module.SECURE_SSL_REDIRECT)
                self.assertEqual(settings_module.SECURE_REDIRECT_EXEMPT, [r"^health/"])
            finally:
                importlib.reload(settings_module)

    def test_empty_entries_are_dropped(self):
        with mock.patch.dict(os.environ, {"CORS_ALLOWED_ORIGINS": "https://one.example,,"}):
            importlib.reload(settings_module)
            try:
                self.assertEqual(settings_module.CORS_ALLOWED_ORIGINS, ["https://one.example"])
            finally:
                importlib.reload(settings_module)


class OllamaAndEmailSettingsTest(SimpleTestCase):
    """Переменные, которые были в примерах окружения, но игнорировались кодом.

    ``OLLAMA_VISION_MODEL`` и ``EMAIL_BACKEND`` присутствовали в
    ``.env.example``, а настройки их не читали: зрительная модель всегда
    оставалась ``gemma3:4b``, бэкенд почты — SMTP.
    """

    def test_vision_model_comes_from_environment(self):
        with mock.patch.dict(os.environ, {"OLLAMA_VISION_MODEL": "gemma3:12b"}):
            importlib.reload(settings_module)
            try:
                self.assertEqual(settings_module.OLLAMA_VISION_MODEL, "gemma3:12b")
            finally:
                importlib.reload(settings_module)

    def test_email_backend_comes_from_environment(self):
        with mock.patch.dict(
            os.environ,
            {"EMAIL_BACKEND": "django.core.mail.backends.console.EmailBackend"},
        ):
            importlib.reload(settings_module)
            try:
                self.assertEqual(
                    settings_module.EMAIL_BACKEND,
                    "django.core.mail.backends.console.EmailBackend",
                )
            finally:
                importlib.reload(settings_module)


class HealthHttpsRedirectTest(TestCase):
    """Проверки состояния должны отвечать по http изнутри контейнера.

    Docker healthcheck опрашивает http://127.0.0.1:8000/health/ready/, а в
    продакшене включён SECURE_SSL_REDIRECT: без исключения запрос уходил на
    https, urllib получал таймаут рукопожатия, контейнер оставался unhealthy и
    гейт деплоя не пропускал ни один релиз.
    """

    def test_health_endpoints_are_exempt_from_https_redirect(self):
        with override_settings(
            SECURE_SSL_REDIRECT=True,
            SECURE_REDIRECT_EXEMPT=[r"^health/"],
            ALLOWED_HOSTS=["testserver"],
        ):
            client = Client()
            self.assertEqual(client.get("/health/").status_code, 200)
            self.assertEqual(client.get("/health/ready/").status_code, 200)

    def test_other_pages_still_redirect_to_https(self):
        with override_settings(
            SECURE_SSL_REDIRECT=True,
            SECURE_REDIRECT_EXEMPT=[r"^health/"],
            ALLOWED_HOSTS=["testserver"],
        ):
            client = Client()
            self.assertEqual(client.get("/login/").status_code, 301)
