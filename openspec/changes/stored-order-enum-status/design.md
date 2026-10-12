# Design — StoredOrder.status as enum

## Context

После `uppercase-status-db` enum упрощён до `PENDING, PAID, REJECTED`,
но `StoredOrder.status` остался `String`. `OrderCharger` делает
`OrderStatus.valueOf(order.status())` — конвертация на месте использования.

## Решение

1. **`StoredOrder`** — тип поля `OrderStatus`:
   ```java
   public static record StoredOrder(String id, String userId, OrderStatus status, BigDecimal amountCents, String itemsJson) {}
   ```

2. **`OrderRepository.findById()`** — единственный мост DB→enum:
   ```java
   (rs, rowNum) -> new StoredOrder(
       rs.getString("id"),
       rs.getString("user_id"),
       OrderStatus.valueOf(rs.getString("status")),
       rs.getBigDecimal("amount_cents"),
       rs.getString("items")
   )
   ```

3. **`OrderCharger`** — убрать `valueOf()`:
   ```java
   OrderStatus status = order.status();
   ```

4. **`OrderQueryService`** — enum → lowercase для API:
   ```java
   return new Order(order.id(), order.status().name().toLowerCase(), order.amountCents());
   ```

5. **Тесты** — `StoredOrder`-мок принимает enum:
   ```java
   new StoredOrder("order-1", "user", OrderStatus.PENDING, new BigDecimal("1000"), "[]")
   ```

## Обязательные вопросы (по rules)

- **Бюджет 1 с**: не затронут.
- **Двойное списание**: не затронут.
- **Заказ с потерянным ответом → paid**: не затронут.
- **Нагрузка на Payment в блэк-холе**: не затронута.
