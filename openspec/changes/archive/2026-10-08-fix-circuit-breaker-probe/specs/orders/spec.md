# Delta: orders

## MODIFIED Requirements

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

## ADDED Requirements

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
