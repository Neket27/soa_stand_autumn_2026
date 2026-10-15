-- Схема базы данных для сервиса заказов.
--
-- === ЧТО СДЕЛАТЬ САМОМУ ===
--
-- S1: Проверь, что при отклонении банком:
--     — Заказ переводится в REJECTED
--     — Резерв товара снимается
--     — release_pending = FALSE
--
-- S2: При 500 от Payment:
--     — Заказ остаётся PENDING
--     — Consumer повторяет попытку
--     — Если retry исчерпан — решение за тобой
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
