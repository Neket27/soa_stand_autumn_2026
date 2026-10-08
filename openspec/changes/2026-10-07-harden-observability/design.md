# Design: harden-observability

## D1. Логирование OrderProcessor

### Текущее состояние

```
OrderProcessor:
  processOnce         → нет логов
  processUntilTerminal → нет логов (charge, retry, reconciliation, transition)
  release()            → return value игнорируется
```

### Решение

```java
private final Logger log = LoggerFactory.getLogger(OrderProcessor.class);

// В processUntilTerminal, кейс CHARGED:
log.info("order={} charge=CHARGED, transitioning to paid", orderId);
int updated = orderRepository.transition(orderId, "paid");
log.info("order={} transition paid -> updated={}", orderId, updated);

// Кейс DECLINED:
log.info("order={} charge=DECLINED, releasing", orderId);
if (!release(orderId)) {
    log.warn("order={} release failed", orderId);
}

// Кейс SERVER_ERROR → retry:
log.info("order={} charge=SERVER_ERROR, retrying", orderId);

// Кейс NETWORK → reconciliation:
boolean found = paymentClient.hasCharges(orderId, chargeTimeout);
log.info("order={} reconciliation found={}", orderId, found);

// При неудачном release:
if (!inventoryClient.release(orderId, Duration.ofMillis(chargeTimeout))) {
    log.warn("order={} release failed after decline", orderId);
}

// При таймауте дедлайна:
if (System.currentTimeMillis() >= deadline) {
    log.warn("order={} deadline expired, still {}", orderId, order.status());
}
```

**Почему INFO, не DEBUG?** При разборе инцидента (вопрос «где заказ?» на защите)
INFO-логи доступны сразу, без переключения уровня.

**Сколько лога:** ~8 строк INFO + 1 WARN на заказ. При 50 заказах за прогон — ~400 строк.
Это приемлемо.

## D2. Логирование HTTP-клиентов

### PaymentClient.charge()

```java
} catch (java.io.IOException e) {
    log.warn("charge error: order={} client={}", orderId, e.getClass().getSimpleName());
    return ChargeResult.NETWORK;
}
```

### InventoryClient.reserve()

```java
} catch (java.io.IOException e) {
    log.warn("reserve error: order={}", orderId);
    return false;
}
```

## D3. OrderRepository.findById — fix

### Сейчас

```java
public Order findById(String id) {
    try {
        return jdbcTemplate.queryForObject(...);
    } catch (Exception e) {
        return null;  // EmptyResultDataAccessException, DataAccessException, PSQLException — всё null
    }
}
```

### Стало

```java
public Order findById(String id) {
    try {
        return jdbcTemplate.queryForObject(...);
    } catch (EmptyResultDataAccessException e) {
        return null;  // Заказ реально не найден
    }
}
// Ошибки БД → DataAccessException → propagate
```

## D4. enum OrderStatus + enum ProcessResult

### OrderStatus

```java
public enum OrderStatus {
    PENDING, PAID, REJECTED;

    public String value() {
        return name().toLowerCase();
    }

    public static OrderStatus fromValue(String v) {
        return valueOf(v.toUpperCase());
    }
}
```

### ProcessResult

```java
public enum ProcessResult {
    REJECTED, PENDING
}
```

### Точки замены

| Где | Было | Стало |
|---|---|---|
| `OrderRepository.transition(id, "paid")` | `String` | `transition(id, OrderStatus.PAID)` |
| SQL: `SET status = ? WHERE status = 'pending'` | literal `'pending'` | `? = 'pending'` (static), `toStatus.value()` (param) |
| `processOnce` return | `String` | `ProcessResult` |
| `if ("REJECTED".equals(result))` в контроллере | `result == ProcessResult.REJECTED` | |
| `OrderCreated(orderId, "rejected")` | `String` | `OrderCreated(orderId, OrderStatus.REJECTED.value())` |
| `OrderRepository.Order.status()` | `String` | `OrderStatus` |
| Row mapper: `rs.getString("status")` | | `OrderStatus.fromValue(rs.getString("status"))` |
| `orderRepository.findById(id).status()` | `String` | `OrderStatus.fromValue(...)` (т.к. record всё ещё из БД) |

