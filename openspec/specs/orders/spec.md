# orders Specification

## Purpose
TBD - created by archiving change resilient-order-payment. Update Purpose after archive.
## Requirements
### Requirement: Idempotent charge

Orders SHALL send a stable `Idempotency-Key` (equal to `order_id`) on every `POST /payments` with an
unchanging body, so that a given order is charged **at most once** regardless of how many times the
request is retried (500, timeout, lost response).

#### Scenario: retry after 500 does not double-charge

- Given a valid order whose first `POST /payments` returns `500` (operation not executed)
- When Orders retries the charge with the same `Idempotency-Key` and the same body
- Then Payment records exactly one charge for the order and the order ends `paid`

#### Scenario: lost response resolves to a single charge

- Given a charge that was executed but whose response was lost (connection closed without a reply)
- When Orders retries with the same `Idempotency-Key` or reconciles via `GET /payments?order_id=`
- Then the order ends `paid` and Payment records exactly one charge for it

### Requirement: One-second response budget

Orders SHALL answer `POST /orders` within 1 second for any Payment behavior (slow, hanging,
unavailable), returning `pending` when the outcome is not yet known. Orders SHALL NOT answer `5xx`
on `POST /orders`; `400` only for an invalid body.

#### Scenario: slow Payment yields an in-budget response

- Given Payment answering `POST /payments` after 2 seconds (operation executed)
- When a `POST /orders` is sent
- Then Orders responds `201` with a valid status (`paid` or `pending`) in under 1 second

#### Scenario: hanging Payment yields pending in budget

- Given Payment not responding at all (connection hangs)
- When a `POST /orders` is sent
- Then Orders responds `201` with status `pending` in under 1 second and never `5xx`

### Requirement: Payment outage protection

While Payment is unavailable, Orders SHALL keep the number of requests it sends to
Payment bounded (both `POST /payments` and `GET /payments` count), and SHALL resume
charging after Payment recovers. The circuit breaker SHALL open on the first network
failure, within one charge timeout (~1 s) of the first blackholed request, **before any
reconciliation request is sent**. Any probe that receives an HTTP response
(200/201/402/500) SHALL close the breaker from OPEN or HALF_OPEN; a probe without a
response SHALL re-open it. If a probe outcome is not reported within the probe window
(default 10 s), a new probe SHALL be released (HALF_OPEN refreshed) so the breaker never
stays in HALF_OPEN without a released probe. The open window (default 15 s) SHALL be
short enough that orders pending at the end of the outage are charged well within the
runner's wait.

#### Scenario: bounded requests during a blackhole

- Given Payment in a blackhole for a sustained window while a stream of orders arrives
- When Orders processes those orders
- Then the number of requests Orders sends to Payment during the outage stays within the
  scenario budget (open params: ≤ 30 for 50 orders during a 60 s blackhole)

#### Scenario: first network failure opens the breaker early

- Given Payment in a blackhole and orders arriving in a stream
- When the first charge attempt times out (1 s)
- Then the circuit breaker is OPEN within that same second, **before the
  reconciliation `GET /payments` of that order is sent**
- So the number of orders that reach Payment before the breaker opens is bounded by the
  worker pool size, not by the order arrival rate

#### Scenario: declined probe closes the breaker

- Given the breaker is HALF_OPEN and the single allowed probe order gets `402` from the charge
- When the probe outcome is reported
- Then the breaker is CLOSED
- And the remaining pending orders are charged without waiting for the next open window

#### Scenario: server error probe closes the breaker

- Given the breaker is HALF_OPEN and the probe order's charge and its retry both get `500`
- When the probe outcome is reported
- Then the breaker is CLOSED (an HTTP response proves Payment is reachable)

#### Scenario: stuck probe triggers a new probe

- Given the breaker is HALF_OPEN and the probe worker stops without reporting an outcome
- When the probe window (default 10 s) elapses
- Then the breaker releases a new probe (HALF_OPEN refreshed)
- And at most one probe is in flight (immediate repeat `allow()` → false)
- And the breaker never stays in HALF_OPEN indefinitely without a released probe

#### Scenario: pending orders are charged after recovery

- Given orders left `pending` while Payment was down (including orders whose charge and
  reconciliation both timed out during the blackhole)
