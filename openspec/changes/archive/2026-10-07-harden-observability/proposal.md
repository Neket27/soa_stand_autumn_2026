# Change: harden-observability

## Why

Код работает (S1–S5 PASS, все 49 тестов зелёные), но по чек-листу продакшен-Spring
есть четыре класса пробелов:

1. **Наблюдаемость**: `OrderProcessor` — самая критичная часть (charge,
   reconciliation, transition) — работает без единого лога. При разборе инцидента
   («куда делся заказ», «почему деньги не спишутся дважды») логировать нечего.
   `PaymentClient`/`InventoryClient` молча возвращают `false`/`NETWORK` при таймаутах.
2. **Надёжность**: `OrderRepository.findById()` ловит `Exception` и возвращает `null`
   — DB-сбой маскируется под «заказ не найден» (404 вместо 500/лога).
3. **Чистота**: мёртвая конфигурация `PAYMENT.READ_TIMEOUT_MS` (принимается в
   конструкторе, не используется — per-request `.timeout()` перекрывает);
   unused imports в `ValidationExceptionHandler` и `DataSourceConfig`.
4. **Структура**: статусная машина на строках в двух регистрах (`"paid"` в БД/API,
   `"PENDING"` в `processOnce`) — опечатка превращается в тихий баг;
   магические числа (budget 900ms, charge timeout 1s, deadline 25s, workers 8)
   зашиты в код; потоки `BackgroundWorker` живут вне жизненного цикла Spring
   (нет `@PreDestroy`).

## What changes

### P0 — дыры

- Логирование в `OrderProcessor`: результат charge, reconciliation, transition,
  неудачный `release()`. В `BackgroundWorker`: заказ не дошёл до терминального
  статуса за дедлайн.
- Логирование в `PaymentClient`/`InventoryClient`: сетевые ошибки и таймауты —
  `log.warn` с `order_id`.
- `OrderRepository.findById()`: ловить только `EmptyResultDataAccessException` → `null`;
  прочие `DataAccessException` пробрасывать.
- Убрать мёртвый параметр `readTimeoutMs` из `PaymentClient` (и `@Value` к нему).
- Убрать unused imports: `ValidationExceptionHandler` (FieldError, Collectors),
  `DataSourceConfig` (Properties, переменная `scheme`).

### P1 — структура

- `enum OrderStatus { PENDING, PAID, REJECTED }` с `value()` (lowercase для БД/API)
  и `enum ProcessResult { REJECTED, PENDING }` — замена строк в
  `OrderProcessor`, `OrderRepository`, `OrdersController`, `BackgroundWorker` (где есть).
  DTO-граница API остаётся строкой (`OrderCreated`, `Order`) — контракт не трогается.
- Магические числа → `application.properties`:
  - `orders.process.budget-ms=900` (controller)
  - `orders.process.charge-timeout-ms=1000` (OrderProcessor, 3 места)
  - `orders.process.breaker-wait-ms=2000` (OrderProcessor)
  - `orders.worker.deadline-ms=25000`, `orders.worker.count=8` (BackgroundWorker)
- `BackgroundWorker`: хранить ссылки на worker-потоки, `@PreDestroy` —
  `Thread::interrupt` по всем (graceful shutdown).

## Scope

- `orders/src/main/java/orders/flow/OrderProcessor.java`
- `orders/src/main/java/orders/flow/BackgroundWorker.java`
- `orders/src/main/java/orders/payment/PaymentClient.java`
- `orders/src/main/java/orders/inventory/InventoryClient.java`
- `orders/src/main/java/orders/store/OrderRepository.java`
- `orders/src/main/java/orders/api/OrdersController.java`
- `orders/src/main/java/orders/api/ValidationExceptionHandler.java`
- `orders/src/main/java/orders/store/DataSourceConfig.java`
- Новый: `orders/src/main/java/orders/flow/OrderStatus.java`, `ProcessResult.java`
- `orders/src/main/resources/application.properties`
- Соответствующие тесты

## Non-goals

- Глобальный `@ExceptionHandler` для unexpected-исключений (отдельная тема, не согласовано).
- Bounded queue, rejection policy — YAGNI для нагрузки стенда.
- Удаление второго `HttpClient` в `PaymentClient` — сначала проверить, что пул общий.
- `@SpringBootTest`, hardening Dockerfile, non-root user.
- Изменение API-контракта, бизнес-логики, поведения при отказах.
- Изменение вне `orders/`.