Важный момент: `Order` record в `OrderRepository` хранит `String status` из БД.
В `processUntilTerminal` мы делаем `if ("paid".equals(order.status()))`.
Меняем на: `if (OrderStatus.PAID.value().equals(order.status()))`.

Также: `transition(id, "paid")` → `transition(id, OrderStatus.PAID.value())`.

Минимальный риск: `Order` record остаётся как есть (`String status`), мы просто
меняем константные сравнения на `OrderStatus.PAID.value()`. Это не добавляет новый
файл и не меняет record signature — меньше рисков рефакторинга.

## D5. Магические числа → application.properties

### application.properties

```properties
orders.process.budget-ms=900
orders.process.charge-timeout-ms=1000
orders.process.breaker-wait-ms=2000
orders.worker.deadline-ms=25000
orders.worker.count=8
```

### Injection (через @Value в конструкторах)

```java
// OrdersController
public OrdersController(...,
        @Value("${orders.process.budget-ms:900}") long budgetMs,
        ObjectMapper objectMapper) { ... }

// OrderProcessor
public OrderProcessor(...,
        @Value("${orders.process.charge-timeout-ms:1000}") long chargeTimeoutMs,
        @Value("${orders.process.breaker-wait-ms:2000}") long breakerWaitMs) { ... }

// BackgroundWorker
public BackgroundWorker(...,
        @Value("${orders.worker.deadline-ms:25000}") long deadlineMs,
        @Value("${orders.worker.count:8}") int workerCount) { ... }
```

## D6. Graceful shutdown BackgroundWorker

### Добавить поле

```java
private final List<Thread> workers = new CopyOnWriteArrayList<>();
```

### В startWorkers

```java
private void startWorkers(int numWorkers) {
    for (int i = 0; i < numWorkers; i++) {
        Thread t = new Thread(this::workerLoop, "bg-worker-" + i);
        t.setDaemon(true);
        workers.add(t);  // ← сохраняем
        t.start();
    }
}
```

### Добавить метод

```java
@PreDestroy
void shutdown() {
    workers.forEach(Thread::interrupt);
    log.info("BackgroundWorker shutdown, interrupted {} workers", workers.size());
}
```

## D7. Удаление мёртвого readTimeoutMs

### PaymentClient

- Удалить параметр `int readTimeoutMs` из конструктора
- Удалить `@Value("${PAYMENT.READ_TIMEOUT_MS:700}")`
- Остальная часть конструктора (connectTimeoutMs, ObjectMapper) — без изменений

## D8. Чистка unused imports

| Файл | Удалить |
|---|---|
| `ValidationExceptionHandler.java` | `FieldError`, `Collectors` |
| `DataSourceConfig.java` | `Properties`, переменная `scheme` (unused in JDBC URL building) |

## D9. Влияние на инварианты

| Инвариант | Влияние |
|---|---|
| Бюджет 1 с | Не меняется (900ms по умолчанию, тот же номер) |
| Не больше одного списания | Не меняется (idempotency-key, логика transition) |
| Деньги → paid | Не меняется |
| Rejected чистый | Улучшается: `release()` failure теперь логируется |
| Не перегружать Payment | Не меняется (breaker тот же, таймауты конфиги) |
| Контракт HTTP | Не меняется (API DTO: строки lowercase) |
| S1–S5 PASS | Утверждается в spec |

## D10. Риски и mitigations

| Риск | Митигация |
|---|---|
| `EmptyResultDataAccessException` не импортируется | Добавить `import org.springframework.dao.EmptyResultDataAccessException` |
| `@PreDestroy` на BackgroundWorker конфликтует с `@Component` | Нет конфликта — `@PreDestroy` — стандартная аннотация JSR-250 |
| Enum `value()` добавляет overhead | Инлайнится JIT, ноль затрат |
| Тесты используют магические числа для таймингов | После внедрения `@Value` тесты передают конкретные значения через конструктор (как сейчас) |
| Логирование в тестах | Логи по уровню INFO; тесты не проверяют логи (моки) |
| `CopyOnWriteArrayList` overhead | Запись в список только при старте (1 раз), чтение при shutdown (1 раз). Безвредно |