- When Payment recovers and the run's wait elapses
- Then those orders are `paid` (no `pending` left at the end of the run)

### Requirement: Single-flight background order processing

Orders SHALL process each pending order on at most one background worker at a time. A worker
SHALL mark the order in flight atomically when it starts processing and clear the mark when it
finishes; the periodic reaper sweep SHALL NOT enqueue an order that is already in flight. If a
duplicate queue entry for the same order exists, the second pickup SHALL be dropped without
starting another processing round.

#### Scenario: reaper and controller enqueue the same order

- Given a pending order enqueued by the controller and not yet picked up by any worker
- When the reaper sweep sees the order still `pending` in the database and not in flight
- Then the order may be enqueued once more, and when both entries are picked up, only one
  worker processes the order; the second pickup is dropped

#### Scenario: in-flight order is not re-enqueued

- Given a pending order currently being processed by a worker (marked in flight)
- When the reaper sweep runs
- Then the order is not put into the queue again

### Requirement: Minimal payment reconciliation

Orders SHALL NOT call `GET /payments?order_id=` before the first `POST /payments` of an
order. Reconciliation via `GET /payments?order_id=` SHALL be performed only after an
indeterminate charge outcome (`ChargeResult.NETWORK` — timeout or lost response).
Reconciliation SHALL report three states: `CHARGED` (200, non-empty list), `NOT_CHARGED`
(200, empty list) and `UNKNOWN` (error, timeout, non-200). A `NOT_CHARGED` outcome SHALL
release the reservation and reject the order; an `UNKNOWN` outcome SHALL keep the order
`pending` (no rejection, no reservation release) until a later round resolves it. A
charge that was already executed in a previous attempt SHALL be discovered by the
idempotent `POST /payments` itself (same `Idempotency-Key` and body → `200` for the same
charge).

#### Scenario: new order's first payment interaction is the charge

- Given a freshly reserved order with no charge attempts yet
- When a background worker starts processing it
- Then the first request Orders sends to Payment for this order is `POST /payments`
- And no `GET /payments?order_id=` is sent for the order before that POST

#### Scenario: previously executed charge is found via the idempotent retry

- Given a charge that was executed for an order but whose response was lost or timed out
- When the order is processed again (next round or reaper re-enqueue)
- Then `POST /payments` with the same `Idempotency-Key` and body returns the same charge (`200`)
- And the order ends `paid` and Payment records exactly one charge for it

#### Scenario: reconciliation only after an indeterminate outcome

- Given a charge attempt that timed out or whose response was lost (`NETWORK`)
- When the worker decides the order's fate
- Then it calls `GET /payments?order_id=` for the order: charge found → `paid`;
  no charge found → release the reservation, `rejected`, breaker OPEN

#### Scenario: reconciliation failure keeps the order pending

- Given a charge attempt that timed out (`NETWORK`) and the reconciliation `GET /payments`
  also failed (timeout, connection error, non-200)
- When the worker decides the order's fate
- Then the order stays `pending` (no rejection, reservation kept)
- And the circuit breaker is OPEN
- And a later round (after Payment recovers) charges the order via the idempotent
  `POST /payments` and it ends `paid`

### Requirement: Order status consistency

Orders SHALL keep order status consistent with the facts: `paid` implies exactly one charge and a
held reservation; `rejected` implies no charge and no held reservation; any order with a charge
always ends `paid`.

#### Scenario: bank decline leads to a clean rejection

- Given Payment returns `402` (bank declined) for an order
- When Orders completes the order
- Then the order is `rejected`, its reservation is released, and no charge exists for it

#### Scenario: a charged order ends paid

- Given a charge exists for an order (including one whose response was lost)
- When the run's wait elapses
- Then the order status is `paid`

### Requirement: Contract compliance

Orders SHALL expose `GET /healthz`, `POST /orders`, `GET /orders/{id}` exactly per
`contracts/orders.yaml`. `GET /orders/{id}` SHALL return `amount_cents = Σ qty × price_cents`; the
`id` returned by `POST /orders` SHALL be the same `order_id` sent to Payment and Inventory.

#### Scenario: health endpoint

