# Delta: orders

## ADDED Requirements

### Requirement: State machine logging

`OrderProcessor.processUntilTerminal()` SHALL log at INFO level the outcome of each
payment charge attempt (including the retry after `SERVER_ERROR`), the reconciliation
result after a `NETWORK` outcome, and each order status transition. A failed
`release()` call SHALL be logged at WARN level with the `order_id`.

#### Scenario: Charge outcome is logged

- Given a background worker processes a pending order
- When `paymentClient.charge()` returns `CHARGED`
- Then a log line containing the `order_id` and the charge result is emitted
- And the subsequent `transition` to `paid` is logged

#### Scenario: Reconciliation result is logged

- Given a charge attempt returns `NETWORK` (timeout or lost response)
- When `paymentClient.hasCharges()` is called for reconciliation
- Then a log line containing the `order_id` and the reconciliation outcome
  (charge found / not found) is emitted

#### Scenario: Failed release is logged

- Given a charge is declined and the order is rejected
- When `inventoryClient.release()` returns `false`
- Then a WARN log line containing the `order_id` is emitted

#### Scenario: Worker deadline exceeded is logged

- Given a worker's `processUntilTerminal()` returns `false` (deadline exhausted)
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

`OrderRepository.findById()` SHALL return `null` only when the order does not exist
(`EmptyResultDataAccessException`). Any other `DataAccessException` (DB down, timeout,
schema error) SHALL propagate to the caller instead of being swallowed.

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

### Requirement: Order status modeled as enum

Order statuses SHALL be represented by `enum OrderStatus { PENDING, PAID, REJECTED }`
in the domain layer (`OrderRepository`, `OrderProcessor`). The enum SHALL expose
`value()` returning the lowercase DB/API form (`"pending"`, `"paid"`, `"rejected"`).
The HTTP API boundary (DTOs `Order`, `OrderCreated`) SHALL continue to use lowercase
strings — the external contract is unchanged.

#### Scenario: processOnce returns typed result

- Given `OrderProcessor.processOnce()` is called
- When the inventory reserve succeeds
- Then the return type is `ProcessResult.PENDING` (not the string `"PENDING"`)

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

### Requirement: Tunable timing and worker configuration

The following values SHALL be read from `application.properties` (overridable via
environment), not hardcoded:

| Property | Default | Used by |
|---|---|---|
| `orders.process.budget-ms` | `900` | `OrdersController.createOrder` |
| `orders.process.charge-timeout-ms` | `1000` | `OrderProcessor` (charge, retry, reconciliation) |
| `orders.process.breaker-wait-ms` | `2000` | `OrderProcessor` sleep while breaker is open |
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

`BackgroundWorker` SHALL keep references to its worker threads and interrupt them on
Spring context shutdown (`@PreDestroy`), allowing the JVM to exit without waiting for
blocked `queue.take()` calls.

#### Scenario: Shutdown interrupts workers

- Given the application context is closing
- When the `@PreDestroy` method runs
- Then every worker thread is interrupted
- And `queue.take()` unblocks with `InterruptedException` (flag preserved)

### Requirement: No dead configuration

`PaymentClient` SHALL NOT accept a read-timeout constructor parameter that is never
used. Per-request timeouts are set at the call site (`HttpRequest.timeout()`), so the
dead `PAYMENT.READ_TIMEOUT_MS` property SHALL be removed from the constructor.

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
