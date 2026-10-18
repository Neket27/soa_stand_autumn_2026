-- Схема базы данных для сервиса заказов.
--
-- Отклонение банком: заказ REJECTED + release_pending = TRUE → снятие
-- резерва (Consumer или Reaper), после успеха release_pending = FALSE.
-- Ошибка Payment: заказ PENDING, событие ретраит Consumer; PENDING
-- старше 30 с повторяет Reaper.
--
-- === СЕРИАЛИЗАЦИЯ ПРИЛОЖЕНИЯ СХЕМЫ ===
--
-- Схему применяют ВСЕ реплики сервиса одновременно (до 5 штук) на одну
-- БД. CREATE TABLE IF NOT EXISTS в PostgreSQL НЕ атомарен между сессиями:
-- параллельные реплики проходят проверку «таблицы нет» ДО коммита друг
-- друга, и проигравшая падает на дубликате композитного типа таблицы:
--   Key (typname, typnamespace)=(orders, 2200) already exists
-- (так падала одна из реплик при стресс-тесте — кластер терял реплику).
--
-- Классическое решение — распределённый мьютекс: pg_advisory_lock в
-- начале скрипта сериализует реплики, остальные после победителя видят
-- «уже существует» и спокойно пропускают. Session-лок снимается явным
-- unlock'ом в конце скрипта, а при падении скрипта — автоматически при
-- закрытии соединения (Spring закрывает его по завершении инициализации),
-- поэтому «зависшей» блокировки не бывает. ScriptUtils выполняет весь
-- скрипт на одном соединении — лок держится от первой до последней
-- команды.
SELECT pg_advisory_lock(71010001);

CREATE TABLE IF NOT EXISTS orders (
    id                TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'PENDING',
    amount_cents      BIGINT NOT NULL,
    items             JSONB NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    release_pending   BOOLEAN DEFAULT FALSE
);

-- Outbox (паттерн Transactional Outbox).
-- Событие пишется в ЭТУ ЖЕ транзакцию, что и заказ: либо обе записи
-- в БД, либо ни той, ни другой — dual-write с Kafka исключён.
-- OutboxDispatcher фоново читает неотправленные строки (sent_at IS NULL),
-- шлёт их в Kafka и помечает sent_at.
-- Доставка at-least-once: падение между send и markSent даст дубль
-- события, но Consumer идемпотентен (проверка статуса + Idempotency-Key).
CREATE TABLE IF NOT EXISTS outbox_events (
    id          BIGSERIAL PRIMARY KEY,
    order_id    TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at     TIMESTAMPTZ,
    claimed_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_outbox_unsent ON outbox_events (id) WHERE sent_at IS NULL;

-- Для существующих БД (схема в основном пересоздаётся, но на всякий случай):
-- claiming-колонка шардирует outbox между репликами (см. OutboxDispatcher).
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

-- Журнал компенсаций (двойник release_pending для случая, когда заказа
-- НЕТ вообще): резерв в Inventory создан ДО транзакции Orders, и если
-- создание заказа откатилось — резерв превратился бы в вечную утечку
-- (у заглушки нет TTL, Reaper ходит только по таблице orders).
--
-- CreateOrderService пишет сюда строку в ОТДЕЛЬНОЙ транзакции
-- (REQUIRES_NEW — иначе запись уедет вместе с откатом), а Reaper
-- повторяет DELETE /reservations/{order_id} до успеха. DELETE
-- идемпотентен, поэтому at-least-once повторы безопасны.
CREATE TABLE IF NOT EXISTS release_duties (
    order_id   TEXT PRIMARY KEY,
    attempts   INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Барьер готовности реплик после прогрева (см. WarmupRunner).
--
-- ЗАЧЕМ (найдено регрессией TASK=1 с агрессивным прогревом): раннер
-- стартует сценарий от /healthz=200 ЛЮБОЙ реплики, а прогрев идёт на
-- каждой независимо. Без барьера S1 начинался, пока 4 реплики из 5 ещё
-- дренировали прогрев-заказы: их оплаты (4624 чужих списания) попадали
-- в факты сценария и роняли проверку «paid без резерва».
--
-- Решение — классический распределённый барьер: реплика добавляет себя
-- ПОСЛЕ прогрева и дренажа, а /healthz=200 отдаёт только когда в таблице
-- все (WARMUP_BARRIER_EXPECTED, по умолчанию 1 — на официальной проверке одна
-- реплика; многорепликовый локальный запуск задаёт число реплик явно).
-- Пустая таблица = холодный старт кластера (БД пересоздаётся при каждом
-- make up), по ней же WarmupRunner отличает первый запуск от рестарта.
CREATE TABLE IF NOT EXISTS warmup_barrier (
    replica_id TEXT PRIMARY KEY,
    done_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Диагностика: логировать в docker logs postgres запросы дольше 200 мс.
-- Стресс-тесты показывают ~1,5-секундные «стоп-окна» внутри отправки
-- (reserve и tx замедляются одновременно) — чтобы отличить «БД встала»
-- от «сеть/хост встал», нужны имена и длительность конкретных запросов
-- в момент окна. Порог 200 мс: в норме такие запросы единичны, лог
-- остаётся компактным.
--
-- ALTER SYSTEM допустим: в стенде роль orders — суперпользователь
-- (POSTGRES_USER), схема выполняется при каждом старте сервиса, PG_reload
-- идемпотентен. Реплики (5 штук) выполняют его одновременно — блокировка
-- сериализует записи в postgresql.auto.conf, дубликат настройки безвреден.
ALTER SYSTEM SET log_min_duration_statement = '200ms';
SELECT pg_reload_conf();

-- Конец сериализованного блока (см. pg_advisory_lock в начале файла).
SELECT pg_advisory_unlock(71010001);
