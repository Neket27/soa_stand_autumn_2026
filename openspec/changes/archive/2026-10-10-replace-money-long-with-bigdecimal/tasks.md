## 1. DTO (api/dto)

- [x] 1.1 `Item`: `long priceCents` → `BigDecimal priceCents`, `@JsonProperty("price_cents")`, валидация `@DecimalMin("0")`.
- [x] 1.2 `OrderRequest`: `amountCents()` = `Σ qty × priceCents` (`BigDecimal`-арифметика).
- [x] 1.3 `Order`: `@JsonProperty("amount_cents") BigDecimal amountCents`.

## 2. Домен и репозиторий (store)

- [x] 2.1 `OrderRepository` (record `StoredOrder`): `BigDecimal amountCents`.
- [x] 2.2 `insertPending`/`findById`: колонка `amount_cents` (SQL обоих диалектов), `rs.getBigDecimal("amount_cents")`.
- [x] 2.3 `schema.sql`: колонка остаётся `amount_cents BIGINT NOT NULL` (NUMERIC(19,4) из v1 не принят — см. design.md, решение 2).

## 3. Клиенты

- [x] 3.1 `PaymentClient`: `charge(String orderId, BigDecimal amountCents, Duration timeout)`; `ChargeRequest` — `@JsonProperty("amount_cents") BigDecimal amountCents` (JSON-целое).
- [x] 3.2 `InventoryClient.reserve`: тело `{order_id, items: [{sku, qty}]}` — по `contracts/inventory.yaml`, без цены; `valueToTree` убран.

## 4. Сервисы (flow/api)

- [x] 4.1 `CreateOrderService`: `request.amountCents()` → `insertPending`.
- [x] 4.2 `OrderCharger` (тогда `OrderFulfiller`): `order.amountCents()` → `paymentClient.charge` (charge + retry).
- [x] 4.3 `OrderQueryService`: `StoredOrder.amountCents` → DTO `Order.amountCents`.

## 5. Тесты

- [x] 5.1 Все суммы в центах (49900/99800/1000/500); wire-макеры — int64 (`"price_cents":49900`, `"amount_cents":99800`).
- [x] 5.2 `PaymentClientTest`: мок сверяет `"amount_cents":NN` в теле charge.
- [x] 5.3 `InventoryClientTest`: мок сверяет `order_id`/`sku`/`qty` (цены в теле нет).
- [x] 5.4 `cd orders && mvn -q test` — 73/73 зелёные.

## 6. Контрактная коррекция (обнаружено при верификации)

- [x] 6.1 Черновик v1 ломал wire (JSON-поля `price`/`amount`): раннер шлёт `price_cents` → `price = null` → NPE → 500 на каждом POST. Возвращены `price_cents`/`amount_cents` в JSON и `amount_cents BIGINT` в БД; `BigDecimal` оставлен внутренним представлением.
- [x] 6.2 `make scenarios ONLY=S5 WAIT=5` — PASS.
- [x] 6.3 `make scenarios TASK=1` — 5/5 PASS, уровень «полностью» (нет pending в конце, нет двойных списаний).
