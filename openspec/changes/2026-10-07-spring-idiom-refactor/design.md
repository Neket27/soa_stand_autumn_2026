# Design: spring-idiom-refactor

## D1. Lombok

### Добавление в pom.xml

```xml
<dependency>
    <groupId>org.projectlombok</groupId>
    <artifactId>lombok</artifactId>
    <scope>provided</scope>
</dependency>
```

Annotation processor path уже включён в Spring Boot Maven plugin (maven-compiler-plugin
подключает annotationProcessorPaths из зависимости `lombok`). Никаких дополнительных
настроек не требуется.

### `@RequiredArgsConstructor`

`@RequiredArgsConstructor` генерирует конструктор из всех `final`-полей без геттеров/сеттеров.

**OrdersController:** 4 final-поля (`OrderRepository`, `OrderProcessor`, `BackgroundWorker`, `ObjectMapper`)
→ конструктор из 4 параметров, который Spring инъектирует.

**OrderRepository:** 3 final-поля (`JdbcTemplate`, `ObjectMapper`, `usingPostgres`)
— `usingPostgres` не final (вычисляемое значение). После рефакторинга он вычисляется в конструкторе,
поэтому в конструкторе только `JdbcTemplate` и `ObjectMapper`:
```java
@RequiredArgsConstructor
public class OrderRepository {
    private final JdbcTemplate jdbcTemplate;
    private final ObjectMapper objectMapper;
    private final boolean usingPostgres;
    // ...
}
```

Но Lombok генерирует конструктор только из `final` полей. `usingPostgres` не final → Lombok генерирует
2-параметный конструктор `(JdbcTemplate, ObjectMapper)`, а `usingPostgres` инициализируется внутри.
Это **ломает логику** — `usingPostgres` вычислялся через `detectPostgres()` в ручном конструкторе.

**Решение:** оставить `OrdersController` на `@RequiredArgsConstructor` (все поля final).
Для `OrderRepository` — оставить ручной конструктор (логика вычисления `usingPostgres` в конструкторе).

### `@AllArgsConstructor`

Не используется — только `@RequiredArgsConstructor`.

## D2. Bean Validation

### Аннотации на DTO

```java
public record OrderRequest(
        @NotBlank @JsonProperty("user_id") String user_id,
        @NotEmpty @JsonProperty("items") List<Item> items) {}

public record Item(
        @NotBlank @JsonProperty("sku") String sku,
        @Min(1) int qty,
        @DecimalMin("0") long priceCents) {}
```

`@NotBlank` — строка не null и не пустая (trim).
`@NotEmpty` — коллекция не null и не пуста.
`@Min(1)` — значение ≥ 1.
`@DecimalMin("0")` — значение ≥ 0 (long поддерживает).

### Исключение: `@DecimalMin` для long

`@DecimalMin` работает и для `long` — Jackson мапит `@DecimalMin` на `BigDecimal`-валидацию,
которая принимает long. Альтернатива — `@PositiveOrZero` (тоже работает для long).
Выбираю `@DecimalMin("0")` — он более очевиден для денежных величин.

### `@Valid` в контроллере

```java
@PostMapping("/orders")
public ResponseEntity<?> createOrder(@Valid @RequestBody OrderRequest request) {
```

`@Valid` запускает валидацию вложенных объектов. Для `List<Item>` — валидация каждого элемента
в списке (nested validation).

### `@RestControllerAdvice`

```java
@RestControllerAdvice
public class ValidationExceptionHandler {

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<Error> handleValidation(MethodArgumentNotValidException ex) {
        return ResponseEntity.badRequest().body(
                new Error("validation_error", "invalid request body"));
    }
}
```

Возвращает тот же формат `400`, что и ручной метод `valid()`. Тело ошибки — точное то же:
`{"code": "validation_error", "message": "invalid request body"}`. Раннер принимает только
`400` на некорректное тело.

### Зависимости

Требуется `jakarta.validation:validation-api` и реализация (Hibernate Validator).
Spring Boot parent транзитивно включает оба.

## D3. Компонентные аннотации вместо @Bean-фабрики

### Проблема

Все бизнес-компоненты создаются в `AppConfig` через `@Bean`-методы:
```java
@Bean public PaymentClient paymentClient() { return new PaymentClient(paymentUrl, ...); }
@Bean public InventoryClient inventoryClient() { return new InventoryClient(inventoryUrl, ...); }
@Bean public PaymentCircuitBreaker paymentCircuitBreaker() { ... }
@Bean public OrderProcessor orderProcessor(...) { ... }
@Bean public BackgroundWorker backgroundWorker(...) { ... }
```

`BackgroundWorker` при этом **уже** имеет `@Component` — потенциальный конфликт bean definitions.
Классы без аннотаций (`PaymentClient`, `InventoryClient`, `OrderProcessor`, `PaymentCircuitBreaker`)
выглядят как «чужие» в Spring-приложении.

### Решение

Перенести регистрацию на аннотации классов, конфигурацию — на `@Value` в конструкторах:

```java
@Service
public class PaymentClient {
    public PaymentClient(
            @Value("${PAYMENT_URL}") String baseUrl,
            @Value("${PAYMENT.CONNECT_TIMEOUT_MS:200}") int connectTimeoutMs,
            @Value("${PAYMENT.READ_TIMEOUT_MS:700}") int readTimeoutMs,
            ObjectMapper objectMapper) { ... }
}
```

```java
@Service
public class InventoryClient {
    public InventoryClient(
            @Value("${INVENTORY_URL}") String baseUrl,
            @Value("${INVENTORY.TIMEOUT_MS:200}") int timeoutMs) { ... }
}
```

