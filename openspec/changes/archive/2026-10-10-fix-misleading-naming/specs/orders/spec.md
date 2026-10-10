## MODIFIED Requirements

### Requirement: State machine logging

`OrderCharger.charge()` SHALL log at INFO level the outcome of each payment
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

- Given a worker's `charge()` returns `false` (time budget exhausted)
- When the worker loop finishes the iteration
- Then a WARN log line containing the `order_id` is emitted

### Requirement: Single path for order status changes

All order status transitions (`pending → paid`, `pending → rejected`) SHALL be made
only by the flow layer: `CreateOrderService` (rejection on failed reserve) and
`OrderCharger` (charge outcomes). `OrdersController` SHALL NOT call
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

### Requirement: Validation exception handling

`GlobalExceptionHandler` (`@RestControllerAdvice`) SHALL handle
`MethodArgumentNotValidException` and return `400 Bad Request` with the
contract error body produced by the `ApiError` record (JSON shape unchanged:
`{"error": ..., "message": ...}`).

#### Scenario: Validation exception returns 400 with the ApiError body

- Given a request body fails validation
- When `MethodArgumentNotValidException` is thrown
- Then the response is `400` with body
  `{"error": "validation_error", "message": "invalid request body"}`

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
order id SHALL be reported as `404` with an `ApiError` JSON body
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

#### Scenario: unknown order answered 404 with an ApiError body

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
