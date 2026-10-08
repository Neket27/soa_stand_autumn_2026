# Tasks: spring-idiom-refactor

- [x] Task 1: PaymentClient — @Service + @Value + ObjectMapper
- [x] Task 2: InventoryClient — @Service + @Value + ObjectMapper
- [x] Task 3: OrderProcessor + PaymentCircuitBreaker — @Service / @Component
- [x] Task 4: BackgroundWorker — удалить @Bean (in AppConfig), оставить @Component
- [x] Task 5: OrdersController + OrderRepository — инъекция ObjectMapper
- [x] Task 6: Удаление AppConfig, перенос @EnableScheduling
- [x] Task 7: Интеграционная проверка (mvn test — green, Java 21)
- [x] Task 8: Lombok в pom.xml + @RequiredArgsConstructor на OrdersController
- [x] Task 9: Bean Validation — аннотации на DTO, @Valid, ValidationExceptionHandler

---

## Task 1: PaymentClient — @Service + @Value + ObjectMapper

**Files:**
- `orders/src/main/java/orders/payment/PaymentClient.java`

**Changes:**
1. Add `@Service` annotation.
2. Replace constructor `(String baseUrl, int connectTimeoutMs, int readTimeoutMs)` with
   `(@Value("${PAYMENT_URL}") String baseUrl, @Value("${PAYMENT.CONNECT_TIMEOUT_MS:200}") int connectTimeoutMs,
    @Value("${PAYMENT.READ_TIMEOUT_MS:700}") int readTimeoutMs, ObjectMapper objectMapper)`.
3. Remove `this.objectMapper = new ObjectMapper()` — use injected instance.
4. Replace `String.format` in `charge()` with a local record + `objectMapper.writeValueAsString()`:
   ```java
   record ChargeRequest(@JsonProperty("order_id") String orderId,
                        @JsonProperty("amount_cents") long amountCents,
                        @JsonProperty("currency") String currency) {}
   ```

**Tests:** `PaymentClientTest` — update constructor calls to pass `new ObjectMapper()`.
**Verification:** `cd orders && mvn -q test` — green.

---

## Task 2: InventoryClient — @Service + @Value + ObjectMapper

**Files:**
- `orders/src/main/java/orders/inventory/InventoryClient.java`

**Changes:**
1. Add `@Service` annotation.
2. Replace constructor `(String baseUrl, int timeoutMs)` with
   `(@Value("${INVENTORY_URL}") String baseUrl, @Value("${INVENTORY.TIMEOUT_MS:200}") int timeoutMs,
    ObjectMapper objectMapper)`.
3. In `reserve()`: replace `String.format` with `ObjectNode` + `readTree(itemsJson)`.

**Tests:** `InventoryClientTest` — update constructor calls.
**Verification:** `cd orders && mvn -q test` — green.

---

## Task 3: OrderProcessor + PaymentCircuitBreaker — @Service / @Component

**Files:**
- `orders/src/main/java/orders/flow/OrderProcessor.java`
- `orders/src/main/java/orders/flow/PaymentCircuitBreaker.java`

**Changes:**
1. `OrderProcessor`: add `@Service`. No other changes (constructor already takes Spring beans).
2. `PaymentCircuitBreaker`: add `@Component`. Replace constructor `(long openForMs)` with
   `(@Value("${CIRCUIT_BREAKER.OPEN_FOR_MS:30000}") long openForMs)`.

**Tests:** `PaymentCircuitBreakerTest`, `OrderProcessorTest` — update constructor calls
(pass the numeric value directly, `@Value` only applies in Spring context).
**Verification:** `cd orders && mvn -q test` — green.

---

## Task 4: BackgroundWorker — удалить @Bean, оставить @Component

**Files:**
- `orders/src/main/java/orders/flow/BackgroundWorker.java`

**Changes:**
1. `@Component` уже есть — не менять.
2. Убедиться, что конструктор корректно принимает Spring beans (уже так).
3. Удалить `@Bean public BackgroundWorker ...` из `AppConfig` (Task 5).

**Tests:** `BackgroundWorkerTest` — не должен требовать изменений.
**Verification:** `cd orders && mvn -q test` — green.

---

## Task 5: OrdersController + OrderRepository — инъекция ObjectMapper

**Files:**
- `orders/src/main/java/orders/api/OrdersController.java`
- `orders/src/main/java/orders/store/OrderRepository.java`

