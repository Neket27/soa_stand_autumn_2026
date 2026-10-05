# Change: fix-interrupt-and-jsonb

## Why

При финальной проверке (all 5 runner scenarios S1–S5 на PostgreSQL) обнаружены два критических бага:

1. **Thread-interrupt corruption**: `Thread.currentThread().interrupt()` в catch-блоке `PaymentClient.charge()`
   и `hasCharges()` ставил флаг прерывания на рабочие потоки после `IOException` таймаутов.
   Это приводило к немедленному `InterruptedException` в `hasCharges()` и `Thread.sleep()` воркеров,
   убивая фоновую обработку заказов. В S3 воркеры «падали» после первого таймаута, заказы оставались pending.

2. **PostgreSQL JSONB incompatibility**: `OrderRepository` использовал plain `?` параметр для колонки
   `JSONB`. В H2 (тесты) это работало, но PostgreSQL отказывался принимать `VARCHAR` в `JSONB`-колонку
   без явного `CAST`:
   ```
   PSQLException: column "items" is of type jsonb but expression is of character varying
   ```
   В результате все `POST /orders` возвращали 500.

## What changes

- `PaymentClient.charge()`: в `catch (IOException)` **не восстанавливаем** interrupt flag.
  `InterruptedException` — только при реальном прерывании потока (shutdown).
- `PaymentClient.hasCharges()`: аналогично — `InterruptedException` только для real interrupts.
- `OrderRepository.insertPending()`: автодетекция БД через `DataSource.getDatabaseProductName()`.
  Для PostgreSQL — `CAST(? AS JSONB)`, для H2 — plain `?`.
- `OrderProcessor.network case`: `circuitBreaker.onTimeoutOrConnError()` вызывается **до** `return true`,
  чтобы состояние брейкера сохранилось при reject.

## Scope

- `orders/src/main/java/orders/payment/PaymentClient.java`
- `orders/src/main/java/orders/store/OrderRepository.java`
- `orders/src/main/java/orders/flow/OrderProcessor.java`

## Non-goals

- Изменение API или контрактов.
- Изменение тестов — они уже отражают правильное поведение.
- Изменение вне `orders/`.
