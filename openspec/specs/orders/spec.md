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

While Payment is unavailable, Orders SHALL keep the number of requests it sends to Payment
bounded (both `POST /payments` and `GET /payments` count), and SHALL resume charging after
Payment recovers. The circuit breaker SHALL open on the first network failure, within one
charge timeout (~1 s) of the first blackholed request.

#### Scenario: bounded requests during a blackhole

- Given Payment in a blackhole for a sustained window while a stream of orders arrives
- When Orders processes those orders
- Then the number of requests Orders sends to Payment during the outage stays within the
  scenario budget (open params: ≤ 30 for 50 orders during a 60 s blackhole)

#### Scenario: first network failure opens the breaker early

- Given Payment in a blackhole and orders arriving in a stream
- When the first charge attempt times out (1 s)
- Then the circuit breaker is OPEN within that same second, and orders that have not started
  processing yet send no requests to Payment until the breaker allows a probe
- So the number of orders that reach Payment before the breaker opens is bounded by the
  worker pool size, not by the order arrival rate

#### Scenario: pending orders are charged after recovery

- Given orders left `pending` while Payment was down
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

Orders SHALL NOT call `GET /payments?order_id=` before the first `POST /payments` of an order.
Reconciliation via `GET /payments?order_id=` SHALL be performed only after an indeterminate
charge outcome (`ChargeResult.NETWORK` — timeout or lost response). A charge that was already
executed in a previous attempt SHALL be discovered by the idempotent `POST /payments` itself
(same `Idempotency-Key` and body → `200` for the same charge).

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
  no charge found → release the reservation, `rejected`, open the circuit breaker

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