- Given the service is up, connected to the database and ready
- When `GET /healthz` is called
- Then it responds `200`

#### Scenario: order amount matches the charge

- Given an order created from items with `qty` and `price_cents`
- When `GET /orders/{id}` is called after the order is `paid`
- Then `amount_cents` equals `Σ qty × price_cents` and equals the amount charged in Payment

#### Scenario: unknown order is 404

- Given an `id` that Orders has never accepted
- When `GET /orders/{id}` is called
- Then it responds `404`

### Requirement: No interrupt flag corruption on timeout

`PaymentClient.charge()` and `PaymentClient.hasCharges()` SHALL **not** call
`Thread.currentThread().interrupt()` when catching `IOException` (timeout/connection drop).
The interrupt flag is only restored on `InterruptedException` (real thread interrupt).

#### Scenario: IOException after timeout does not kill worker thread

- Given a background worker processing pending orders
- When `PaymentClient.charge()` times out and catches `IOException`
- Then `Thread.currentThread().isInterrupted()` returns `false`
- And the worker can continue to the next iteration without `InterruptedException`

#### Scenario: real InterruptedException preserves flag

- Given a worker thread is interrupted (shutdown or timeout)
- When `PaymentClient.charge()` catches `InterruptedException`
- Then `Thread.currentThread().isInterrupted()` returns `true` (flag preserved)

### Requirement: Cross-dialect SQL for order insert

`OrderRepository.insertPending()` SHALL detect the database type at construction time
and use dialect-appropriate SQL: `CAST(? AS JSONB)` for PostgreSQL, plain `?` for H2.

#### Scenario: insert works on PostgreSQL with JSONB column

- Given the database is PostgreSQL with `items JSONB NOT NULL`
- When `POST /orders` is called with a valid body
- Then the order is inserted successfully (no `PSQLException`)
- And `GET /orders/{id}` returns the correct `amount_cents`

#### Scenario: insert works on H2 with VARCHAR column

- Given the database is H2 with `items VARCHAR`
- When `POST /orders` is called with a valid body
- Then the order is inserted successfully
- And `GET /orders/{id}` returns the correct `amount_cents`

### Requirement: Circuit breaker state updated before reject

When `OrderProcessor` transitions an order to `rejected` due to a network error
(`ChargeResult.NETWORK` with no charge found via reconciliation),
`PaymentCircuitBreaker.onTimeoutOrConnError()` MUST be called **before** the transition returns.

#### Scenario: NETWORK with no charge opens breaker

- Given an order times out on `charge()` and `hasCharges()` returns `false`
- When the order is rejected
- Then the circuit breaker is in `OPEN` state after the method returns


### Requirement: Minimal DataSource configuration

The application SHALL be configured to use the stand-provided
`DATABASE_URL` environment variable (Heroku form
`postgres://user:pass@host:port/db?query`) through a single `DataSource`
bean; `JdbcTemplate` SHALL be provided by Spring Boot auto-configuration
(no explicit `JdbcTemplate` bean in `orders/`). The URI-to-JDBC conversion
(scheme, host:port, path, query string and user/password extraction) SHALL
be a testable static method of `DataSourceConfig`. The data source SHALL
use a short connection timeout (5 s) so that `GET /healthz` fails fast
(503) when the database is unreachable.

#### Scenario: service starts from DATABASE_URL without extra properties

- Given the environment variable
  `DATABASE_URL=postgres://orders:orders@postgres:5432/orders?sslmode=disable`
  and no `spring.datasource.*` properties
- When the application starts
- Then a single `DataSource` bean is created and `JdbcTemplate` is
  auto-configured from it
- And `GET /healthz` responds `200`

#### Scenario: conversion preserves host, port, path and query

- Given `DATABASE_URL=postgres://orders:orders@postgres:5432/orders?sslmode=disable`
- When the conversion is applied
- Then the JDBC URL is
  `jdbc:postgresql://postgres:5432/orders?sslmode=disable`
- And the username is `orders` and the password is `orders`

#### Scenario: conversion without password and without query

- Given `DATABASE_URL=postgres://user@host:5432/db`
- When the conversion is applied
- Then the JDBC URL is `jdbc:postgresql://host:5432/db`
- And the username is `user` and the password is empty

