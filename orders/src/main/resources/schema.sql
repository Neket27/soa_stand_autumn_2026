-- Схема базы данных для сервиса заказов.
--
-- Отклонение банком: заказ REJECTED + release_pending = TRUE → снятие
-- резерва (Consumer или Reaper), после успеха release_pending = FALSE.
-- Ошибка Payment: заказ PENDING, событие ретраит Consumer; PENDING
-- старше 30 с повторяет Reaper.
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
    sent_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_outbox_unsent ON outbox_events (id) WHERE sent_at IS NULL;

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
