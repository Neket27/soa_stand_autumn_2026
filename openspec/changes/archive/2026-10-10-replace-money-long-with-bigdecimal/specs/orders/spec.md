## ADDED Requirements

### Requirement: Money values use BigDecimal internally

Order and payment amounts SHALL be represented as `java.math.BigDecimal`
(integer cents, scale 0) end to end inside the Orders service: request and
response DTOs, the domain model (`StoredOrder`), the repository and the
payment client. `BigDecimal` SHALL be the sole monetary type — `long`,
`int`, `double` or `float` SHALL NOT be used for amount fields inside the
service. The wire representation SHALL remain integer cents per
`contracts/orders.yaml` and `contracts/payment.yaml` (`price_cents`,
`amount_cents`, int64): a scale-0 `BigDecimal` serializes to a JSON
integer, so the external contract is unchanged.

#### Scenario: amount flows as integer cents on the wire

- Given a request with items `{sku: "sku-1", qty: 2, price_cents: 49900}`
- When the order is created and later charged
- Then `GET /orders/{id}` returns `amount_cents: 99800` as a JSON integer
- And the `POST /payments` body carries `"amount_cents": 99800` as a JSON
  integer

#### Scenario: amount arithmetic is exact

- Given an order with items `{qty: 3, price_cents: 33}`
- When the order amount is computed
- Then `amount_cents` is exactly `99` (no floating-point rounding)

## REMOVED Requirements

### Requirement: Money values stay integer cents

**Reason**: Replaced by `BigDecimal` (scale-0 cents) for precise arithmetic
and per the engineering standard «все денежные величины — BigDecimal». The
wire stays integer cents — the contract is immutable.
**Migration**: All monetary Java fields changed from `long`/`int` cents to
`BigDecimal` cents (scale 0); JSON field names (`price_cents`,
`amount_cents`) and the `amount_cents BIGINT` column are unchanged.