#### Scenario: healthz fails fast on a dead database

- Given the database is unreachable
- When `GET /healthz` is called
- Then it responds `503` in under the data source connection timeout (5 s)

### Requirement: Money values stay integer cents

Order and payment amounts SHALL remain `long` cents end to end (DTOs,
domain, repository, `amount_cents BIGINT` column and the `int64` contract
fields), per the engineering standard «все денежные величины — long
(центы)». `BigDecimal` SHALL NOT be introduced for amount fields.

#### Scenario: amount flows as integer cents

- Given a request with items `{qty: 2, price_cents: 49900}`
- When the order is created and later charged
- Then `amount_cents` is `99800` in `GET /orders/{id}` and in the
  `POST /payments` body, as an integer

### Requirement: State machine logging

`OrderFulfiller.fulfill()` SHALL log at INFO level the outcome of each payment
charge attempt (including the retry after `SERVER_ERROR`), the reconciliation
result after a `NETWORK` outcome, and each order status transition. A failed
`release()` call SHALL be logged at WARN level with the `order_id`.

#### Scenario: Charge outcome is logged

- Given a background worker processes a pending order
- When `paymentClient.charge()` returns `CHARGED`
- Then a log line containing the `order_id` and the charge result is emitted
- And the subsequent `transition` to `paid` is logged

#### Scenario: Reconciliation result is logged

- Given a charge attempt returns `NETWORK` (timeout or lost response)
- When `paymentClient.reconcile()` is called for reconciliation
- Then a log line containing the `order_id` and the reconciliation outcome
  (charge found / not found / unable to verify) is emitted

#### Scenario: Failed release is logged

- Given a charge is declined and the order is rejected
- When `inventoryClient.release()` returns `false`
- Then a WARN log line containing the `order_id` is emitted

#### Scenario: Worker deadline exceeded is logged

- Given a worker's `fulfill()` returns `false` (deadline exhausted)
- When the worker loop finishes the iteration
- Then a WARN log line containing the `order_id` is emitted

### Requirement: HTTP client failure logging

`PaymentClient` and `InventoryClient` SHALL log at WARN level network failures
(timeouts, connection errors) with the `order_id` and the exception type/message.

#### Scenario: Charge timeout is logged

- Given `paymentClient.charge()` encounters an `IOException` (timeout)
- When it returns `ChargeResult.NETWORK`
- Then a WARN log line containing the `order_id` is emitted

#### Scenario: Reserve failure is logged

- Given `inventoryClient.reserve()` encounters an `IOException`
- When it returns `false`
- Then a WARN log line containing the `order_id` is emitted

### Requirement: findById reports data access errors

`OrderRepository.findById()` SHALL return `null` only when the order does not
exist (`EmptyResultDataAccessException`). Any other `DataAccessException`
(DB down, timeout, schema error) SHALL propagate to the caller instead of
being swallowed.

#### Scenario: Missing order returns null

- Given the `orders` table does not contain the given id
- When `findById(id)` is called
- Then `null` is returned and no exception propagates

#### Scenario: DB error propagates

- Given the database is unreachable (or the query fails)
- When `findById(id)` is called
- Then a `DataAccessException` propagates to the caller
- And the background worker logs it via its existing `catch (Exception)` block
- And the HTTP `GET /orders/{id}` responds with an error (not a silent 404)

### Requirement: Tunable timing and worker configuration

The following values SHALL be read from `application.properties` (overridable
via environment), not hardcoded:

| Property | Default | Used by |
|---|---|---|
| `orders.process.budget-ms` | `900` | `CreateOrderService` (reserve timeout) |
| `orders.process.charge-timeout-ms` | `1000` | `OrderFulfiller` (charge, retry, reconciliation) |
| `orders.process.breaker-wait-ms` | `2000` | `OrderFulfiller` sleep while breaker is open |
| `orders.worker.deadline-ms` | `25000` | `BackgroundWorker` max deadline |
| `orders.worker.count` | `8` | `BackgroundWorker` worker threads |

#### Scenario: Defaults preserve current behavior

- Given `application.properties` ships with the default values above
- When the service starts without environment overrides
- Then all S1–S5 scenarios pass with the same outcomes as before the change