**Changes:**
1. `OrdersController`: удалить `private static final ObjectMapper objectMapper = new ObjectMapper()`.
   Добавить `ObjectMapper` в конструктор как 4-й параметр. Использовать в `buildItemsJson()`.
2. `OrderRepository`: удалить `private static final ObjectMapper objectMapper = new ObjectMapper()`.
   Добавить `ObjectMapper` в конструктор. Использовать в `insertPending()`.

**Tests:** `OrdersControllerTest`, `OrderRepositoryTest` — передать `new ObjectMapper()` в конструктор.
**Verification:** `cd orders && mvn -q test` — green.

---

## Task 6: Удаление AppConfig, перенос @EnableScheduling

**Files:**
- `orders/src/main/java/orders/config/AppConfig.java` — **удалить**
- `orders/src/main/java/orders/OrdersApplication.java`

**Changes:**
1. Удалить файл `AppConfig.java` (все `@Bean`-методы больше не нужны).
2. В `OrdersApplication` добавить `@EnableScheduling`:
   ```java
   @SpringBootApplication
   @EnableScheduling
   public class OrdersApplication { ... }
   ```
3. Удалить пустой пакет `orders/config/` (если больше ничего не содержит).

**Tests:** Все тесты — `mvn -q test`.
**Verification:** `cd orders && mvn -q test` — green.

---

## Task 7: Интеграционная проверка

**Files:** — (проверка, не изменение)

**Changes:**
1. `cd orders && mvn -q test` — **PASS** (Java 21, все тесты зелёные).
2. `make up` — стенд поднимается, `GET /healthz` → 200.
3. `make scenarios TASK=1 WAIT=10` — S1–S5 PASS, уровень «полностью».

**Rollback:** если какой-то сценарий упал — `git diff` покажет только аннотации и инъекцию,
бизнес-логика не тронута. Лечить точечно.

---

## Task 8: Lombok в pom.xml + @RequiredArgsConstructor на OrdersController

**Files:**
- `orders/pom.xml`
- `orders/src/main/java/orders/api/OrdersController.java`

**Changes:**
1. В `pom.xml` добавить `lombok` dependency:
   ```xml
   <dependency>
       <groupId>org.projectlombok</groupId>
       <artifactId>lombok</artifactId>
       <scope>provided</scope>
   </dependency>
   ```
2. `OrdersController`: заменить ручной конструктор (4 параметра) на:
   ```java
   @RequiredArgsConstructor
   public class OrdersController {
       private final OrderRepository orderRepository;
       private final OrderProcessor orderProcessor;
       private final BackgroundWorker backgroundWorker;
       private final ObjectMapper objectMapper;
       // ...
   }
   ```
3. Удалить ручной конструктор-метод.
4. Добавить импорты `lombok.RequiredArgsConstructor`, `javax.validation.Valid` (или `jakarta.validation.Valid` — зависит от версии Spring Boot 3.x).

**Tests:** `OrdersControllerTest` — `@InjectMocks` будет работать с Lombok-generated конструктором.
**Verification:** `cd orders && mvn -q test` — green.

---

## Task 9: Bean Validation — аннотации на DTO, @Valid, ValidationExceptionHandler

**Files:**
- `orders/src/main/java/orders/api/dto/OrderRequest.java`
- `orders/src/main/java/orders/api/dto/Item.java`
- `orders/src/main/java/orders/api/OrdersController.java`
- `orders/src/main/java/orders/api/ValidationExceptionHandler.java` (новый)

**Changes:**
1. `OrderRequest`: добавить Bean Validation на поля:
   ```java
   public record OrderRequest(
           @NotBlank @JsonProperty("user_id") String user_id,
           @NotEmpty @JsonProperty("items") List<Item> items) {}
   ```
2. `Item`: добавить аннотации:
   ```java
   public record Item(
           @NotBlank @JsonProperty("sku") String sku,
           @Min(1) int qty,
           @DecimalMin("0") long priceCents) {}
   ```
3. `OrdersController`:
   - `@PostMapping("/orders") public ResponseEntity<?> createOrder(@Valid @RequestBody OrderRequest request)`
   - Удалить ручной `valid()` метод
   - Удалить ручной вызов `valid(request)` и условную ветку
4. `ValidationExceptionHandler` (новый):
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

**Tests:** Обновить `OrdersControllerTest` — добавить `@Valid` на mockMvc.perform(...),
убедиться что валидация срабатывает.
**Verification:** `cd orders && mvn -q test` — green.

---
