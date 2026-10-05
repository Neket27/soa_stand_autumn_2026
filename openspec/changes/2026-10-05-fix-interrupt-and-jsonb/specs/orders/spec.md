# Delta: orders

## ADDED Requirements

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
