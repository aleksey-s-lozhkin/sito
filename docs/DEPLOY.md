# Развёртывание diploma

## Схема

diploma живёт на хосте приложений `infra-dev` вместе с другими проектами и не
поднимает собственных PostgreSQL, Redis, Elasticsearch и nginx — он пользуется
общими контейнерами в сети `infra`.

```text
sito.pyconstrictor.ru      → nginx → diploma-web:8000
docsearch.pyconstrictor.ru → nginx → 301 → sito.pyconstrictor.ru   (переходный период)

diploma-web:8000 ──┬── postgres:5432        (база diploma)
                   ├── redis:6379/1         (кэш и лимиты)
                   └── elasticsearch:9200   (индексы documents и chunks)
```

`diploma-web` не публикует ни одного порта на хост. nginx находит его по имени
контейнера в сети `infra`, поэтому снаружи приложение доступно только по HTTPS и
только с заголовками, которые проставил прокси.

Поиск и кэш — не критичные зависимости: при отказе Elasticsearch документы
продолжают создаваться, при отказе Redis — продолжает работать вход.
`/health/ready/` сообщит о них как о `degraded`.

## Переходный период: прежний домен

Приложение переименовано, но **прежний адрес `https://docsearch.pyconstrictor.ru`
продолжает работать**: общий nginx отдаёт с него редирект на новый домен. Так
задумано, а не оставлено по недосмотру: у людей сайт может быть открыт или лежать
в закладках, и без редиректа они получили бы ошибку сертификата — переезд
выглядел бы как поломка.

Пока переход не закончен, держится вместе вот это:

- **в окружении оба имени.** В `/srv/config/env/diploma.env`:

  ```bash
  ALLOWED_HOSTS=sito.pyconstrictor.ru,docsearch.pyconstrictor.ru,<IP>,localhost,127.0.0.1
  CSRF_TRUSTED_ORIGINS=https://sito.pyconstrictor.ru,https://docsearch.pyconstrictor.ru
  CORS_ALLOWED_ORIGINS=https://sito.pyconstrictor.ru,https://docsearch.pyconstrictor.ru
  ```

  Причина: nginx принимает запрос по старому имени и передаёт его приложению, а
  Django сверяет заголовок `Host` со списком. Уберёшь старое имя раньше
  редиректа — получишь `DisallowedHost` ровно у тех, ради кого переход и
  делается. Снимать имя можно только когда гаснет сам старый адрес;
- **оба сертификата живы.** Редирект по HTTPS возможен лишь после
  TLS-рукопожатия по старому имени, поэтому сертификат
  `/srv/data/letsencrypt/live/docsearch.pyconstrictor.ru/` остаётся в хранилище и
  в продлении рядом с новым (`docs/OPERATIONS.md`);
- **правка окружения требует пересоздания контейнера.** Три списка читаются
  `config/settings.py` один раз при старте, поэтому после изменения `.env`
  значения подхватит только `up -d` (автоматический деплой делает это сам).

Проверка, что переход идёт правильно, — в `docs/OPERATIONS.md`, раздел
«Переходный период». Заканчивает переход владелец: тогда снимаются старое имя из
трёх списков, старый vhost и его сертификат — **сразу все**, иначе останется либо
`DisallowedHost`, либо ошибка сертификата.

## Раскладка на сервере

| Путь | Что |
|---|---|
| `/srv/compose/diploma/compose.yaml` | `docker-compose.prod.yml` из репозитория |
| `/srv/compose/diploma/.env` | только образ и путь к окружению, без секретов |
| `/srv/config/env/diploma.env` | окружение и секреты приложения, права `600` |
| `/srv/config/nginx/conf.d/sito.pyconstrictor.ru.conf` | vhost в общем nginx |
| `/srv/config/nginx/conf.d/docsearch-redirect.conf` | переходный редирект со старого домена и ACME для его сертификата; снимается вместе со старым именем |
| `/srv/config/nginx/conf.d/pyconstrictor.ru-redirect.conf` | `pyconstrictor.ru` и `www` — редирект на Самогон |
| `/srv/config/nginx/conf.d/00-default-server.conf` | сервер по умолчанию: обрыв запросов по IP и на неизвестные имена |
| `/srv/data/diploma/static` | собранная статика, её отдаёт nginx |
| `/srv/data/diploma/media` | загруженные документы |
| `/srv/data/letsencrypt` | сертификаты (общий для всех проектов, владелец root) |
| `/srv/data/certbot` | ACME-каталог для продления (владелец root) |

