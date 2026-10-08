# Tasks: harden-observability

- [x] Task 1: Логирование в OrderProcessor + PaymentClient + InventoryClient + BackgroundWorker
- [x] Task 2: OrderRepository.findById — ловить только EmptyResultDataAccessException
- [x] Task 3: Убрать мёртвый readTimeoutMs + unused imports
- [x] Task 4: Магические числа → application.properties + конструкторы
- [x] Task 5: @PreDestroy graceful shutdown в BackgroundWorker
- [x] Task 6: Интеграционная проверка (mvn test — green, Java 21)

---

## Task 1: Логирование в OrderProcessor + PaymentClient + InventoryClient

**Файлы:**
- `orders/src/main/java/orders/flow/OrderProcessor.java`
- `orders/src/main/java/orders/payment/PaymentClient.java`
- `orders/src/main/java/orders/inventory/InventoryClient.java`
- `orders/src/main/java/orders/flow/BackgroundWorker.java`

**Изменения:**

1. `OrderProcessor`: добавить `private static final Logger log = LoggerFactory.getLogger(OrderProcessor.class)`.
   Логировать в `processUntilTerminal`:
   - После `paymentClient.charge()` — INFO с order_id и chargeResult (CHARGED/DECLINED/SERVER_ERROR/NWORK)
   - После reconciliation (hasCharges) — INFO с order_id и result (found/not found)
   - При failed `release()` — WARN с order_id

2. `PaymentClient.charge()`: в `catch (java.io.IOException)` — добавить `log.warn("charge error: order={} ({})", orderId, e.getClass().getSimpleName())`.

3. `InventoryClient.reserve()`: в `catch (java.io.IOException)` — добавить `log.warn("reserve error: order={}", orderId)`.

4. `BackgroundWorker.workerLoop()`: после `processUntilTerminal` — если `false`, лог WARN: `"order={} deadline expired"`.

**Тесты:** unit-тесты не проверяют логи — пропускаем.

**Верификация:** `cd orders && mvn -q test` — green.

---

## Task 2: OrderRepository.findById — ловить только EmptyResultDataAccessException

**Файлы:**
- `orders/src/main/java/orders/store/OrderRepository.java`

**Изменения:**

1. Убрать `catch (Exception e) { return null; }` из `findById()`.
2. Заменить на: `catch (EmptyResultDataAccessException e) { return null; }`.
3. Добавить `import org.springframework.dao.EmptyResultDataAccessException;`.
4. В `insertPending()` — оставить текущий `catch (JsonProcessingException e) { throw new ... }`.

**Тесты:** `OrderRepositoryTest` — `findByIdNotFound` возвращает null для отсутствующего заказа. Тест не бросает DataAccessException.

**Верификация:** `cd orders && mvn -q test` — green.

---

## Task 3: Убрать мёртвый readTimeoutMs + unused imports

**Файлы:**
- `orders/src/main/java/orders/payment/PaymentClient.java`
- `orders/src/main/java/orders/api/ValidationExceptionHandler.java`
- `orders/src/main/java/orders/store/DataSourceConfig.java`

**Изменения:**

1. `PaymentClient`:
   - Удалить параметр `int readTimeoutMs` из конструктора.
   - Удалить `@Value("${PAYMENT.READ_TIMEOUT_MS:700}")`.
   - Остальная логика (connectTimeoutMs, ObjectMapper) — без изменений.

2. `ValidationExceptionHandler`:
   - Удалить `FieldError`, `Collectors` из импортов (не используются).

3. `DataSourceConfig`:
   - Удалить `Properties` из импортов.
   - Удалить переменную `scheme` — она объявлена, но не используется (JDBC URL строится без неё).

**Тесты:** `PaymentClientTest` — обновить конструктор (убрать 3-й параметр).

**Верификация:** `cd orders && mvn -q test` — green.

---

## Task 4: Магические числа → application.properties

**Файлы:**
- `orders/src/main/resources/application.properties`
- `orders/src/main/java/orders/api/OrdersController.java`
- `orders/src/main/java/orders/flow/OrderProcessor.java`
- `orders/src/main/java/orders/flow/BackgroundWorker.java`

**Изменения:**

1. `application.properties` — добавить:
   ```properties
   orders.process.budget-ms=900
   orders.process.charge-timeout-ms=1000
   orders.process.breaker-wait-ms=2000
   orders.worker.deadline-ms=25000
   orders.worker.count=8
   ```

2. `OrdersController`:
   - Добавить `@Value("${orders.process.budget-ms:900}") long budgetMs` в конструктор.
   - Заменить `Duration.ofMillis(900)` на `Duration.ofMillis(budgetMs)`.

