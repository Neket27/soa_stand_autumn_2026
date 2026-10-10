## Why

Инженерный стандарт требует для денежных величин `BigDecimal` (никаких
float/double), а работа с `long`-центами подвержена ошибкам переполнения и
лишним конвертациям. При этом HTTP-контракты `contracts/*.yaml` (read-only,
инвариант 7) фиксируют денежный wire-формат: целые центы `price_cents` /
`amount_cents` (int64). Поэтому изменение ограничено **внутренним
представлением**: wire и БД-колонка остаются в центах, а `BigDecimal`
используется внутри сервиса.

## What Changes

- Все денежные поля Java-кода — `BigDecimal` в центах (scale 0): DTO
  (`Item.priceCents`, `Order.amountCents`), доменная модель
  (`OrderRepository.StoredOrder.amountCents`), payment-клиент
  (`PaymentClient.charge`, `ChargeRequest.amountCents`).
- Имена полей несут семантику «центы»: `priceCents` / `amountCents` —
  wire-имена `price_cents` / `amount_cents` **не меняются**.
- БД-колонка остаётся `amount_cents BIGINT` (scale-0 `BigDecimal`
  отображается losslessly в обоих диалектах, H2 и Postgres).
- `InventoryClient.reserve` шлёт в теле только `sku` + `qty` (по
  `contracts/inventory.yaml` — цены в схеме резерва нет).
- **Контракт API не меняется**: JSON-поля, типы, статусы — как в
  `contracts/*.yaml`.

## Capabilities

### New Capabilities

(нет)

### Modified Capabilities

- `orders`: требование «Money values stay integer cents» заменяется на
  требование «Money values use BigDecimal internally» — тип и арифметика
  внутри сервиса изменены, wire-поведение идентично.

## Scope

- Только `orders/src/main/java/**` и `orders/src/test/java/**`.

## Non-Goals

- Изменение wire-полей, БД-колонки и контрактов Payment/Inventory.
- Десятичные деньги: домен — центы, `amountCents = Σ qty × priceCents`,
  всегда целое.

## Impact

- **Код**: DTO, репозиторий, сервисы, payment-клиент, inventory-клиент, тесты.
- **API / БД**: не затрагиваются (JSON-поля и колонка — те же).
- **Верификация**: `cd orders && mvn -q test` — 73/73; `make scenarios
  TASK=1` — 5/5 `PASS`, уровень «полностью».
