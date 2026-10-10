## Context

Мотивация — в proposal.md (Why). Ограничение, определившее форму решения:
`contracts/orders.yaml` и `contracts/payment.yaml` — read-only (инвариант 7,
03-architecture.md), раннер шлёт `price_cents` (int64) и сверяет
`amount_cents` в фактах списаний. Черновик v1 этого change планировал
переименовать и wire-поля (`amount_cents → amount`, `price_cents → price`)
и БД-колонку (`NUMERIC(19,4)`) — это оказалось невозможным: контракт
неизменен, а раннер при `price` вместо `price_cents` даёт
`NPE` → `500` на каждом `POST /orders`. Решение переделано (v2):
`BigDecimal` — только внутреннее представление, wire не тронут.

## Goals / Non-Goals

**Goals:**
- Все денежные поля Java-кода — `BigDecimal` (центы, scale 0): DTO, домен,
  репозиторий, payment-клиент.
- Wire и БД — ровно по контракту: `price_cents` / `amount_cents` (int64),
  колонка `amount_cents BIGINT`.
- Поведение неизменно: S1–S5, суммы, статусы, бюджеты.

**Non-Goals:**
- Изменение JSON-полей, БД-колонки, контрактов Payment/Inventory.
- Введение дробных денег: домен — центы, `amountCents = Σ qty × priceCents`,
  всегда целое.

## Decisions

### 1. BigDecimal в центах (scale 0) вместо long

**Решение:** Все денежные поля — `BigDecimal` в центах, scale 0.

**Обоснование:**
- Инженерный стандарт (04-engineering-standards.md): «все денежные
  величины — `BigDecimal`, никаких float/double».
- Арифметика без риска переполнения и без float-округлений.
- Scale-0 `BigDecimal` сериализуется в JSON как целое → wire остаётся
  int64, контракт не нарушается.

**Альтернативы:**
- `double` — отклонена (неточность, NaN/Infinity).
- Дробные `amount`/`price` на wire (v1) — отклонена: нарушает read-only
  контракт; раннер шлёт `price_cents`.
- Оставить `long` — отклонена: стандарт.

### 2. Wire и БД не меняются

**Решение:** `price_cents` / `amount_cents` (int64) в JSON, колонка
`amount_cents BIGINT NOT NULL` в обоих диалектах (Postgres, H2).

**Обоснование:** Контракты read-only; раннер шлёт `price_cents` и сверяет
`amount_cents`. `BigDecimal` ↔ `BIGINT` отображается losslessly через JDBC
в обоих диалектах; драйвер возвращает scale 0.

**Альтернатива:** `amount NUMERIC(19,4)` (v1) — отклонена: scale 4
возвращает `99800.0000` из БД, что сериализуется в дробь на wire (ломает
int64-контракт) и ломает точное сравнение `BigDecimal.equals` в тестах.

### 3. Имена полей несут семантику «центы»

**Решение:** `Item.priceCents`, `Order.amountCents`,
`OrderRequest.amountCents()`, `StoredOrder.amountCents`,
`ChargeRequest.amountCents` — имя говорит «центы», тип — точность.

**Альтернатива:** имена `price`/`amount` (v1) — отклонены: имя не должно
создавать впечатление дробной величины.

### 4. InventoryClient.reserve — только sku + qty

**Решение:** Тело резерва собирается явно:
`{order_id, items: [{sku, qty}]}` — ровно по схеме
`ReservationRequest` в `contracts/inventory.yaml` (цены в ней нет).

**Обоснование:**
- Отправка всего `Item` DTO тащит в тело поле, которого нет в контрактной
  схеме.
- `ObjectMapper.valueToTree()` нормализует `BigDecimal` (`1.00` → `1`) —
  скрытая ловушка на money-типах; явная сборка её исключает.

**Альтернатива:** `valueToTree(items)` — отклонена (оба пункта выше).

## Инварианты (03-architecture.md)

| Инвариант | Влияние |
|---|---|
| Бюджет 1 с | не затронут (тайминги `CreateOrderService` без изменений) |
| Не больше одного списания | не затронут (Idempotency-Key, реконсиляция) |
| Деньги → `paid` | путь суммы изменён только по типу (scale 0), значения те же |
| Нагрузка на Payment в блэк-холе | не затронут |
| `rejected` чистый | не затронут |

## Risks / Trade-offs

- [Risk] `BigDecimal` с scale ≠ 0 попадёт на wire → mock читает int64 и
  сломается. [Mitigation] Все суммы — `Σ qty × priceCents`, обе операнды
  scale 0, результат scale 0; Jackson пишет целое.
- [Risk] H2/Postgres вернут `BigDecimal` с разным scale. [Mitigation]
  Колонка `BIGINT` → драйвер возвращает scale 0 в обоих диалектах.
- [Trade-off] `long` проще и короче, но стандарт требует `BigDecimal`;
  семантика центы несётся именем поля (`*Cents`).

## Migration Plan (как выполнено)

1. DTO: `Item.priceCents`, `Order.amountCents` — `BigDecimal` +
   `@JsonProperty("price_cents"/"amount_cents")`.
2. `OrderRequest.amountCents()` — `Σ qty × priceCents` (`BigDecimal`).
3. Репозиторий: `StoredOrder.amountCents`; SQL `amount_cents` (оба диалекта).
4. `schema.sql`: колонка остаётся `amount_cents BIGINT NOT NULL`.
5. `PaymentClient.charge(orderId, BigDecimal amountCents, timeout)`;
   `ChargeRequest.amount_cents`.
6. `InventoryClient.reserve` — явное тело sku+qty.
7. Сервисы: `CreateOrderService`, `OrderCharger` (тогда `OrderFulfiller`),
   `OrderQueryService`.
8. Тесты: суммы в центах (49900/99800/1000…), wire-макеры — int64.
9. Верификация: `cd orders && mvn -q test` — 73/73; `make scenarios TASK=1`
   — 5/5 `PASS` «полностью».

Откат: `git revert`; wire и БД не менялись — миграция данных не нужна
(база стенда пересоздаётся каждый прогон).

## Open Questions

(нет)
