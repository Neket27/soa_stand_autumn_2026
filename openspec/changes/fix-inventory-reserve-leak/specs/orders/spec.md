## MODIFIED Requirements

### Requirement: Order status consistency

Orders SHALL keep order status consistent with the facts: `paid` implies exactly one charge and a
held reservation; `rejected` implies no charge and **no held reservation, where the release is
confirmed, not merely attempted** (a failed or uncertain release SHALL NOT leave a `rejected`
order with a held reservation — see the «Durable reservation release» requirement); any order
with a charge always ends `paid`.

#### Scenario: bank decline leads to a clean rejection

- Given Payment returns `402` (bank declined) for an order
- When Orders completes the order
- Then the order is `rejected`, its reservation is released, and no charge exists for it

#### Scenario: a charged order ends paid

- Given a charge exists for an order (including one whose response was lost)
- When the run's wait elapses
- Then the order status is `paid`

## ADDED Requirements

### Requirement: Durable reservation release

A timeout or network error on `POST /reservations` or `DELETE /reservations/{order_id}` SHALL be
treated as an **indeterminate** outcome: «no response» is neither «reservation not created» nor
«reservation not released». When a `rejected` order may still hold a reservation (its inline
release failed, or its reserve outcome was indeterminate), Orders SHALL persist this obligation
(a release duty) in the database together with the terminal transition in one atomic update, and
SHALL retry `DELETE /reservations/{order_id}` until a `204` is received. Because `DELETE` is
idempotent per `contracts/inventory.yaml` (`204` also for an order without a reservation),
at-least-once retries SHALL NOT corrupt reservation state. The duty SHALL survive a process
restart (it is read from the database, not from memory). Orders SHALL NOT send a release
`DELETE` on the synchronous `POST /orders` path after an indeterminate reserve; the synchronous
path SHALL stay within the one-second response budget.

#### Scenario: failed release after bank decline is retried until confirmed

- Given Payment returns `402` for an order and the inline `DELETE /reservations/{order_id}`
  fails (timeout or network error)
- When the order is transitioned to `rejected` and the periodic release sweep runs
- Then the reservation is released (a subsequent `DELETE` returns `204`) and the release duty is
  cleared
- And the order remains `rejected` with no held reservation at the end of the run

#### Scenario: lost reserve response leads to a clean rejection

- Given `POST /reservations` returned no response (timeout) and the reservation may have been
  created
- When Orders answers `POST /orders` with `201` / `rejected` within the one-second budget
- Then the order is `rejected` with the release duty recorded, and no extra `DELETE` was sent on
  the synchronous path
- And when the periodic release sweep runs during the runner's wait
- Then any created reservation is released (`204`) and the duty is cleared

#### Scenario: duty survives a process restart

- Given a `rejected` order with a recorded release duty
- When the process restarts (new JVM, same database)
- Then the release sweep finds the duty from the database and completes the release

#### Scenario: no extra requests when the inline release succeeds

- Given Payment returns `402` and the inline `DELETE /reservations/{order_id}` returns `204`
- When the periodic release sweep runs
- Then no further `DELETE` is sent for this order (no release duty recorded)

#### Scenario: timed-out release that actually executed converges

- Given a release `DELETE` timed out on the client but was executed in Inventory
- When the next release sweep retries the `DELETE` for the same order
- Then Inventory answers `204` (idempotent), the duty is cleared, and no duplicate side effect
  occurs
