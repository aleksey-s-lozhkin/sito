import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # Загружаем переменные окружения из .env файла

BASE_DIR = Path(__file__).resolve().parent.parent

# Безопасность
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "django-insecure-default-key-for-dev")
DEBUG = os.getenv("DJANGO_DEBUG", "False") == "True"
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

# Кастомная модель пользователя и бэкенд аутентификации по email
AUTH_USER_MODEL = "users.User"
AUTHENTICATION_BACKENDS = [
    "users.backends.EmailAuthBackend",
    "django.contrib.auth.backends.ModelBackend",
]

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # Third party
    "corsheaders",  # CORS поддержка
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "django_elasticsearch_dsl",
    "django_htmx",
    # Local
    "users",
    "documents",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",  # CORS middleware - должен быть как можно выше
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",  # HTMX поддержка
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Версия статики для ссылок на css и js.
                "config.context.asset_version",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# База данных PostgreSQL
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        # Имя базы намеренно осталось прежним: снаружи его не видит никто,
        # а переименование потребовало бы переноса данных ради косметики.
        # Приложение теперь называется «Сито», база — нет.
        "NAME": os.getenv("POSTGRES_DB", "docsearch"),
        "USER": os.getenv("POSTGRES_USER", "postgres"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "postgres"),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": os.getenv("DB_PORT", "5432"),
    }
}

# Elasticsearch для полнотекстового поиска
ELASTICSEARCH_DSL = {
    "default": {
        "hosts": (f"http://{os.getenv('ELASTICSEARCH_HOST', 'localhost')}:{os.getenv('ELASTICSEARCH_PORT', '9200')}"),
        # Короткий таймаут и одна попытка: поиск не должен держать запрос, а
        # недоступный ES не должен влиять на сохранение документов.
        "timeout": 5,
        "max_retries": 1,
        "retry_on_timeout": False,
    },
}

# Автоматические сигналы django-elasticsearch-dsl пишут в ES без обработки
# ошибок и тем самым роняют Document.save(). Индексация выполняется в
# documents/signals.py через transaction.on_commit и ошибок не пробрасывает.
ELASTICSEARCH_DSL_AUTOSYNC = False

# Redis (кеширование и очереди)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Локализация
LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True

# Статические файлы
# Описания и теги документов делает языковая модель на Ollama. Она может быть
# недоступна — тогда документ просто остаётся без описания: поиск и загрузка от
# неё не зависят.
OLLAMA_URL = os.getenv("OLLAMA_URL", "")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "90"))
#: Зрительная модель для `describe_unreadable_documents`. Отдельная переменная,
#: потому что она вытесняет общую `qwen3:8b` с единственной видеокарты, пока
#: работает; включать её в конвейер нельзя. До этой правки переменная была в
#: примерах окружения, но настройка её не читала — значение всегда оставалось
#: по умолчанию.
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "gemma3:4b")

# Модель эмбеддингов — отдельно от чат-модели.
#
# Почему это важно и почему именно так: **первая версия поиска по смыслу
# мерилась `nomic-embed-text`**, а корпус и вопросы русские. Модель
# англоязычная, и русские синонимы она почти не различает: «цикл for» ↔
# «повторить действие несколько раз» давало 0,632 против 0,607 у пары
# «конкатенация строк» ↔ «погода на Марсе». Разрыв 0,025.
#
# На `bge-m3` (многоязычная) тот же гибрид дал 13/15 против 8/15.
# Переменной была модель, а не подход.
#
# Размерность обязана совпадать с маппингом индекса: у nomic 768, у bge-m3
# 1024. Несовпадение не даёт понятной ошибки при поиске — вектор запроса
# просто не подходит, и выглядит это как «вариант не работает».
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "bge-m3")
OLLAMA_EMBED_DIMS = int(os.getenv("OLLAMA_EMBED_DIMS", "1024"))
#: Сколько знаков документа отправлять модели: остальное не влезает в контекст
#: и не нужно для описания.
SUMMARY_TEXT_LIMIT = int(os.getenv("SUMMARY_TEXT_LIMIT", "6000"))

STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "static")


STATICFILES_DIRS = (
    [os.path.join(BASE_DIR, "static_src")] if os.path.exists(os.path.join(BASE_DIR, "static_src")) else []
)