```java
@Service
public class OrderProcessor {
    public OrderProcessor(InventoryClient, PaymentClient, OrderRepository, PaymentCircuitBreaker) { ... }
}
```

```java
@Component
public class PaymentCircuitBreaker {
    public PaymentCircuitBreaker(@Value("${CIRCUIT_BREAKER.OPEN_FOR_MS:30000}") long openForMs) { ... }
}
```

`BackgroundWorker` — остаётся `@Component`, `@Bean`-метод удаляется из `AppConfig`.

**Почему не `@ConfigurationProperties`:** конфигурация — 4–5 скалярных значений,
отдельный `@ConfigurationProperties`-класс избыточен. `@Value` в конструкторе — стандартная
идиомата для такого объёма.

**Почему `@Service` а не `@Component` для клиентов/процессора:** `@Service` — семантическая
подсказка «бизнес-логика / внешний вызов», `@Component` — для инфраструктурных (`CircuitBreaker`,
`BackgroundWorker`). Разграничение не строгое, но читается лучше.

## D2. Инъекция ObjectMapper

### Проблема

```java
// OrdersController
private static final ObjectMapper objectMapper = new ObjectMapper();

// OrderRepository
private static final ObjectMapper objectMapper = new ObjectMapper();

// PaymentClient
this.objectMapper = new ObjectMapper();
```

Локальные инстансы не наследуют Spring-настройки (например, `spring.jackson.*` из
`application.properties`), дублируют конфигурацию и не подменяются в тестах без reflection.

### Решение

Инъекция через конструктор:
```java
@RestController
public class OrdersController {
    private final ObjectMapper objectMapper;
    public OrdersController(OrderRepository r, OrderProcessor p, BackgroundWorker w,
                            ObjectMapper objectMapper) { ... }
}
```

Spring Boot auto-configure уже создаёт `ObjectMapper` bean — достаточно принять его в конструкторе.

## D3. JSON-сериализация исходящих тел

### Проблема

```java
// PaymentClient.charge()
String body = String.format(
    "{\"order_id\":\"%s\",\"amount_cents\":%d,\"currency\":\"RUB\"}", orderId, amountCents);

// InventoryClient.reserve()
String body = String.format("{\"order_id\":\"%s\",\"items\":%s}", orderId, itemsJson);
```

`String.format` — хрупко (эскейпинг, если orderId содержит спецсимволы), не типобезопасно,
не использует уже имеющийся `ObjectMapper`.

### Решение

Маленькие record-обёртки + `ObjectMapper`:

```java
record ChargeRequest(String order_id, long amount_cents, String currency) {}

String body = objectMapper.writeValueAsString(
    new ChargeRequest(orderId, amountCents, "RUB"));
```

Для `InventoryClient.reserve()` — `itemsJson` уже сериализован вызывающим,
поэтому используем `ObjectNode`:

```java
ObjectNode root = objectMapper.createObjectNode();
root.put("order_id", orderId);
root.set("items", objectMapper.readTree(itemsJson));
String body = objectMapper.writeValueAsString(root);
```

Или (чистее) — менять сигнатуру `reserve()` на приём `List<Item>` и сериализовать целиком:
```java
record ReserveRequest(String order_id, List<Item> items) {}
```

Выбираем второй вариант — меньше магии с `readTree`, и `OrderProcessor` передаёт уже
разобранный `List<Item>` (сейчас он хранит `itemsJson` строку, но может передать список).

**Компромисс:** чтобы не менять `OrderProcessor` и `OrderRepository` (они работают со строкой
`itemsJson` для хранения в БД), `InventoryClient.reserve()` принимает `String itemsJson`
и использует `ObjectNode` + `readTree`. Это минимальное изменение при сохранении контракта.

## D4. Минимизация AppConfig

### После рефакторинга

```java
@Configuration
@EnableScheduling
public class AppConfig {
    // Пустой — только маркер для @EnableScheduling.
    // Альтернатива: перенести @EnableScheduling на @SpringBootApplication.
}
```

Или (чистее) — убрать `AppConfig` целиком и добавить `@EnableScheduling` на `OrdersApplication`:
```java
@SpringBootApplication
@EnableScheduling
public class OrdersApplication { ... }
```

Выбираем второй вариант — меньше классов, `@EnableScheduling` логично на main-классе.

## D5. Влияние на инварианты

| Инвариант | Влияние |
|-----------|---------|
| Бюджет 1 с | Не меняется: тайм-ауты конфигурируются теми же `@Value`-параметрами. |
| Не больше одного списания | Не меняется: Idempotency-Key и логика `OrderProcessor` не трогаются. |
| Деньги → paid | Не меняется: `processUntilTerminal()` не модифицируется. |
| Rejected чистый | Не меняется: `release()` вызывается в тех же местах. |
| Не перегружать Payment | Не меняется: `PaymentCircuitBreaker` — та же логика, только с аннотацией. |
| Контракт HTTP | Не меняется: JSON-тела семантически идентичны (те же поля, те же значения). |

## D6. Риски и mitigations

| Риск | Митигция |
|------|----------|
| Bean definition conflict (BackgroundWorker: @Component + @Bean) | Удаляем `@Bean`, оставляем только `@Component`. |
| `@Value` не подставляется при прямом `new` в тестах | Тесты уже используют `new` — не трогаем. Spring-инъекция нужна только для runtime. |
| Порядок сериализации JSON-полей изменился | Контракт — по именам полей, порядок не значим. Mock-и парсят JSON. |
| `ObjectMapper` из Spring может иметь `FAIL_ON_UNKNOWN_PROPERTIES` | Spring Boot default — `false`. Не влияет. |
