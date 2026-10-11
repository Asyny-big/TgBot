# Daily Content — запуск и эксплуатация

Daily Content — второй Telegram-бот магазина. VIP-покупатели (15+ завершённых
покупок) получают от него один новый материал в день в своё персональное окно;
покупатели с 20+ покупками могут предлагать контент. Владелец наполняет
библиотеку прямо в чате с Daily-ботом, а заявки, библиотеку, подписчиков и
доставки ведёт в админ-панели (раздел «Daily»).

Документ — runbook для оператора: настройка, запуск по шагам, бета, выключение,
откат и что проверять, если что-то идёт не так.

## Из чего состоит

| Компонент | Где работает | Что делает |
|---|---|---|
| `daily-bot` | контейнер, `python -m app.daily_bot`, webhook на `:8082` | общается с покупателями и владельцем; принимает контент и пожелания; запускает **watchdog** воркера |
| `daily-worker` | контейнер, `python -m app.daily_worker`, health на `:8083` | планирует и отправляет ежедневные доставки, outbox (алерты, ответы на пожелания, «прислать мне»), закрывает просроченное, считает запас и алерты, пишет **heartbeat** в Redis |
| кнопка «👑 Открыть VIP Daily» | основной бот магазина | под VIP-уведомлением и на экране «🏆 Мой прогресс»; deep link `https://t.me/<DAILY_BOT_USERNAME>?start=vip` |
| раздел «Daily» | админ-панель | сводка и алерты, пожелания, библиотека, подписчики, доставки |
| nginx `/webhook/daily` | edge | проксирует webhook Daily-бота в `daily-bot:8082` |

Оба Daily-контейнера описаны в `docker-compose.prod.yml` за профилем `daily`:
обычный `docker compose up` их не запускает, пока в `.env` нет
`COMPOSE_PROFILES=daily`. Магазин разворачивается как раньше, пока Daily не
настроен.

Миграции Daily — `0006` (схема), `0007` (outbox и алерты), `0008` (пожелания) —
только добавляют таблицы и колонки: предыдущий релиз магазина работает с ними
без изменений.

## 1. Бот в BotFather

1. `/newbot` → имя (например, «VIP Daily») и username (например,
   `VipDailyShopBot`). Это **отдельный** бот: токен основного бота использовать
   нельзя — его webhook перехватил бы обновления магазина. Процессы Daily
   проверяют это при старте.
2. Токен → `DAILY_BOT_TOKEN`, username без `@` → `DAILY_BOT_USERNAME`.
3. `/setjoingroups` → **Disable**: бот работает только в личных чатах.
4. `/setdescription` и `/setabouttext` — по желанию (что увидит покупатель до `/start`).
5. Команды задавать не нужно: бот публикует их сам при старте (меню покупателя и
   отдельное меню владельца в его личном чате).
6. Каждый аккаунт владельца из `DAILY_ADMIN_TELEGRAM_IDS` должен **один раз
   написать боту** (`/start`): без этого Telegram не даст боту прислать ему алерт
   или «📨 Прислать мне».

## 2. Переменные окружения

Полный список с комментариями — в `.env.example`, раздел «Daily Content».

| Переменная | Значение | Примечание |
|---|---|---|
| `COMPOSE_PROFILES` | `daily` | включает контейнеры `daily-bot` и `daily-worker` |
| `DAILY_ENABLED` | `false` / `true` | главный флаг; `false` — ничего не планируется и не отправляется |
| `DAILY_BETA_USER_IDS` | `111,222` | бета: обслуживаются только эти покупатели; пусто — все VIP |
| `DAILY_ADMIN_TELEGRAM_IDS` | `111` | владельцы: добавляют контент, получают алерты и watchdog |
| `DAILY_BOT_TOKEN` | токен BotFather | не токен магазина |
| `DAILY_BOT_USERNAME` | `VipDailyShopBot` | без `@`; без него кнопка в магазине не появится |
| `DAILY_WEBHOOK_BASE_URL` | `https://<TGSHOP_DOMAIN>` | тот же origin, что `TELEGRAM_WEBHOOK_BASE_URL` |
| `DAILY_WEBHOOK_SECRET` | `openssl rand -hex 32` | ≥ 16 символов, не равен секрету магазина |
| `DAILY_PROTECT_CONTENT` | `true` | материалы нельзя переслать или сохранить |
| `DAILY_HEARTBEAT_INTERVAL_SECONDS` | `15` | как часто воркер пишет heartbeat |
| `DAILY_WATCHDOG_INTERVAL_SECONDS` | `60` | как часто watchdog его проверяет |
| `DAILY_WATCHDOG_STALE_SECONDS` | `300` | тишина, после которой воркер считается упавшим (≥ 3 heartbeat) |
| `DAILY_WATCHDOG_COOLDOWN_MINUTES` | `60` | как часто напоминать, пока воркер лежит |

