# Delta: orders

## ADDED Requirements

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