Каталоги статики и медиа принадлежат uid 1000 — тому же, под которым работает
контейнер. Поэтому `collectstatic` и загрузка файлов идут без root и без `chown`.

## Что должно быть готово

- DNS `sito.pyconstrictor.ru` указывает на адрес хоста, порты 80 и 443
  открыты наружу.
- DNS прежнего имени (`docsearch.pyconstrictor.ru`) **тоже** указывает на хост:
  пока идёт переход, с него работает редирект. Снимать запись можно только
  вместе со старым vhost'ом.
- Есть Docker и внешняя сеть `infra`: `docker network inspect infra`.
- Работают общие контейнеры `postgres`, `redis`, `elasticsearch`, `nginx`.
- Пользователь деплоя входит в группу `docker`.
- Общий nginx смонтирован на `/srv/config/nginx/conf.d` и на
  `/srv/data/diploma/{static,media}`:

  ```yaml
  # /srv/compose/nginx/compose.yaml
        - /srv/config/nginx/conf.d:/etc/nginx/conf.d:ro
        - /srv/data/certbot:/var/www/certbot:ro
        - /srv/data/letsencrypt:/etc/letsencrypt:ro
        - /srv/data/diploma/static:/var/www/diploma/static:ro
        - /srv/data/diploma/media:/var/www/diploma/media:ro
  ```

## 1. База и роль в общем PostgreSQL

```bash
docker exec -it postgres psql -U postgres -d postgres
```

```sql
CREATE ROLE diploma LOGIN PASSWORD 'CHANGE_ME';
CREATE DATABASE diploma OWNER diploma;
\c diploma
GRANT ALL ON SCHEMA public TO diploma;
\q
```

Проверка:

```bash
docker exec postgres psql -U diploma -d diploma -c "SELECT current_database(), current_user"
```

## 2. Окружение приложения

```bash
cp .env.production.example /srv/config/env/diploma.env
chmod 600 /srv/config/env/diploma.env
nano /srv/config/env/diploma.env
```

Обязательно заменить:

- **`DJANGO_SECRET_KEY`** — `python -c "import secrets; print(secrets.token_urlsafe(64))"`;
- **`POSTGRES_PASSWORD`** — тот же, что у роли `diploma`;
- **`EMAIL_HOST_PASSWORD`** — пароль приложения Яндекс.Почты;
- **`REDIS_URL`** — база `/1`. База `/0` занята Самогоном: общий номер означает,
  что `cache.clear()` одного проекта стирает ключи другого. `/3`, `/4`, `/5`
  заняты лаптем;
- **`ALLOWED_HOSTS`** — оба домена (новый и прежний — на время перехода), IP
  хоста, `localhost` и `127.0.0.1`. Только новый домен указывать нельзя: запрос,
  пришедший на прежний и перенаправленный прокси в приложение, Django отклонит.

`ELASTICSEARCH_HOST=elasticsearch` менять не нужно: индексы `documents` и `chunks`
принадлежат этому проекту.

## 3. Файлы compose

```bash
scp docker-compose.prod.yml deploy-user@host:/srv/compose/diploma/compose.yaml
```

Рядом `.env` — без секретов, только то, что compose подставляет при разборе:

```bash
cat > /srv/compose/diploma/.env <<'EOF'
DIPLOMA_IMAGE=alserloz/diploma:latest
DIPLOMA_ENV_FILE=/srv/config/env/diploma.env
EOF
```

Проверить, что файл разбирается, ничего не запуская:

```bash
cd /srv/compose/diploma
DIPLOMA_IMAGE=alserloz/diploma:latest docker compose -f compose.yaml config --quiet
```

