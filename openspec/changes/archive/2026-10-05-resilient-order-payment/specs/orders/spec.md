# Delta: orders

## ADDED Requirements

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

While Payment is unavailable, Orders SHALL keep the number of requests it sends to Payment bounded,
and SHALL resume charging after Payment recovers.

#### Scenario: bounded requests during a blackhole

- Given Payment in a blackhole for a sustained window while a stream of orders arrives
- When Orders processes those orders
- Then the number of requests Orders sends to Payment during the outage stays within the scenario
  budget (open params: ≤ 30 for 50 orders during a 60 s blackhole)

#### Scenario: pending orders are charged after recovery

- Given orders left `pending` while Payment was down
- When Payment recovers and the run's wait elapses
- Then those orders are `paid` (no `pending` left at the end of the run)

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
