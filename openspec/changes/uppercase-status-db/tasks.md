# Tasks — Uppercase status in DB + simplified enum (uppercase-status-db)

После каждой фазы: `cd orders && mvn -q test`.

## 1. OrderStatus — упрощение enum

- [x] 1.1 `OrderStatus.java`: удалить поле `value`, конструктор, `value()`, `fromValue()`.
  Результат: `public enum OrderStatus { PENDING, PAID, REJECTED }`.
- [x] 1.2 `OrderCharger.java:59`: `OrderStatus.fromValue(order.status())` →
  `OrderStatus.valueOf(order.status())`.

## 2. OrderRepository — SQL на uppercase

- [x] 2.1 `insertPending`: `'pending'` → `'PENDING'` (2 SQL-строки, postgres + h2).
- [x] 2.2 `transition`: `to.value()` → `to.name()`; `'pending'` → `'PENDING'`.
- [x] 2.3 `findReleasePendingIds`: `'rejected'` → `'REJECTED'`.
- [x] 2.4 `clearReleaseDuty`: `'rejected'` → `'REJECTED'`.
- [x] 2.5 `findPendingIds`: `'pending'` → `'PENDING'`.

## 3. schema.sql

- [x] 3.1 `CHECK (status IN ('PENDING','PAID','REJECTED'))`.

## 4. Граница API — lowercase для контракта

- [x] 4.1 `OrdersController:33`: `result.status().value()` →
  `result.status().name().toLowerCase()`.
- [x] 4.2 `OrderQueryService:23`: `order.status()` → `order.status().toLowerCase()`.

## 5. Тесты

- [x] 5.1 `OrderRepositoryTest`: assertions `"pending"`/`"paid"`/`"rejected"`
  → `"PENDING"`/`"PAID"`/`"REJECTED"` (5 мест).
- [x] 5.2 `OrderChargerTest`: `StoredOrder`-мок — status → uppercase
  (~20 мест).
- [x] 5.3 `OrderQueryServiceTest`: `StoredOrder`-мок → uppercase;
  assertion на `Order.status()` — остаётся lowercase (API-слой конвертирует).
- [x] 5.4 `OrdersControllerTest`: jsonPath-assertions — без изменений
  (API возвращает lowercase); `Order`-мок — без изменений (это API-DTO,
  уже lowercase).
- [ ] 5.5 `cd orders && mvn -q test` — все зелёные. (Blocked: JDK 21 не доступен)
- [x] 5.6 `git diff --name-only` — изменены только файлы в `orders/`.
