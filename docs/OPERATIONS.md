# Эксплуатация

Хост приложений `infra-dev`: 4 ядра, около 15 ГБ памяти, 58 ГБ диска. На нём
живут ещё десяток контейнеров, поэтому общие ресурсы ломаются тихо, а диск и
память нужно беречь.

## Что проверять первым

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
docker inspect --format '{{json .State.Health}}' diploma-web | head -c 400
curl -fsS https://sito.pyconstrictor.ru/health/ready/ | python3 -m json.tool
```

Ответ `/health/ready/`:

```json
{"status": "ok", "checks": {"database": "ok", "redis": "ok", "elasticsearch": "ok"}}
```

`503` и `"database": "error: ..."` — приложение действительно не работает.
`200` с `degraded` у `redis` или `elasticsearch` — работает, но без кэша или
поиска; это не повод перезапускать контейнер.

## Переходный период: старый домен ещё работает

Приложение переименовано, но **прежний адрес `https://docsearch.pyconstrictor.ru`
отвечает** — он отдаёт редирект на `https://sito.pyconstrictor.ru`. Это не
недоделка, а условие перехода: у людей сайт может быть открыт или лежать в
закладках, и без редиректа они получили бы ошибку сертификата вместо страницы.
Редирект живёт в `deploy/nginx/docsearch-redirect.conf` (на сервере —
`/srv/config/nginx/conf.d/docsearch-redirect.conf`); на стороне приложения для
него нужно второе имя в трёх списках окружения.

Что из этого следует при эксплуатации:

- в `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` и `CORS_ALLOWED_ORIGINS`
  (`/srv/config/env/diploma.env`) стоят **оба имени**. Снять старое раньше
  срока нельзя: Django ответит `DisallowedHost` на запрос, который nginx уже
  принял по старому имени, — то есть переход сломается ровно у тех, ради кого
  он делается;
- **сертификат прежнего имени не удалять, пока работает редирект.** Редирект
  возможен только после TLS-рукопожатия по старому имени, а рукопожатию нужен
  действующий сертификат в `/srv/data/letsencrypt/live/docsearch.pyconstrictor.ru/`.
  Поэтому имя оставлено в хранилище и в продлении;
- рядом живут два vhost'а: `sito.pyconstrictor.ru.conf` и переходный
  `docsearch-redirect.conf`. Во втором — и редирект, и ACME-путь для продления
  сертификата прежнего имени, который обязан оставаться доступным по HTTP без
  редиректа, иначе продление сломается. Как выпускается сертификат нового
  домена — `docs/DEPLOY.md`, раздел 4.

Проверка, что переход идёт правильно: старый адрес отвечает редиректом, новый —
`200`.

```bash
curl -sS -o /dev/null -w 'старый: %{http_code} -> %{redirect_url}\n' \
  https://docsearch.pyconstrictor.ru/health/
curl -fsS https://sito.pyconstrictor.ru/health/ready/ | python3 -m json.tool
```

Заканчивает переход владелец, и снимается он **целиком**: старое имя из трёх
списков окружения, старый vhost и его сертификат. По отдельности снимать
опасно — останется либо `DisallowedHost`, либо ошибка сертификата.

## Логи

```bash
docker logs --tail 200 diploma-web                # gunicorn: access и ошибки
docker logs --tail 100 diploma-migrate            # миграции и collectstatic
docker logs --tail 50 nginx
docker exec nginx sh -c 'tail -50 /var/log/nginx/access.log'
```

Ротация задана в compose (`max-size: 10m`, `max-file: 3`). До её введения лог
одного контейнера дорос до 1,7 млн строк — почти всё это `DisallowedHost` от
сканеров, которые ходят по IP. IP добавлен в `ALLOWED_HOSTS`, поэтому вместо
трассировок теперь обычные строки в access-логе.

Признаки в логах:

| Запись | Что значит |
|---|---|
| `Не удалось проиндексировать документ N` | Elasticsearch недоступен или отказал в записи. Документ в базе есть, поиском не находится |
| `Векторная половина недоступна, отвечаю полнотекстом` | Модель эмбеддингов или поле вектора недоступны. Поиск работает, но ищет по словам; проверить — раздел «Поиск отвечает по словам» ниже |
| `Кэш недоступен, лимит ... не применяется` | Redis недоступен, лимиты пропускают запросы |
| `DisallowedHost` | запрос пришёл на хост, которого нет в `ALLOWED_HOSTS` |
| `413 Request Entity Too Large` | ответ nginx до приложения: нет `client_max_body_size` в vhost либо файл больше 20 МБ |

## Диагностика отказов

### Не загружаются документы

Порядок проверки — от внешнего края к приложению:

```bash
# 1. Файл доходит до Django? 403 (нет CSRF) — доходит, 413 — рубит nginx
curl -sS -o /dev/null -w '%{http_code}\n' --resolve sito.pyconstrictor.ru:443:127.0.0.1 \
  -X POST --data-binary @файл https://sito.pyconstrictor.ru/documents/create/

# 2. Есть ли лимит в vhost
docker exec nginx grep -n client_max_body_size /etc/nginx/conf.d/sito.pyconstrictor.ru.conf

# 3. Доступен ли Elasticsearch из приложения
docker exec diploma-web python -c "
from elasticsearch_dsl.connections import connections
print(connections.get_connection().info()['version']['number'])"

# 4. Доступен ли Redis
docker exec diploma-web python -c "
from django.core.cache import cache; cache.set('probe', 1); print('redis:', cache.get('probe'))"
```

Создание документа не зависит от Elasticsearch и Redis: 500 при загрузке — это
база, права на каталог медиа или ошибка в коде, но не поиск.

### Пропали загруженные файлы

```bash
ls -la /srv/data/diploma/media/documents/2026/   # владелец должен быть uid 1000
docker exec diploma-web sh -c 'touch /app/media/.probe && rm /app/media/.probe && echo writable'
```

### Не ищется документ, который точно есть

```bash
# Сколько документов в базе и сколько в индексах
docker exec postgres psql -U diploma -d diploma -c "SELECT count(*) FROM documents_document;"
docker exec diploma-web python -c "
from elasticsearch_dsl.connections import connections
print(connections.get_connection().cat.indices(index='documents,chunks', format='json', h='index,health,docs.count,store.size'))"

# Пересобрать индексы из базы (единственный источник правды)
docker exec diploma-web python manage.py search_index --rebuild -f
docker exec diploma-web python manage.py reindex_chunks
docker exec diploma-web python manage.py embed_chunks   # без него не будет векторов
```

Здоровье индексов должно быть `green`: реплик у них нет (`number_of_replicas=0`),
поэтому `yellow` означал бы, что настройки не применились и индекс нужно
пересоздать.

### Поиск отвечает по словам, хотя должен по смыслу

Признак — `source: "fulltext"` в ответе `/api/v1/search/retrieve`, когда запрос
заведомо перефразированный. Порядок проверки:

```bash
# 1. Есть ли у отрывков вектора (их не будет после reindex_chunks, если забыть embed_chunks)
docker exec diploma-web python -c "
from elasticsearch_dsl.connections import connections
print(connections.get_connection().count(index='chunks', body={'query': {'exists': {'field': 'dense_vector'}}}))"

# 2. Добираются ли вектора (команда берёт только куски без векторов)
docker exec diploma-web python manage.py embed_chunks

# 3. Доступна ли модель эмбеддингов
docker exec diploma-web python -c "
from documents.services import embedding_service
print(embedding_service.model_name(), embedding_service.dimension())"
```

Векторная половина — **дополнение, а не замена**: её отказ не ломает поиск, и
именно поэтому о нём легко не узнать. В логе это видно строкой «Векторная
половина недоступна, отвечаю полнотекстом». Пусто в `OLLAMA_URL` означает, что
вектора выключены намеренно, — тогда `fulltext` это норма, а не поломка.

### Контейнер не становится healthy

```bash
docker inspect --format '{{json .State.Health}}' diploma-web | python3 -m json.tool
docker logs --tail 100 diploma-web
docker logs --tail 100 diploma-migrate
```

Healthcheck опрашивает `/health/ready/`, то есть PostgreSQL. Если база недоступна,
контейнер останется `unhealthy` и деплой откатится на предыдущий образ.

## Бэкапы

Приложение не делает резервных копий. Данные проекта — это база `diploma` и
каталог `/srv/data/diploma/media`.

```bash
# Дамп базы
docker exec postgres pg_dump -U diploma -d diploma -Fc > /srv/backups/diploma/diploma-$(date +%F).dump

# Восстановление
docker exec -i postgres pg_restore -U diploma -d diploma --clean --if-exists < dump
```

Индексы Elasticsearch бэкапить не нужно: оба производные и пересобираются из базы
командами `search_index --rebuild -f` и `reindex_chunks` — но после второй нужен
`embed_chunks`, иначе векторная половина поиска отключится молча.

## Ресурсы и место на диске

```bash
free -h
df -h /
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}' | sort -k2 -h
```

Образы diploma удаляются автоматически при деплое: остаются только текущий и
`rollback`. Если места мало, деплой сначала чистит старые теги, а при нехватке
5 ГиБ отказывается разворачиваться — это защита соседних проектов, а не сбой.

## Лишний трафик и регистрации

Публичный домен сканируют боты. Признаки: строки `DisallowedHost` в логах,
регистрации с неподтверждённых адресов.

