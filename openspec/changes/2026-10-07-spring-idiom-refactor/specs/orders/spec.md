# Delta: orders

## ADDED Requirements

### Requirement: Lombok for constructor generation

`OrdersController` and `OrderRepository` SHALL use `@RequiredArgsConstructor` from Lombok
instead of manually written constructors. Lombok is added as a `provided` dependency in `pom.xml`.

#### Scenario: OrdersController uses @RequiredArgsConstructor

- Given `OrdersController` is annotated with `@RequiredArgsConstructor`
- When Spring creates the bean
- Then the generated constructor receives all 4 final dependencies via injection

#### Scenario: OrderRepository still uses manual constructor

- Given `OrderRepository` computes `usingPostgres` in its constructor via `detectPostgres()`
- And `usingPostgres` is not a `final` field
- Then `OrderRepository` uses a manual constructor (not Lombok `@RequiredArgsConstructor`)
  to properly initialize `usingPostgres`

### Requirement: Bean Validation on DTOs

All incoming request DTOs SHALL use Bean Validation annotations (`@NotBlank`, `@NotEmpty`,
`@Min`, `@DecimalMin`) instead of manual null/empty checks in the controller.

#### Scenario: Missing user_id returns 400

- Given `POST /orders` is called with `{"user_id": "", "items": [...]}`
- When the request body is processed
- Then the response is `400 Bad Request` (not `500 Internal Server Error`)

#### Scenario: Empty items list returns 400

- Given `POST /orders` is called with `{"user_id": "u-1", "items": []}`
- When the request body is processed
- Then the response is `400 Bad Request`

#### Scenario: Invalid item quantity returns 400

- Given `POST /orders` is called with `{"user_id": "u-1", "items": [{"sku": "s", "qty": 0, "price_cents": 100}]}`
- When the request body is processed
- Then the response is `400 Bad Request`

#### Scenario: Invalid item price returns 400

- Given `POST /orders` is called with `{"user_id": "u-1", "items": [{"sku": "s", "qty": 1, "price_cents": -1}]}`
- When the request body is processed
- Then the response is `400 Bad Request`

### Requirement: Controller uses @Valid

The `POST /orders` endpoint SHALL use `@Valid` on the `@RequestBody` parameter
to trigger Bean Validation before the handler method executes.

#### Scenario: Valid request passes validation

- Given a valid request body with non-empty `user_id`, non-empty `items`, valid `sku`, `qty >= 1`, `price_cents >= 0`
- When `POST /orders` is called
- Then validation passes and the handler executes normally

### Requirement: ValidationExceptionHandler

A `@RestControllerAdvice` class SHALL exist to handle `MethodArgumentNotValidException`
and return `400 Bad Request` with the same error format as the previous manual validation.

#### Scenario: Validation exception returns 400 with correct error format

- Given a request body fails validation
- When `MethodArgumentNotValidException` is thrown
- Then the response is `400` with body `{"code": "validation_error", "message": "invalid request body"}`

### Requirement: Component annotations for business classes

All business components SHALL be registered in the Spring context via component annotations
(`@Service`, `@Component`) rather than exclusively through `@Bean` factory methods in a
central configuration class. The application context SHALL discover them via component scanning.

#### Scenario: PaymentClient is a Spring-managed service

- Given the application starts with `@SpringBootApplication`
- When the context is initialized
- Then `PaymentClient` is available as a singleton bean in the context
- And it was discovered via `@Service` annotation, not via a `@Bean` method

#### Scenario: OrderProcessor receives dependencies via constructor injection

- Given `OrderProcessor` is annotated with `@Service`
- When the Spring context creates the bean
- Then its dependencies (`InventoryClient`, `PaymentClient`, `OrderRepository`, `PaymentCircuitBreaker`)
  are injected via constructor

### Requirement: Injected ObjectMapper for serialization

All JSON serialization SHALL use the Spring-managed `ObjectMapper` bean (injected via constructor)
rather than locally instantiated `static` instances.

#### Scenario: Controller uses injected ObjectMapper

- Given `OrdersController` has an `ObjectMapper` field
- When building JSON for items
- Then it uses the injected `ObjectMapper` instance
- And no `static ObjectMapper` field exists in the class

#### Scenario: PaymentClient serializes request body via ObjectMapper

- Given `PaymentClient` has an injected `ObjectMapper`
- When building the `POST /payments` request body
- Then the JSON is produced by `ObjectMapper.writeValueAsString()` on a typed object
- And no `String.format` is used for JSON construction

### Requirement: No central AppConfig for bean factory

The application SHALL NOT use a dedicated `@Configuration` class solely for defining `@Bean`
methods for business components. Configuration properties SHALL be injected directly into
components via `@Value` or `@ConfigurationProperties`.

#### Scenario: AppConfig is absent or minimal

- Given the refactored application
- When inspecting `orders.config` package
- Then there is no `@Bean` method for `PaymentClient`, `InventoryClient`, `OrderProcessor`,
  `PaymentCircuitBreaker`, or `BackgroundWorker`
- And `@EnableScheduling` is present (either on the main class or a minimal config)

### Requirement: Behavior invariance

The refactored code SHALL produce identical runtime behavior:
- All S1–S5 scenarios pass with the same pass/fail outcomes.
- HTTP response bodies (JSON) are semantically equivalent (same fields, same values).
- Outgoing HTTP requests to Payment and Inventory carry the same JSON payloads
  (field names and values match the contract, key ordering is irrelevant).
- Background worker thread count, queue behavior, and circuit breaker state machine are unchanged.
- Response times and timeout configurations remain the same.

#### Scenario: Full scenario suite passes after refactor

- Given the refactored code with all annotation changes applied
- When `make scenarios TASK=1` is run
- Then all 5 scenarios (S1–S5) report PASS
- And the level is «полностью»

#### Scenario: Unit tests pass without behavioral change

- Given the existing unit tests in `orders/src/test/`
- When `mvn -q test` is run
- Then all tests pass
- And no test requires modification of its assertions (only constructor/injection setup may change)