## 4. Сертификат Let's Encrypt

Сертификат выпускается **до** установки боевого vhost: тот ссылается на файлы
сертификата, и nginx не стартует, пока их нет. Сначала временный конфиг только с
проверочным путём:

```bash
cat > /srv/config/nginx/conf.d/sito-acme.conf <<'NGINX'
server {
    listen 80;
    server_name sito.pyconstrictor.ru;
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 503; }
}
NGINX
docker exec nginx nginx -t && docker exec nginx nginx -s reload
```

Убедиться, что проверочный путь доступен снаружи (каталог `/srv/data/certbot`
принадлежит root, поэтому файл создаётся контейнером):

```bash
docker run --rm -v /srv/data/certbot:/var/www/certbot alpine:3 \
  sh -c 'mkdir -p /var/www/certbot/.well-known/acme-challenge && echo ok > /var/www/certbot/.well-known/acme-challenge/probe'
curl -fsS http://sito.pyconstrictor.ru/.well-known/acme-challenge/probe
docker run --rm -v /srv/data/certbot:/var/www/certbot alpine:3 \
  sh -c 'rm -rf /var/www/certbot/.well-known'
```

Выпуск:

```bash
docker run --rm \
  -v /srv/data/certbot:/var/www/certbot \
  -v /srv/data/letsencrypt:/etc/letsencrypt \
  certbot/certbot certonly --webroot -w /var/www/certbot \
  -d sito.pyconstrictor.ru \
  --email ВАШ_ЯЩИК@pyconstrictor.ru --agree-tos --no-eff-email --non-interactive
```

Установка боевых vhost: новый домен и переходный редирект со старого. Заодно
удаляются два файла, которых быть не должно: временный ACME-конфиг и **прежний
vhost** — он переименован в `sito.pyconstrictor.ru.conf`, а два блока с одним
`server_name` дают `conflicting server name`: nginx может выбрать старый файл, и
тогда редирект со старого домена молча не сработает.

```bash
scp deploy/nginx/sito.pyconstrictor.ru.conf deploy/nginx/docsearch-redirect.conf \
  deploy-user@host:/srv/config/nginx/conf.d/
ssh deploy-user@host 'rm -f /srv/config/nginx/conf.d/sito-acme.conf \
  /srv/config/nginx/conf.d/docsearch.pyconstrictor.ru.conf'
docker exec nginx nginx -t && docker exec nginx nginx -s reload
```

После перезагрузки убеждаемся, что старое имя отвечает `301` на новое, а новое —
`200`: команды в `docs/OPERATIONS.md`, раздел «Переходный период». Сертификат
старого домена при этом остаётся на месте — без него HTTPS-редирект невозможен.

В vhost обязательна строка `client_max_body_size 20m`. Без неё действует дефолт
nginx в 1 МБ, и любая загрузка больше мегабайта получает `413 Request Entity Too
Large` **до** Django — в логах приложения при этом пусто. Именно это однажды
выглядело как «перестало работать добавление документов».

## 5. Первый деплой

```bash
cd /srv/compose/diploma
DIPLOMA_IMAGE=alserloz/diploma:latest docker compose -f compose.yaml pull
DIPLOMA_IMAGE=alserloz/diploma:latest docker compose -f compose.yaml up -d --no-build
docker compose ps
docker inspect --format '{{json .State.Health}}' diploma-web
```

Порядок: сервис `migrate` применяет миграции и собирает статику, и только после
его успешного выхода стартует `web` (`depends_on: service_completed_successfully`).
Неудачная миграция не оставит работающее приложение на неготовой базе.

При переходе с прежнего compose (там сервис назывался `diploma`, а не `web`)
добавьте `--remove-orphans`: контейнер с тем же именем, но другими метками,
иначе останется висеть и займёт имя. Автоматический деплой делает это сам.

Проверка:

```bash
curl -fsS https://sito.pyconstrictor.ru/health/
curl -fsS https://sito.pyconstrictor.ru/health/ready/
docker port diploma-web     # пусто: портов на хост нет
```