Путь webhook (`/webhook/daily`), порты `8082`/`8083` и размеры пулов БД для
Daily-контейнеров зафиксированы в `docker-compose.prod.yml` и из `.env` не
меняются.

Кнопка «👑 Открыть VIP Daily» показывается только на экранах программы
лояльности, поэтому для неё нужен ещё и `LOYALTY_ENABLED=true`. Без него Daily
работает, но в магазине нет входа в него.

Перед любым развёртыванием: `make preflight`. Скрипт проверяет и раздел Daily:
профиль, отличие токена и секрета от магазина, origin webhook, владельцев, бету
и связку с `LOYALTY_ENABLED`.

## 3. Webhook

- Telegram шлёт обновления на `DAILY_WEBHOOK_BASE_URL` + `/webhook/daily`.
- nginx (`location = /webhook/daily`) проксирует их в `daily-bot:8082`. Адрес
  резолвится на каждый запрос через DNS Docker, поэтому edge стартует и работает
  без Daily-контейнера: пока его нет, Telegram получает 502 и повторяет позже.
- Бот сам вызывает `setWebhook` при старте — с секретом
  (`X-Telegram-Bot-Api-Secret-Token`), `allowed_updates` и сбросом накопленных
  обновлений.
- Проверка: `getWebhookInfo` — поле `url` должно указывать на `/webhook/daily`,
  `last_error_message` — быть пустым. Токен в командную строку не вставляйте:
  храните его в переменной окружения сессии.

## 4. Первый запуск по шагам (флаг выключен)

Это обычное обновление магазина (README, «Updating the project») плюс раздел
Daily в `.env`.

1. **Резервная копия и метки образов:** `make backup`, затем пометить образы,
   которые работают сейчас, — откат (раздел 8) запускает их снова, а не
   пересобирает старый код:

   ```
   docker image tag tgshop-backend:latest tgshop-backend:pre-<release>
   docker image tag tgshop-edge:latest tgshop-edge:pre-<release>
   ```

   Миграции `0006`–`0008` применяются на production только вместе с
   развёртыванием и только после бэкапа.
2. `git pull`. Заполнить в `.env` раздел Daily (раздел 2): `DAILY_ENABLED=false`,
   `COMPOSE_PROFILES=daily`. Затем `make preflight` — без ошибок.
3. Собрать образы, проверить их, применить миграции и только потом
   пересоздавать контейнеры:

   ```
   make prod-build
   docker compose -f docker-compose.prod.yml run --rm --no-deps migrations \
     python -c "from app.core.config import get_settings; get_settings()"
   docker compose -f docker-compose.prod.yml run --rm --no-deps nginx nginx -t
   make prod-migrate
   make prod-up
   make prod-reload
   ```

   Первая проверка — новый код принимает этот `.env`, вторая — новый nginx
   принимает свою конфигурацию; обе идут во временных контейнерах и работающий
   магазин не трогают. `make prod-migrate` доводит схему до `0008`, пока старые
   контейнеры магазина продолжают работать. Если миграция упала, она
   откатывается целиком и ничего не меняет — дальше не идти (как вернуть
   прежние образы — README, «Updating the project»). Только после успешной
   миграции — `make prod-up`: стартуют новые контейнеры, включая `daily-bot` и
   `daily-worker`. Почему не сразу `make prod-up`: он сначала останавливает
   старые `api` и `bot` и лишь потом ждёт миграцию — упавшая миграция оставила
   бы магазин лежать.
4. `make prod-ps`: `daily-bot` и `daily-worker` в статусе `healthy`.
5. Владелец пишет Daily-боту `/start` и получает справку режима владельца.
6. **Наполнить библиотеку.** Отправить или переслать боту фото, видео, GIF,
   документы, аудио, голосовые, кружки — каждый файл сразу попадает в
   библиотеку. Текст добавляется кнопкой «➕ Добавить». `/stats` показывает
   библиотеку в цифрах. Приём работает и при выключенном флаге.

