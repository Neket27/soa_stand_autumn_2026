CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('PENDING','PAID','REJECTED')),
    amount_cents    BIGINT NOT NULL,
    items           JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    release_pending BOOLEAN NOT NULL DEFAULT FALSE
);

-- Durable-обязанность «снять резерв» для rejected-заказов (фикс утечек L1/L2).
-- Идемпотентно для баз, где таблица уже существует без этой колонки.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS release_pending BOOLEAN NOT NULL DEFAULT FALSE;