После первого деплоя индексы стоит пересобрать один раз — оба, и **вместе с
векторами**:

```bash
docker exec diploma-web python manage.py search_index --rebuild -f   # documents
docker exec diploma-web python manage.py reindex_chunks              # chunks
docker exec diploma-web python manage.py embed_chunks                # вектора
```

Индекс, созданный прежней версией кода, содержит одну реплику и на однонодовом
кластере навсегда остаётся в состоянии `yellow`. В коде реплик теперь 0, но
настройки применяются только при пересоздании индекса. Пересборка занимает
секунды и берёт данные из PostgreSQL — он единственный источник правды.

`embed_chunks` после `reindex_chunks` **обязателен**, и вот почему: пересборка
пересоздаёт индекс отрывков целиком и о векторах не знает. Без них поиск не
падает — он отвечает полнотекстовым и помечает ответ `source: "fulltext"`, то
есть тихо теряет половину возможностей. Дальше вектора добираются сами при каждой
загрузке документа; команда идемпотентна и повторный запуск ничего не пересчитает.

Ни один из этих шагов в автоматический деплой не вынесен намеренно: раньше
пересборка стояла в команде запуска, и недоступный Elasticsearch не давал
контейнеру подняться. `embed_chunks` вдобавок ходит в Ollama — недоступная модель
тоже не должна задерживать выкатку.

## 6. Автоматический деплой

Пуш в `main` запускает `publish-deploy.yml`: проверки → сборка образа с тегами
`latest` и `<sha>` → деплой именно `<sha>` по SSH.

`main` — ветка релизов (git flow): изменения приходят туда мержем из `develop`
(или из `hotfix/*`), и каждый такой мерж разворачивается. Обычные правки идут в
`develop` и на прод не попадают. Хотфикс, влитый в `main`, нужно затем влить и в
`develop`, иначе следующая правка вернёт исправленное место.

Секреты репозитория (не окружения) — те же имена, что уже заведены в этом
репозитории, поэтому добавлять ничего не нужно:

| Секрет | Что внутри |
|---|---|
| `DOCKER_USERNAME` | логин Docker Hub |
| `DOCKER_TOKEN` | access token Docker Hub, не пароль |
| `SSH_PRIVATE_KEY` | приватный ключ деплоя |
| `SSH_KNOWN_HOSTS` | вывод `ssh-keyscan -p <порт> <хост>` |
| `SSH_PORT` | порт SSH |
| `DEPLOY_HOST`, `DEPLOY_USER` | адрес и пользователь SSH |
| `SSH_KEY_PASSPHRASE` | необязательный: парольная фраза ключа, если он с ней |

`environment: production` в workflow не используется намеренно: секреты лежат на
уровне репозитория, а окружение без правил защиты ничего не добавляет, только
плодит второй список секретов.

Что делает деплой на сервере:

1. Запоминает работающий образ и ставит на него локальный тег `rollback`.
2. Проверяет свободное место: диск общий с другими проектами, и забить его
   образами diploma означает уронить соседей. При нехватке удаляет старые теги.
3. Загружает образ и поднимает сервисы.
4. Ждёт статус `healthy` до 180 секунд. Healthcheck проверяет `/health/ready/`,
   то есть и PostgreSQL.
5. Перезагружает nginx: пересозданный контейнер получил новый адрес в сети, и без
   `reload` часть запросов ушла бы в 502.
6. При любой ошибке возвращает предыдущий образ из тега `rollback`.

## 7. Обновление и откат

Обновление — пуш в `main`. Вручную:

```bash
cd /srv/compose/diploma
DIPLOMA_IMAGE=alserloz/diploma:<sha> docker compose -f compose.yaml pull
DIPLOMA_IMAGE=alserloz/diploma:<sha> docker compose -f compose.yaml up -d --no-build
```

Откат:

```bash
cd /srv/compose/diploma
DIPLOMA_IMAGE=alserloz/diploma:rollback docker compose -f compose.yaml up -d --no-build
docker exec nginx nginx -t && docker exec nginx nginx -s reload
```