#### Scenario: Override via environment

- Given `ORDERS_PROCESS_CHARGE_TIMEOUT_MS=500` is set
- When the service starts
- Then charge attempts use a 500 ms per-request timeout

### Requirement: Graceful shutdown of background workers

`BackgroundWorker` SHALL keep references to its worker threads and interrupt
them on Spring context shutdown (`@PreDestroy`), allowing the JVM to exit
without waiting for blocked `queue.take()` calls.

#### Scenario: Shutdown interrupts workers

- Given the application context is closing
- When the `@PreDestroy` method runs
- Then every worker thread is interrupted
- And `queue.take()` unblocks with `InterruptedException` (flag preserved)

### Requirement: No dead configuration

`PaymentClient` SHALL NOT accept a read-timeout constructor parameter that is
never used. Per-request timeouts are set at the call site
(`HttpRequest.timeout()`), so the dead `PAYMENT.READ_TIMEOUT_MS` property SHALL
be removed from the constructor.

#### Scenario: PaymentClient constructor has no unused parameter

- Given `PaymentClient` is constructed by Spring
- When the context is initialized
- Then the constructor signature is `(String baseUrl, int connectTimeoutMs, ObjectMapper)`
- And the service passes all S1–S5 scenarios

### Requirement: Behavior invariance after hardening

The hardened code SHALL produce identical runtime behavior:
- All S1–S5 scenarios pass, level «полностью».
- HTTP responses byte-equivalent to the contract (status strings lowercase).
- Idempotency-Key, reconciliation, circuit breaker logic unchanged.
- `cd orders && mvn -q test` green (Java 21).

### Requirement: Lombok for constructor generation

Classes whose dependencies are only final fields without `@Value`
(`OrdersController`, `OrderQueryService`) SHALL use Lombok
`@RequiredArgsConstructor` instead of manually written constructors. Lombok
is a `provided` dependency in `pom.xml`. Classes with `@Value` parameters
(`CreateOrderService`, `OrderFulfiller`, `PaymentCircuitBreaker`,
`PaymentClient`, `InventoryClient`, `BackgroundWorker`) and
`OrderRepository` (computes `usingPostgres` in its constructor via
`detectPostgres()`) SHALL keep manual constructors — Spring does not see
`@Value` on Lombok-generated constructor parameters without
`lombok.config` with `copyableAnnotations`.

#### Scenario: OrdersController uses @RequiredArgsConstructor

- Given `OrdersController` is annotated with `@RequiredArgsConstructor`
- When Spring creates the bean
- Then the generated constructor receives all final dependencies via injection

#### Scenario: OrderRepository uses a manual constructor

- Given `OrderRepository` computes `usingPostgres` in its constructor via `detectPostgres()`
- And `usingPostgres` is not a `final` field
- Then `OrderRepository` uses a manual constructor (not Lombok
  `@RequiredArgsConstructor`) to properly initialize `usingPostgres`

### Requirement: Bean Validation on DTOs

All incoming request DTOs SHALL use Bean Validation annotations (`@NotBlank`,
`@NotEmpty`, `@Min`, `@DecimalMin`) instead of manual null/empty checks in
the controller.

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

The `POST /orders` endpoint SHALL use `@Valid` on the `@RequestBody`
parameter to trigger Bean Validation before the handler method executes.

#### Scenario: Valid request passes validation

- Given a valid request body with non-empty `user_id`, non-empty `items`,
  valid `sku`, `qty >= 1`, `price_cents >= 0`
- When `POST /orders` is called
- Then validation passes and the handler executes normally

### Requirement: Validation exception handling

`GlobalExceptionHandler` (`@RestControllerAdvice`) SHALL handle
`MethodArgumentNotValidException` and return `400 Bad Request` with the
contract `Error` body.

#### Scenario: Validation exception returns 400 with the Error body

- Given a request body fails validation
- When `MethodArgumentNotValidException` is thrown
- Then the response is `400` with body
  `{"error": "validation_error", "message": "invalid request body"}`

### Requirement: Component annotations for business classes

All business components SHALL be registered in the Spring context via
component annotations (`@Service`, `@Component`) rather than exclusively
through `@Bean` factory methods in a central configuration class. The
application context SHALL discover them via component scanning.