3. `OrderProcessor`:
   - Добавить в конструктор `@Value("${orders.process.charge-timeout-ms:1000}") long chargeTimeoutMs`
     и `@Value("${orders.process.breaker-wait-ms:2000}") long breakerWaitMs`.
   - Заменить `Duration.ofSeconds(1)` (3 места: charge, retry, hasCharges) на `Duration.ofMillis(chargeTimeoutMs)`.
   - Заменить `Thread.sleep(2000)` на `Thread.sleep(breakerWaitMs)`.

4. `BackgroundWorker`:
   - Заменить `NUM_WORKERS = 8` на `@Value("${orders.worker.count:8}") int workerCount` в конструкторе.
   - Заменить `MAX_DEADLINE = 25s` на `@Value("${orders.worker.deadline-ms:25000}") long deadlineMs` в конструкторе.
   - Удалить `private static final`.

**Тесты:** `BackgroundWorkerTest`, `OrderProcessorTest`, `PaymentCircuitBreakerTest` — тесты создают классы через `new`, поэтому конструктор должен принимать и plain-параметры.

**Проблема:** Spring `@Value` — это только при инъекции через контекст. Тесты вызывают конструктор напрямую. Если мы добавим `@Value` к параметрам, тесты будут ломаться — конструктор будет меняться, и тесты будут вызывать его с другими параметрами.

**Решение:** добавить перегруженный конструктор для тестов (без @Value), основной — с `@Value`:

```java
public BackgroundWorker(OrderRepository r, OrderProcessor p, PaymentCircuitBreaker cb) {
    this(r, p, cb, 8, 25000);
}

public BackgroundWorker(OrderRepository r, OrderProcessor p, PaymentCircuitBreaker cb,
        @Value("${orders.worker.count:8}") int workerCount,
        @Value("${orders.worker.deadline-ms:25000}") long deadlineMs) {
    // ...
}
```

Нет, так нельзя — `@Value` не копируется автоматически. Lombok `@RequiredArgsConstructor` тоже не сработает для `@Value`.

**Правильное решение:** использовать `@ConfigurationProperties`-подход через отдельный config-класс, или `@Value` на полях + setter-инъекция, или оставить конструкторы для тестов без `@Value`.

На самом деле проще всего: `@Value` на параметры конструктора + тесты вызывают конструктор напрямую. `@Value` аннотация на параметр — это метаданные, Spring её обрабатывает при создании бина, а plain `new` просто игнорирует. Это работает из коробки. Тесты просто передадут нужные значения напрямую.

Таким образом: конструктор `BackgroundWorker` меняет сигнатуру:
```java
public BackgroundWorker(OrderRepository orderRepository,
                        OrderProcessor orderProcessor,
                        PaymentCircuitBreaker circuitBreaker,
                        @Value("${orders.worker.count:8}") int workerCount,
                        @Value("${orders.worker.deadline-ms:25000}") long deadlineMs) {
```

Тест `BackgroundWorkerTest` — обновить вызовы конструктора с 4-м и 5-м параметром (уже есть `BackgroundWorker(repo, proc, breaker, 2)` — добавить `deadlineMs`).

То же для `OrderProcessor` — 2 новых параметра в конструкторе.
То же для `OrdersController` — 1 новый параметр.

**Тесты обновить:**
- `BackgroundWorkerTest` — передать 5 параметров (workerCount, deadlineMs).
- `OrderProcessorTest` — передать 2 параметра (chargeTimeoutMs, breakerWaitMs).
- `OrdersControllerTest` — передать 1 параметр (budgetMs).

**Верификация:** `cd orders && mvn -q test` — green.

---

## Task 5: @PreDestroy graceful shutdown в BackgroundWorker

**Файлы:**
- `orders/src/main/java/orders/flow/BackgroundWorker.java`

**Изменения:**

1. Добавить поле `private final List<Thread> workers = new CopyOnWriteArrayList<>();`
2. В `startWorkers`: после создания потока — `workers.add(t);`
3. Добавить метод:
   ```java
   @PreDestroy
   void shutdown() {
       workers.forEach(Thread::interrupt);
   }
   ```
4. Добавить `import javax.annotation.PreDestroy;`

**Тесты:** `BackgroundWorkerTest` — не должен требовать изменений (тесты не проверяют shutdown).

**Верификация:** `cd orders && mvn -q test` — green.

---

## Task 6: Интеграционная проверка

**Файлы:** — (проверка, не изменение)

**Изменения:**
1. `cd orders && mvn -q test` — все тесты зелёные.
2. `make scenarios TASK=1 WAIT=10` — S1–S5 PASS, уровень «полностью».
3. `make scenarios ONLY=S3,S4 WAIT=5` — проверить сценарии с таймаутами и отказами.

**Rollback:** изменения ограничены логами, конфиг-параметрами и exception-хэндлингом. Бизнес-логика не тронута. Лечить точечно.