Пока `DAILY_ENABLED=false`, покупатель видит «Раздел скоро откроется.», воркер
крутится вхолостую, а кнопки в магазине нет.

## 5. Бета

1. В `.env`: `DAILY_ENABLED=true` и `DAILY_BETA_USER_IDS=<id тестовых покупателей>`.
   Убедиться, что `LOYALTY_ENABLED=true`.
2. Пересоздать процессы, читающие флаг: `make prod-restart` и
   `make prod-daily-restart`, затем `make prod-reload`. После правки `.env`
   Compose пересоздаёт `api` и `bot`, но не nginx, и тот должен заново найти
   их адреса (README, «Do not skip `make prod-reload`»).
3. Проверить на бета-покупателе с 15+ покупками:
   - «🏆 Мой прогресс» в магазине показывает «👑 Открыть VIP Daily»;
   - кнопка открывает Daily-бота; `/start` активирует подписку: приветствие и
     первый материал сразу;
   - на следующий день материал приходит в окно ±1 час от времени активации (МСК);
   - покупатель вне беты кнопки не видит, а Daily-бот отвечает ему «Раздел скоро
     откроется.».
4. Сводку смотреть в админ-панели (Daily → Сводка): подписчики, доставки за
   сегодня, запас, алерты.

## 6. Полный запуск

`DAILY_BETA_USER_IDS=` (пусто), затем `make prod-restart`, `make prod-daily-restart`
и `make prod-reload`.
Активируется каждый покупатель с 15+ завершёнными покупками, при первом
обращении к боту.

## 7. Выключение флага

`DAILY_ENABLED=false`, затем `make prod-restart`, `make prod-daily-restart` и
`make prod-reload`.

- Ничего не планируется и не отправляется. Бот отвечает «Раздел скоро
  откроется.», кнопка в магазине пропадает, watchdog молчит.
- Подписки, библиотека, история доставок и заявки сохраняются. После повторного
  включения доставки продолжаются со следующего окна; пропущенные дни **не
  досылаются**.

## 8. Откат

От мягкого к жёсткому:

1. **Флаг.** `DAILY_ENABLED=false` (раздел 7) — мгновенно останавливает всё
   поведение Daily, ничего не удаляя.
2. **Контейнеры.** Убрать `daily` из `COMPOSE_PROFILES`, затем
   `docker compose -f docker-compose.prod.yml --profile daily stop daily-bot daily-worker`.
   После этого вызвать у Daily-бота `deleteWebhook`, чтобы Telegram перестал
   повторять обновления на 502. Магазин этого не замечает.
3. **Код.** Если релиз, к которому возвращаетесь, Daily ещё не содержит (он
   старше `0006`), сначала выполнить пункт 2: `daily-bot` и `daily-worker`
   нужно остановить, пока текущий `docker-compose.prod.yml` их ещё описывает, —
   после `git checkout` Compose о них не знает и остановить их не сможет. Затем
   вернуть образы, помеченные перед обновлением (раздел 4, шаг 1), а не
   пересобирать старый код:

   ```
   git checkout <previous-tag>
   docker image tag tgshop-backend:pre-<release> tgshop-backend:latest
   docker image tag tgshop-edge:pre-<release> tgshop-edge:latest
   docker compose -f docker-compose.prod.yml up -d --no-deps --no-build api bot nginx
   make prod-reload
   ```

   Если Daily есть и в предыдущем релизе, его контейнеры не останавливают, а
   откатывают вместе с остальными — команда `up` тогда такая:

   ```
   docker compose -f docker-compose.prod.yml --profile daily up -d --no-deps --no-build api bot nginx daily-bot daily-worker
   ```

   Схема остаётся как есть: миграции `0006`–`0008` только добавляют таблицы и
   колонки, предыдущий релиз с ними совместим. Но его сервис `migrations` не
   знает новой ревизии и падает с `Can't locate revision identified by '<rev>'`.
   Поэтому `--no-deps` обязателен, а до следующего релиза нельзя
   `make prod-up`, `make prod-restart`, `make prod-daily-restart`,
   `make prod-migrate` и любой другой `up` без `--no-deps`: все они упираются в
   эту миграцию, а контейнеры, пересозданные по пути, остаются остановленными
   (README, «Rolling back»).
   `alembic downgrade` на production не выполнять: он не нужен и отказывает,
   если в таблицах Daily есть данные, — а они существуют только там.

