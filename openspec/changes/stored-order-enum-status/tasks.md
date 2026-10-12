# Tasks — StoredOrder.status as enum (stored-order-enum-status)

После задачи: `cd orders && mvn -q test`.

## 1. Main code

- [x] 1.1 `OrderRepository.java`:
  - `StoredOrder` record: `String status` → `OrderStatus status`.
  - `findById()`: `rs.getString("status")` → `OrderStatus.valueOf(rs.getString("status"))`.
- [x] 1.2 `OrderCharger.java`: `OrderStatus.valueOf(order.status())` → `order.status()`.
- [x] 1.3 `OrderQueryService.java`: `order.status().toLowerCase()` →
  `order.status().name().toLowerCase()`.

## 2. Tests

- [x] 2.1 `OrderChargerTest`: все `StoredOrder`-мок — String → enum
  (`"PENDING"` → `OrderStatus.PENDING`, `"PAID"` → `OrderStatus.PAID`,
  `"REJECTED"` → `OrderStatus.REJECTED`).
- [x] 2.2 `OrderRepositoryTest`: assertions на `order.status()` —
  String → enum (`"PENDING"` → `OrderStatus.PENDING` и т.д.).
- [x] 2.3 `OrderQueryServiceTest`: `StoredOrder`-мок → enum.
- [ ] 2.4 `cd orders && mvn -q test` — все зелёные. (Blocked: JDK 21 не доступен)
- [x] 2.5 `git diff --name-only` — изменены только файлы в `orders/`.
