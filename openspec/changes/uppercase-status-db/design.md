# Design — Uppercase status in DB + simplified enum

## Context

Текущее состояние:

```java
public enum OrderStatus {
    PENDING("pending"), PAID("paid"), REJECTED("rejected");
    private final String value;
    // конструктор, value(), fromValue() — 26 строк
}
```

БД хранит lowercase. `fromValue()` — цикл по 3 элементам. `value()` вызывается
в 2 местах (controller, repository).

## Решение

**Внутренний формат — uppercase (enum `name()`), внешний (API) — lowercase.**

### 1. OrderStatus — 1 строка

```java
public enum OrderStatus { PENDING, PAID, REJECTED }
```

- `fromValue(s)` → `OrderStatus.valueOf(s)` (встроенный, быстрый).
- `value()` → `name().toLowerCase()` (на границе API).

### 2. schema.sql

```sql
CHECK (status IN ('PENDING','PAID','REJECTED'))
```

### 3. OrderRepository

- `INSERT ... VALUES (?, ?, 'PENDING', ...)`
- `WHERE status = 'PENDING'` / `WHERE status = 'REJECTED'`
- `transition`: `to.name()` вместо `to.value()`

### 4. OrderCharger

- `OrderStatus.valueOf(order.status())` вместо `OrderStatus.fromValue(order.status())`

### 5. Граница API (единственное место, где происходит конвертация)

- `OrdersController.createOrder()`: `result.status().name().toLowerCase()`
- `OrderQueryService.getOrder()`: `order.status().toLowerCase()`

### 6. StoredOrder

Остаётся `String status` — теперь содержит uppercase.
Никаких изменений в сигнатуре.

## Почему не `@JsonValue` / Jackson-маппинг

- `OrderStatus` не сериализуется напрямую в JSON: DTO (`Order`, `OrderCreated`)
  содержат `String status`, не enum.
- Jackson-аннотации усложнили бы код ради одного `.toLowerCase()`.

## Обязательные вопросы (по rules)

- **Бюджет 1 с**: не затронут.
- **Двойное списание**: не затронут.
- **Заказ с потерянным ответом → paid**: не затронут.
- **Нагрузка на Payment в блэк-холе**: не затронута.

## Trade-offs

- `valueOf()` бросает `IllegalArgumentException` на неизвестное значение —
  то же поведение, что у `fromValue()` (бросал `IllegalArgumentException`).
- Если в будущем появится новый статус — нужно добавить и в enum, и в CHECK.
  Как и сейчас.