## 9. Эксплуатация

**Здоровье:**

- `make prod-ps` — оба Daily-контейнера `healthy`.
- Сводка воркера изнутри контейнера — код ответа и тело. `http.client`
  печатает тело и при 503; `urllib.request.urlopen` на 503 бросает исключение,
  и список зависших циклов не виден:

  ```
  docker compose -f docker-compose.prod.yml exec daily-worker python -c \
    "import http.client; c = http.client.HTTPConnection('127.0.0.1', 8083, timeout=5); c.request('GET', '/internal/health'); r = c.getresponse(); print(r.status, r.read().decode())"
  ```

  Ответ содержит возраст каждого цикла (`planner`, `sender`, `outbox`, `expiry`,
  `evaluator`, `pulse`) и список зависших. Если какой-то цикл завис, ответ —
  503, и контейнер помечается `unhealthy`. Compose **не перезапускает**
  unhealthy-контейнеры сам — для этого есть watchdog и `make prod-daily-restart`.

**Восстановление базы из дампа** (`make restore f=backups/<файл>.dump`, README,
«The database is corrupt or the data is wrong»):

- `scripts/restore.sh` останавливает `daily-bot` и `daily-worker` вместе с
  `api` и `bot`: во время восстановления никто не пишет в базу — ни доставки,
  ни подписки, ни заявки.
- После восстановления запускает `api` и `bot`, а `daily-bot` и `daily-worker` —
  только если они работали до него (стек без Daily остаётся без Daily), и
  перезагружает nginx.
- Если схему не удалось довести до head, ничего не запускается; скрипт
  печатает команду запуска на после исправления.
- Всё, что Daily записал после момента дампа, теряется вместе с остальными
  данными: подписки, доставки, заявки и ответы на них. Покупатель может
  получить повторно материал, отправленный ему после дампа.
- Если восстановление длилось дольше `DAILY_WATCHDOG_STALE_SECONDS`, watchdog
  после запуска может один раз сообщить «🚨 Daily-воркер молчит», а следующей
  проверкой — «✅ снова работает»: воркер действительно был остановлен.

**Логи:** `make prod-daily-logs`. Полезные события:

- `daily_sender_pass` — работа отправителя;
- `daily_send_paused` — Telegram ответил 429, весь воркер ждёт `retry_after`;
- `daily_worker_pulse_withheld` — heartbeat не пишется, потому что зависли другие циклы;
- `daily_worker_loop_died` — цикл завершился сам, процесс выходит с кодом 1;
- `daily_watchdog_notice` — watchdog сообщил владельцу;
- `daily_bot_not_configured` / `daily_worker_not_configured` — не хватает настроек.

**Алерты владельцу:**

- через Daily-бота:
  - мало материалов: худшим 10% покупателей хватит меньше чем на `DAILY_LOW_STOCK_DAYS`;
  - материалы закончились у части подписчиков;
  - ошибки доставки выше порога;
  - watchdog (раздел 10);
- через **основной** бот:
  - Telegram отклонил токен Daily-бота — Daily-бот в этом случае сообщить не может.

У каждого алерта есть сообщение «снова в норме», напоминания приходят не чаще
cooldown.

## 10. Watchdog

- Воркер каждые `DAILY_HEARTBEAT_INTERVAL_SECONDS` пишет heartbeat в Redis —
  ключ `daily:heartbeat:daily-worker` (ISO-время, TTL 7 дней). Пишет только
  пока живы все остальные его циклы: воркер с зависшим отправителем для
  watchdog равен упавшему.
- `daily-bot` каждые `DAILY_WATCHDOG_INTERVAL_SECONDS` читает heartbeat:
  - тишина дольше `DAILY_WATCHDOG_STALE_SECONDS` → владельцу один раз уходит
    «🚨 Daily-воркер молчит N мин…»;
  - пока воркер лежит → напоминание не чаще раза в `DAILY_WATCHDOG_COOLDOWN_MINUTES`;
  - heartbeat снова свежий → один раз «✅ Daily-воркер снова работает. Перерыв: N мин.».
- Состояние хранится в Redis (`daily:watchdog:state`), поэтому рестарт бота не
  повторяет уже отправленный алерт. Проверки разных процессов сериализуются
  Redis-блокировкой.
