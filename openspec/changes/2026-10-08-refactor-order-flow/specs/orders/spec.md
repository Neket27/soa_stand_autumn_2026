# Delta: orders

## ADDED Requirements

### Requirement: Order status modeled as enum

Order statuses SHALL be represented by `enum OrderStatus { PENDING, PAID, REJECTED }`
in the flow layer. The enum SHALL expose `value()` returning the lowercase DB/API form
(`"pending"`, `"paid"`, `"rejected"`). The HTTP API boundary (DTOs `Order`,
`OrderCreated`) SHALL continue to use lowercase strings — the external contract is
unchanged.

(Требование перенесено из дельты `2026-10-07-harden-observability`: там оно не попало
в tasks и не реализовано; при архивации `harden-observability` исключить из его дельты.)

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
`POST /orders` (and `OrderRepository.findById` for reads only). The order amount rule
`amount_cents = Σ qty × price_cents` SHALL live in one place (derived
`OrderRequest.amountCents()`), not in the controller.

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

### Requirement: API and behavior invariance after the flow refactor

The refactor SHALL NOT change runtime behavior or the external contract:

- JSON request/response bodies byte-identical (`user_id`, `amount_cents`, lowercase
  status strings) per `contracts/orders.yaml`.
- The charge state machine (idempotent charge, POST-first, three-state reconciliation,
  breaker semantics from `2026-10-08-fix-circuit-breaker-probe`) is preserved verbatim
  in `OrderFulfiller`.
- All S1–S5 scenarios pass with level «полностью»; no double charge; no `pending` at
  the end of the run.
- `cd orders && mvn -q test` green (Java 21).

#### Scenario: scenarios pass after the refactor

- Given the refactored code (CreateOrderService, OrderFulfiller, enum statuses)
- When `make scenarios TASK=1` is run
- Then S1–S5 are `PASS`, level «полностью»
- And the runner's «ответы Orders по контракту» check passes (contract conformance)

#### Scenario: regression shield moved with the logic

- Given the charge state machine tests (no reconciliation before NETWORK, one charge
  per order, breaker probe semantics, deadline behavior)
- When the logic moves from `OrderProcessor` to `OrderFulfiller`
- Then all those tests pass against `OrderFulfiller` with unchanged assertions
