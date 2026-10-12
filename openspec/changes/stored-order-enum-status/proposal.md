# Proposal — StoredOrder.status as enum (stored-order-enum-status)

## Why

`StoredOrder.status` — `String`, а `OrderCharger` делает
`OrderStatus.valueOf(order.status())` на месте использования.
После упрощения enum (без `value()`/`fromValue()`) это лишний слой:
конвертация DB-строка → enum должна происходить в одном месте — репозитории.

## What Changes

- `StoredOrder.status` — `String` → `OrderStatus`.
- `OrderRepository.findById()`: `OrderStatus.valueOf(rs.getString("status"))`.
- `OrderCharger`: `order.status()` вместо `OrderStatus.valueOf(order.status())`.
- `OrderQueryService`: `order.status().name().toLowerCase()`.
- Тесты: `StoredOrder`-мок — enum вместо строки.

## Capabilities

### New Capabilities

(нет)

### Modified Capabilities

(нет — поведение не меняется)

## Scope

- `orders/src/main/java/orders/store/OrderRepository.java`
- `orders/src/main/java/orders/flow/OrderCharger.java`
- `orders/src/main/java/orders/api/OrderQueryService.java`
- `orders/src/test/java/**` (StoredOrder-мок)

## Non-Goals

- Изменение API-контракта.
- Изменение `Order`/`OrderCreated` DTO.

## Impact

- **Код**: 3 main-файла + тесты.
- **Поведение**: byte-identical.