- Состояние сохраняется только после того, как алерт дошёл хотя бы до одного
  владельца: недошедшее сообщение будет повторено на следующей проверке.
- При `DAILY_ENABLED=false` watchdog ничего не читает и не шлёт.
- Только что развёрнутый стек получает время: если heartbeat ещё ни разу не было
  видно, тишина отсчитывается от старта watchdog.

## 11. Что проверять при проблемах

**Пришло «🚨 Daily-воркер молчит»:**

1. `make prod-ps`: жив ли `daily-worker` и не в цикле ли рестартов.
2. `make prod-daily-logs`:
   - `daily_worker_not_configured` — не хватает настроек;
   - `daily_worker_loop_died` — упал цикл;
   - `daily_worker_pulse_withheld` — зависли циклы;
   - ошибки подключения к PostgreSQL или Redis.
3. Health воркера (раздел 9): какой цикл завис.
4. Время последнего heartbeat:

   ```
   docker compose -f docker-compose.prod.yml exec redis sh -c 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" GET daily:heartbeat:daily-worker'
   ```

   Одинарные кавычки обязательны: `$REDIS_PASSWORD` должен подставить shell
   внутри контейнера `redis`, где пароль задан. Shell сервера его не знает и
   подставил бы пустую строку.
5. `make prod-daily-restart`. Когда heartbeat станет свежим, придёт «✅ снова работает».

**Доставки не приходят покупателю:**

1. `DAILY_ENABLED=true`. При бете — покупатель есть в `DAILY_BETA_USER_IDS`.
2. Админ-панель → Daily → Подписчики, по Telegram ID:
   - статус `Активен`, не на паузе;
   - покупатель не заблокировал бота;
   - покупок ≥ 15 (считаются только завершённые покупки, как в программе лояльности).
3. Окно доставки — время активации ±1 час (МСК). Опоздавшая доставка
   досылается до 12 часов после центра окна (`DAILY_RECOVERY_HOURS`), позже
   день пропускается.
4. Daily → Доставки, по покупателю: статус, число попыток, `last_error`.
   - `Нет материалов` — покупатель видел всю библиотеку (алерт «материалы
     закончились»).
   - `Ошибка` с blocked — покупатель заблокировал бота.
5. Daily → Сводка: запас библиотеки и алерты.
6. В логах `daily_send_paused` — Telegram ограничивает частоту, отправка
   продолжится сама.

**Бот не отвечает покупателям:**

1. `getWebhookInfo`: `last_error_message`, `pending_update_count`.
2. 502 на `/webhook/daily` — не запущен `daily-bot` (`make prod-ps`).
3. 401 — расходится `DAILY_WEBHOOK_SECRET`; бот выставляет секрет сам при
   старте, поможет перезапуск после правки `.env`.
4. Пришёл алерт «токен отклонён» через основной бот — перевыпустить токен в
   BotFather и обновить `DAILY_BOT_TOKEN`.

**Ответ на пожелание или «прислать мне» не доходит:**

- Их отправляет воркер через outbox. Если воркер лежит, они ждут его до своего
  срока: ответ — 3 дня, «прислать мне» — 1 час. Daily → Пожелания показывает
  состояние каждого ответа (в очереди / доставлен / не доставлен).

**Watchdog шлёт лишнее или молчит:**

- Порог и cooldown — `DAILY_WATCHDOG_STALE_SECONDS`, `DAILY_WATCHDOG_COOLDOWN_MINUTES`.
- Сбросить память watchdog — с теми же одинарными кавычками, что и при чтении
  heartbeat выше:

  ```
  docker compose -f docker-compose.prod.yml exec redis sh -c 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" DEL daily:watchdog:state'
  ```

  Следующая проверка начнёт с «всё в порядке».

## 12. Бюджет соединений с PostgreSQL

`max_connections=100`, из них 3 зарезервированы для суперпользователя:

| Процесс | Пул | Пик |
|---|---|---|
| api (2 воркера uvicorn) | 10 + 10 | 40 |
| bot | 10 + 10 | 20 |
| daily-bot | 5 + 5 | 10 |
| daily-worker | 5 + 5 | 10 |
| **итого** | | **80** |

Запас остаётся на миграцию и сессию `psql` оператора. Увеличение пулов или
`API_WORKERS` нужно сверять с этой таблицей. Её же проверяет тест
`tests/test_launch_artifacts.py`.