**Откат не откатывает миграции.** Автоматика возвращает код и контейнеры, но
схема базы остаётся той, что применил неудачный деплой. Поэтому миграции должны
быть совместимы с предыдущей версией: сначала добавление полей и таблиц, удаление
старых — отдельным релизом.

## 8. Аварийный деплой без CI

Если CI недоступен, а исправление нужно на сервере немедленно, образ можно
собрать на самом хосте. Это отступление от конвенции (обычно образ собирает CI, а
сервер только загружает готовый), поэтому образ помечается SHA коммита и тег
`latest` не затирает.

```bash
cd /srv/compose/diploma
commit=<нужный коммит>

rm -rf /tmp/diploma-build
git clone --quiet --branch "$commit" --depth 1 \
  https://github.com/aleksey-s-lozhkin/sito.git /tmp/diploma-build
docker build --tag "alserloz/diploma:$commit" /tmp/diploma-build

# Запоминаем работающий образ, чтобы было куда вернуться
docker image tag "$(docker inspect --format '{{.Config.Image}}' diploma-web)" alserloz/diploma:rollback

DIPLOMA_IMAGE="alserloz/diploma:$commit" docker compose -f compose.yaml up -d --no-build --remove-orphans

for attempt in $(seq 1 90); do
  [ "$(docker inspect --format '{{.State.Health.Status}}' diploma-web 2>/dev/null)" = healthy ] && break
  sleep 2
done
docker inspect --format '{{json .State.Health}}' diploma-web
docker exec nginx nginx -t && docker exec nginx nginx -s reload
rm -rf /tmp/diploma-build
```

Откат — тот же тег `rollback`:

```bash
DIPLOMA_IMAGE=alserloz/diploma:rollback docker compose -f compose.yaml up -d --no-build --remove-orphans
```

Сборку можно проверить, не трогая работающий контейнер: собрать с другим тегом,
прогнать в нём `python manage.py migrate --check` и запустить на свободном порту
вида `-p 127.0.0.1:18000:8000`, чтобы посмотреть `/health/ready/`. Именно так
проверялся образ перед первым деплоем.

## 9. Продление сертификата

Продлением занимается задание в crontab пользователя деплоя (дважды в день,
certbot обращается к Let's Encrypt только если до истечения меньше 30 дней):

```cron
17 3,15 * * * /usr/bin/docker run --rm \
  -v /srv/data/certbot:/var/www/certbot \
  -v /srv/data/letsencrypt:/etc/letsencrypt \
  certbot/certbot renew --webroot -w /var/www/certbot --quiet \
  >> /home/alserloz/certbot-renew.log 2>&1 \
  && /usr/bin/docker exec nginx nginx -s reload >> /home/alserloz/certbot-renew.log 2>&1
```

Проверка механизма без изменений:

```bash
docker run --rm -v /srv/data/certbot:/var/www/certbot \
  -v /srv/data/letsencrypt:/etc/letsencrypt \
  certbot/certbot renew --webroot -w /var/www/certbot --dry-run
```

Хостовый `certbot.timer` здесь не помогает: он продлевает `/etc/letsencrypt`
хоста, а контейнерный nginx смонтирован на `/srv/data/letsencrypt`.

## Чего автоматика не делает

- **Не обновляет vhost.** `deploy/nginx/sito.pyconstrictor.ru.conf` и
  `deploy/nginx/docsearch-redirect.conf` копируются в общий nginx вручную, и
  старый `docsearch.pyconstrictor.ru.conf` удаляется тоже вручную: пока он лежит
  рядом, имена конфликтуют.
- **Не откатывает миграции.**
- **Не обновляет `.env`.** Изменения `ALLOWED_HOSTS` или `REDIS_URL` применяются
  только при пересоздании контейнера, то есть при следующем деплое.
- **Не делает бэкапы.** См. `docs/OPERATIONS.md`.
- **Не управляет общими PostgreSQL, Redis, Elasticsearch и nginx.**
- **Не хранит секреты.** Всё, что нужно приложению, лежит в
  `/srv/config/env/diploma.env` на сервере.