#### Scenario: PaymentClient is a Spring-managed service

- Given the application starts with `@SpringBootApplication`
- When the context is initialized
- Then `PaymentClient` is available as a singleton bean in the context
- And it was discovered via `@Service` annotation, not via a `@Bean` method

#### Scenario: OrderFulfiller receives dependencies via constructor injection

- Given `OrderFulfiller` is annotated with `@Service`
- When the Spring context creates the bean
- Then its dependencies (`InventoryClient`, `PaymentClient`,
  `OrderRepository`, `PaymentCircuitBreaker`) and `@Value` parameters are
  injected via the manual constructor

### Requirement: Injected ObjectMapper for serialization

All JSON serialization SHALL use the Spring-managed `ObjectMapper` bean
(injected via constructor) rather than locally instantiated `static`
instances.

#### Scenario: Repository serializes items via the injected ObjectMapper

- Given `OrderRepository` has an injected `ObjectMapper`
- When storing an order
- Then the items JSON is produced by `objectMapper.writeValueAsString()`
- And no `static ObjectMapper` field exists in the class

#### Scenario: PaymentClient serializes request body via ObjectMapper

- Given `PaymentClient` has an injected `ObjectMapper`
- When building the `POST /payments` request body
- Then the JSON is produced by `ObjectMapper.writeValueAsString()` on a
  typed object
- And no `String.format` is used for JSON construction

### Requirement: No central AppConfig for bean factory

The application SHALL NOT use a dedicated `@Configuration` class solely for
defining `@Bean` methods for business components. Configuration properties
SHALL be injected directly into components via `@Value`. The only allowed
`@Bean` methods are infrastructure data source setup in `DataSourceConfig`
(`DATABASE_URL` conversion — no properties-only equivalent exists).

#### Scenario: No @Bean methods for business components

- Given the refactored application
- When inspecting all `@Configuration` classes in `orders/`
- Then there is no `@Bean` method for any business component
  (`PaymentClient`, `InventoryClient`, `CreateOrderService`,
  `OrderFulfiller`, `PaymentCircuitBreaker`, `BackgroundWorker`)
- And `@EnableScheduling` is present on the main application class

### Requirement: Main API responses are contract DTOs

The main API endpoints SHALL return contract DTOs directly: `POST /orders`
SHALL return the `OrderCreated` DTO with `201` (for both `pending` and
`rejected` outcomes) without building a `ResponseEntity` in the controller
method; `GET /orders/{id}` SHALL return the `Order` DTO directly. The read
path SHALL be owned by a query use case (`OrderQueryService`): it performs
the lookup, maps the stored row to the `Order` DTO, and throws
`OrderNotFoundException` when the order does not exist. The controller
method for `GET /orders/{id}` SHALL contain no null check, no repository
access and no status branching — it returns the use case result. An unknown
order id SHALL be reported as `404` with an `Error` JSON body
(`{"error":"not_found","message":"order not found"}`) produced by
`GlobalExceptionHandler`, per the 404 response declared in
`contracts/orders.yaml`. `GET /healthz` SHALL remain a `ResponseEntity<Void>`
(200 when the database is reachable, 503 otherwise) — a fixed point of the
refactor.

#### Scenario: created order answered 201 with the contract body

- Given a valid `POST /orders` request whose inventory reserve succeeds
- When the controller responds
- Then the response is `201` with body `{"id": ..., "status": "pending"}`
- And the controller method returns the `OrderCreated` DTO directly
  (no `ResponseEntity` in the method)

#### Scenario: rejected order answered 201 with the contract body

- Given a valid `POST /orders` request whose inventory reserve fails
- When the controller responds
- Then the response is `201` with body `{"id": ..., "status": "rejected"}`

#### Scenario: existing order answered 200 with the contract body

- Given an order id that Orders has accepted
- When `GET /orders/{id}` is called
- Then the response is `200` with body
  `{"id": ..., "status": ..., "amount_cents": ...}`

#### Scenario: unknown order answered 404 with an Error body

- Given an `id` that Orders has never accepted
- When `GET /orders/{id}` is called
- Then the response is `404` with JSON body
  `{"error": "not_found", "message": "order not found"}`
