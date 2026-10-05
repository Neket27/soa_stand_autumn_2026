# Delta: orders

## ADDED Requirements

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

## MODIFIED Requirements

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
