# Proposal — Uppercase status in DB + simplified enum (uppercase-status-db)

## Why

`OrderStatus` несёт дублирование: Java-имя (`PAID`) и wire-строка (`"paid"`),
с мостом `fromValue()` / `value()`. Контракт API фиксирует нижний регистр,
но **внутреннее** представление (БД, enum) не обязано ему следовать.

Если БД хранит верхний регистр (`'PAID'`), enum упрощается до
`PENDING, PAID, REJECTED` без поля `value`, без `fromValue()`,
без конструктора. Конвертация в нижний регистр — только на границе API.

База пустая (стенд пересоздаёт БД через `make up`), миграция не нужна.

## What Changes

- **`OrderStatus`**: удалить поле `value`, конструктор, `fromValue()`, `value()`.
  Остаётся `public enum OrderStatus { PENDING, PAID, REJECTED }`.
- **`schema.sql`**: `CHECK (status IN ('PENDING','PAID','REJECTED'))`.
- **`OrderRepository`**: SQL-константы → верхний регистр;
  `transition` → `to.name()` вместо `to.value()`.
- **`OrderCharger`**: `OrderStatus.valueOf(order.status())` вместо `fromValue`.
- **Граница API** (нижний регистр для контракта):
  - `OrdersController`: `result.status().name().toLowerCase()`.
  - `OrderQueryService`: `order.status().toLowerCase()`.
- **Тесты**: `StoredOrder`-мок → верхний регистр;
  API-тесты (jsonPath) — без изменений (контракт не меняется).

## Capabilities

### New Capabilities

(нет)

### Modified Capabilities

(нет — поведение на уровне API-контракта не меняется)

## Scope

- `orders/src/main/java/orders/flow/OrderStatus.java`
- `orders/src/main/java/orders/store/OrderRepository.java`
- `orders/src/main/java/orders/flow/OrderCharger.java`
- `orders/src/main/java/orders/api/OrdersController.java`
- `orders/src/main/java/orders/api/OrderQueryService.java`
- `orders/src/main/resources/schema.sql`
- `orders/src/test/java/**` (assertions на статус)

## Non-Goals

- Изменение API-контракта (контракт остаётся lowercase).
- Изменение `StoredOrder.status()` типа (останется `String`).
- Введение `@JsonCreator` / Jackson-маппинга.

## Impact

- **API-контракт**: без изменений (lowercase в запросах/ответах).
- **БД**: значения в столбце `status` — теперь uppercase.
- **Код**: ~10 файлов, mostly однострочные замены.
- **Сценарии S1–S5**: без изменений (раннер читает HTTP-ответы, не БД).