- And the exception is thrown by `OrderQueryService`, not by the controller
- And the status and body are produced by `GlobalExceptionHandler`

#### Scenario: get order controller has no null check

- Given the `OrdersController.getOrder` source
- When it is inspected
- Then it contains no `findById` call, no null check and no
  `ResponseEntity` — it delegates to `OrderQueryService.getOrder` and
  returns its result

#### Scenario: health endpoint keeps ResponseEntity semantics

- Given the service is up and the database is reachable (or not)
- When `GET /healthz` is called
- Then it responds `200` (database reachable) or `503` (not), as before this
  change

### Requirement: Circuit breaker probe determinism

Every probe (the single request allowed while the breaker is HALF_OPEN) SHALL report an
outcome to the breaker: any HTTP response from Payment → `onReachable()`; no response
(timeout / connection error) → `onUnavailable()`. No probe outcome SHALL leave the
breaker in HALF_OPEN. `onReachable()` SHALL close the breaker from OPEN as well as
HALF_OPEN (any response refutes unavailability). HALF_OPEN SHALL additionally be bounded
by a probe window: if no outcome is reported within the window (default 10 s), a new
probe SHALL be released (HALF_OPEN refreshed).

#### Scenario: every probe outcome resolves HALF_OPEN

- Given the breaker is HALF_OPEN
- When the probe order's interaction completes with any outcome
  (charge: CHARGED / DECLINED / SERVER_ERROR / NETWORK; reconciliation: CHARGED /
  NOT_CHARGED / UNKNOWN)
- Then the breaker is CLOSED or OPEN after the outcome is reported — never HALF_OPEN

#### Scenario: no order is lost while HALF_OPEN resolves

- Given the breaker is HALF_OPEN and a probe outcome arrives
- When the breaker transitions (CLOSED or OPEN)
- Then all pending orders continue to be processed: charged immediately (CLOSED) or
  retried after the next open window (OPEN)
- And no order remains `pending` at the end of the run because of the transition

### Requirement: Order status modeled as enum

Order statuses SHALL be represented by `enum OrderStatus { PENDING, PAID, REJECTED }`
in the flow layer. The enum SHALL expose `value()` returning the lowercase DB/API form
(`"pending"`, `"paid"`, `"rejected"`). The HTTP API boundary (DTOs `Order`,
`OrderCreated`) SHALL continue to use lowercase strings — the external contract is
unchanged.

#### Scenario: use case result is typed

- Given `CreateOrderService.createOrder()` is called
- When the inventory reserve succeeds
- Then the result is `CreateResult(id, OrderStatus.PENDING)` (not the string `"PENDING"`)

#### Scenario: Transition uses enum

- Given `OrderRepository.transition(id, OrderStatus.PAID)` is called
- When the SQL is executed
- Then the `status` column is updated to the literal `'paid'` (lowercase)
- And the `WHERE` clause still guards on `status = 'pending'`

#### Scenario: API responses unchanged

- Given a paid order
- When `GET /orders/{id}` is called
- Then the JSON body is `{"id": ..., "status": "paid", "amount_cents": ...}`
  (identical to the pre-change contract)

### Requirement: Single path for order status changes

All order status transitions (`pending → paid`, `pending → rejected`) SHALL be made
only by the flow layer: `CreateOrderService` (rejection on failed reserve) and
`OrderFulfiller` (charge outcomes). `OrdersController` SHALL NOT call
`OrderRepository.transition` directly; it SHALL depend on `CreateOrderService` for
`POST /orders`, and the read path SHALL be owned by the `OrderQueryService` use case.
The order amount rule `amount_cents = Σ qty × price_cents` SHALL live in one place
(derived `OrderRequest.amountCents()`), not in the controller.

#### Scenario: rejected on reserve failure goes through the use case

- Given a `POST /orders` whose inventory reserve fails
- When the controller handles the request
- Then the transition to `rejected` is made by `CreateOrderService`, not by the
  controller
- And the response is `201 {"id": ..., "status": "rejected"}`

#### Scenario: controller has no business logic

- Given the `OrdersController` source
- When it is inspected
- Then it contains no amount calculation, no items JSON serialization and no
  `OrderRepository.transition` call
