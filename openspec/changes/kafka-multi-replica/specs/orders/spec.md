## ADDED Requirements

### Requirement: Multi-replica charge dispatch without duplicate processing

When the service runs as N replicas, each pending order SHALL be picked up for
charge by exactly one replica at a time (no duplicate concurrent processing across
replicas). Work SHALL be distributed across replicas; the mechanism (in-memory
dispatcher + atomic claim, or Kafka consumer group) SHALL guarantee that a redelivered
or re-claimed order is processed idempotently. The order amount and charge behavior
SHALL be identical to the single-replica case.

#### Scenario: two replicas contend for the same pending order

- Given a pending order and two (or more) running replicas
- When both attempt to pick it up for charge
- Then exactly one replica processes it; the other does not start a second charge round
- And the order ends `paid` or `rejected` with at most one charge

#### Scenario: Kafka consumer group distributes orders across replicas

- Given the Kafka dispatch mode and N replicas in one consumer group
- When a stream of orders is published to `orders.charge` (key = `order_id`)
- Then each order record is consumed by exactly one replica in the group
- And all orders reach a terminal status (`paid`/`rejected`) with no double charges

### Requirement: Global payment outage budget across replicas

While Payment is unavailable, the bound on the number of requests sent to Payment
SHALL be enforced **globally across all N replicas**, not per replica. The circuit
breaker state SHALL be shared (stored outside the JVM); when the breaker is OPEN,
exactly one replica SHALL be allowed to release a HALF_OPEN probe at a time
(global single-flight). This SHALL keep the S4 budget (requests to Payment during a
blackhole) within the scenario limit even when N > 1.

#### Scenario: bounded global requests during a blackhole with N replicas

- Given Payment in a blackhole and N replicas processing a stream of orders
- When the shared breaker opens on the first network failure
- Then the total number of requests all replicas send to Payment during the outage
  stays within the S4 budget (it is not multiplied by N)
- And after recovery the pending orders are charged and end `paid`

#### Scenario: exactly one replica releases the HALF_OPEN probe

- Given the shared breaker is OPEN and the open window elapses on several replicas
- When they race to release a probe
- Then exactly one replica wins the transition and sends the probe; the others wait
- And the probe outcome (any HTTP response) closes the shared breaker for all replicas

### Requirement: Order charge dispatch keeps the one-second response budget

`POST /orders` SHALL keep the one-second response budget in dispatch mode. Publishing
the charge trigger (e.g. to Kafka) in the synchronous path SHALL be best-effort: it
SHALL NOT hold or fail the response. The durable guarantee that an order is eventually
charged SHALL come from the database `pending` row plus the safety-net re-publish, not
from the success of the inline publish.

#### Scenario: slow or unavailable broker does not break the budget

- Given Kafka is slow or briefly unavailable
- When a `POST /orders` is sent
- Then Orders responds `201` (valid status, `pending` allowed) in under 1 second and
  never `5xx`
- And the order is still charged afterwards (via the DB `pending` + safety-net re-publish)

### Requirement: No lost orders across replica crashes and restarts

An order accepted by any replica (persisted `pending`) SHALL NOT be lost if that
replica crashes or is restarted before the order reaches a terminal status. The
durable source of truth for «not yet terminal» SHALL be the database `pending` state,
not an in-memory queue or a message offset. A surviving replica (safety-net reaper)
SHALL re-drive such orders to a terminal status.

#### Scenario: replica dies after accepting an order, before charging it

- Given an order accepted (`pending`) by replica A that then crashes before it is charged
- When the run continues with the remaining replicas
- Then the order is still charged and ends `paid` or `rejected` (not lost, not `pending`
  at the end of the run)
- And there is at most one charge for it

### Requirement: Order domain events published via transactional outbox

On a terminal transition (`pending → paid` or `pending → rejected`), Orders SHALL write
an outbox event in the **same** database transaction as the status transition. A relay
SHALL publish `order.paid` / `order.rejected` to Kafka (key = `order_id`) and mark the
event published. Publishing SHALL be at-least-once; the event SHALL carry the `order_id`
and the terminal status. The event payload SHALL NOT contain secrets or connection
strings.

#### Scenario: terminal transition and its event are atomic

- Given a charge that reaches a terminal status
- When the status transition is committed
- Then an outbox row for `order.paid` (or `order.rejected`) is committed in the same
  transaction (a rolled-back transition leaves no outbox row)

#### Scenario: relay publishes the event exactly enough to be safe

- Given a committed outbox event for a terminal order
- When the relay processes it
- Then the event is published to the events topic and marked published
- And if the relay crashes between publish and mark, re-publishing is allowed
  (at-least-once) and downstream consumers are idempotent

## MODIFIED Requirements

### Requirement: Single-flight background order processing

Orders SHALL process each pending order on at most one background worker at a time,
**across all replicas** (not only within one process). A worker SHALL mark the order in
flight atomically (a per-process in-memory mark is insufficient once N replicas run)
and clear it when it finishes; the periodic reaper sweep SHALL NOT enqueue an order that
is already in flight. If a duplicate entry for the same order exists (in-memory queue or
re-delivery), the second pickup SHALL be dropped without starting another processing
round. The single-flight guarantee SHALL hold whether the transport is the in-memory
dispatcher or the Kafka consumer group.

#### Scenario: reaper and controller enqueue the same order

- Given a pending order enqueued for charge and not yet picked up
- When the safety-net sweep sees the order still `pending` and not in flight
- Then the order may be enqueued once more, and when both entries are picked up, only
  one worker (on exactly one replica) processes the order; the second pickup is dropped

#### Scenario: in-flight order is not re-enqueued or re-claimed

- Given a pending order currently being processed by a worker (marked in flight / claimed)
- When the safety-net sweep runs, possibly on another replica
- Then the order is not enqueued or claimed again