Запросы по IP и на неизвестные имена домена обрываются на общем nginx
(`00-default-server.conf`: `return 444` для http и `ssl_reject_handshake` для
https). Проверочный путь ACME там оставлен рабочим, иначе нельзя было бы
выпустить сертификат новому домену. До этой правки сервером по умолчанию для
`:443` был vhost diploma, и лог приложения рос от сканеров: 1,7 млн строк.

```bash
# Кто зарегистрировался и что у него есть
docker exec postgres psql -U diploma -d diploma -c "
SELECT u.id, u.email, u.date_joined::date, u.is_email_verified,
       (SELECT count(*) FROM documents_document d WHERE d.user_id = u.id) AS docs
FROM users_user u ORDER BY u.id;"

# Удалить разом мёртвые аккаунты: не подтверждённые и без данных
docker exec -i postgres psql -U diploma -d diploma <<'SQL'
BEGIN;
CREATE TEMP TABLE junk AS
SELECT u.id FROM users_user u
WHERE NOT u.is_email_verified
  AND NOT EXISTS (SELECT 1 FROM documents_document d WHERE d.user_id = u.id)
  AND NOT EXISTS (SELECT 1 FROM documents_searchhistory h WHERE h.user_id = u.id)
  AND NOT EXISTS (SELECT 1 FROM django_admin_log l WHERE l.user_id = u.id);
DELETE FROM users_user u USING junk j WHERE u.id = j.id;
COMMIT;
SQL
```

Перед удалением обязателен дамп: `pg_dump -Fc`, см. раздел про бэкапы.

Особенности, которые нужно помнить:

- регистрация ставит `is_active=True` сразу, поэтому «неподтверждённый» и
  «неактивный» — не одно и то же; войти без подтверждения всё равно нельзя
  (`LoginView` и `APILoginView` проверяют `is_email_verified`);
- **API-регистрация возвращает access и refresh токены до подтверждения почты.**
  Это ослабляет смысл верификации: аккаунт сразу может пользоваться API.
  Исправление — не выдавать токены до подтверждения, но это изменение контракта,
  поэтому оно ждёт решения (другой проект может зависеть от текущего поведения);
- лимит регистрации — три попытки в час **на адрес**, поэтому волна регистраций
  с разных адресов им не ограничивается; при недоступном Redis лимит вообще не
  применяется (осознанный выбор в пользу доступности). Действенная мера против
  волны — капча на регистрации.

## Сертификат

```bash
docker run --rm -v /srv/data/letsencrypt:/etc/letsencrypt:ro \
  certbot/certbot certificates | grep -E 'Certificate Name|Domains|Expiry'
docker run --rm -v /srv/data/certbot:/var/www/certbot \
  -v /srv/data/letsencrypt:/etc/letsencrypt \
  certbot/certbot renew --webroot -w /var/www/certbot --dry-run
```

Продление — задание cron пользователя деплоя, см. `docs/DEPLOY.md`. Файл
`/srv/data/letsencrypt/archive/*/cert1.pem` в единственном числе означает, что
сертификат ни разу не продлевался.

### Сертификаты

Все домены хоста берут сертификаты из `/srv/data/letsencrypt`, который продлевает
cron пользователя деплоя (см. `docs/DEPLOY.md`): `sito`, `lapot`, `sam`,
`cloud` и общий на `pyconstrictor.ru`, `www.pyconstrictor.ru` и
`equip.pyconstrictor.ru`. Каталог `/srv/config/ssl` больше не используется —
файлы там остались от прежней схемы, ссылок на них в vhost'ах нет.

Сертификат прежнего имени (`docsearch.pyconstrictor.ru`) на время перехода
остаётся в хранилище и в продлении: без него не состоится TLS-рукопожатие по
старому адресу, а значит и редирект на новый. Подробнее — раздел «Переходный
период» выше.

```bash
# Сроки всех сертификатов хранилища
for d in /srv/data/letsencrypt/live/*/; do
  echo -n "$(basename "$d"): "
  docker run --rm -v "$d:/c:ro" alpine:3 sh -c \
    'apk add --no-cache openssl >/dev/null 2>&1; openssl x509 -in /c/fullchain.pem -noout -enddate'
done

# Проверка продления без изменений
docker run --rm -v /srv/data/certbot:/var/www/certbot \
  -v /srv/data/letsencrypt:/etc/letsencrypt \
  certbot/certbot renew --webroot -w /var/www/certbot --dry-run
```

Две вещи, без которых продление молча не работает:

- **проверочный путь ACME в `:80` блоке.** У `equip.pyconstrictor.ru` и
  `cloud.pyconstrictor.ru` его не было, поэтому сертификаты этих имён не
  продлевались: запрос уходил по редиректу на https, где его обрабатывало
  приложение. Новому домену без этого пути сертификат тоже не выпустить;
- **редирект внутри `location /`, а не на уровне сервера.** Серверный `return`
  в nginx выполняется до выбора location и перебивает ACME-путь.
