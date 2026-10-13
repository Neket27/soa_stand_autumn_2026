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