# Медиафайлы (загруженные документы)
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Настройки безопасности для продакшена
if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    X_FRAME_OPTIONS = "DENY"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# DRF (Django REST Framework)
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ("rest_framework_simplejwt.authentication.JWTAuthentication",),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "PAGE_SIZE": 10,
}

# API документация Swagger/OpenAPI
SPECTACULAR_SETTINGS = {
    "TITLE": "Сито API",
    "DESCRIPTION": "API для поиска по документам с аутентификацией",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# JWT настройки
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": False,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "VERIFYING_KEY": None,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
    "TOKEN_TYPE_CLAIM": "token_type",
}

HEALTH_CHECK_ENABLED = True

# URL для аутентификации и редиректов
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# Настройки безопасности для продакшена (дополнительные)
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_HSTS_SECONDS = 31536000  # 1 год
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

    # Проверки состояния опрашиваются по http изнутри контейнера (docker
    # healthcheck) и не должны попадать под редирект на HTTPS: иначе urllib
    # уходит на https://127.0.0.1:8000, получает таймаут рукопожатия и
    # контейнер навсегда остаётся unhealthy, а вместе с ним падает гейт деплоя.
    # Наружу эти адреса не отдаются: nginx проксирует только HTTPS.
    SECURE_REDIRECT_EXEMPT = [r"^health/"]

# =====================
# CORS НАСТРОЙКИ
# =====================

# Источники, которым разрешено обращаться к API кросс-доменно. Это origin
# вызывающего, а не наш домен: собственный веб-интерфейс работает с того же
# origin и в CORS не нуждается. Читается из окружения — раньше в продакшене
# список молча подменялся захардкоженным, и переменная из .env не действовала.
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",")
    if origin.strip()
]

# Разрешить отправку credentials (cookies, токены)
CORS_ALLOW_CREDENTIALS = True

# Разрешенные HTTP методы
CORS_ALLOW_METHODS = [
    "DELETE",
    "GET",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
]

# Разрешенные заголовки
CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]

# Время кеширования preflight запросов (в секундах)
CORS_PREFLIGHT_MAX_AGE = 86400  # 24 часа

# =====================
# CSRF НАСТРОЙКИ
# =====================

# Доверенные источники для CSRF. Читаются из окружения: при смене домена
# правка настроек не требуется, а захардкоженный список раньше перекрывал .env.
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "http://localhost,http://127.0.0.1").split(",")
    if origin.strip()
]

# CSRF настройки для HTMX
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"

# Кеширование через Redis
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        # Префикс переименован вместе с приложением. Старые ключи становятся
        # недостижимыми, но это безопасно: кэш — ускоритель, а не источник
        # правды (IGNORE_EXCEPTIONS ниже), и ключи сами истекают по TIMEOUT.
        # Номер базы Redis при этом не меняется — он закреплён за проектом.
        "KEY_PREFIX": "sito",
        "TIMEOUT": 300,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "CONNECTION_POOL_CLASS": "redis.BlockingConnectionPool",
            "CONNECTION_POOL_CLASS_KWARGS": {
                "max_connections": 100,
                "timeout": 30,
            },
            "SOCKET_CONNECT_TIMEOUT": 5,
            "SOCKET_TIMEOUT": 5,
            "RETRY_ON_TIMEOUT": True,
            # Кэш — ускоритель, а не источник правды: при недоступном Redis
            # запросы продолжают обслуживаться, а не отвечают 500.
            "IGNORE_EXCEPTIONS": True,
        },
    }
}

# Лимитирование запросов
RATELIMIT_USE_CACHE = "default"
RATELIMIT_CACHE_PREFIX = "rl:"
RATELIMIT_ENABLE = True

# Email настройки (для верификации)
#: Бэкенд читается из окружения: локально `.env.example` обещает консольный
#: вывод писем, на проде в `/srv/config/env/diploma.env` стоит SMTP. Пока
#: значение было захардкожено, локальная переменная молча игнорировалась.
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.yandex.ru")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", 465))
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "False") == "True"
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "True") == "True"
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", EMAIL_HOST_USER)

# Верификация email
VERIFICATION_EMAIL_TITLE = "Подтверждение email"
VERIFICATION_TOKEN_EXPIRATION_DAYS = 1

# Настройка логов
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}
