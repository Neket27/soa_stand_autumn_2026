# Delta: orders

## ADDED Requirements

### Requirement: Minimal DataSource configuration

The application SHALL be configured to use the stand-provided
`DATABASE_URL` environment variable (Heroku form
`postgres://user:pass@host:port/db?query`) through a single `DataSource`
bean; `JdbcTemplate` SHALL be provided by Spring Boot auto-configuration
(no explicit `JdbcTemplate` bean in `orders/`). The URI-to-JDBC conversion
(scheme, host:port, path, query string and user/password extraction) SHALL
be a testable static method of `DataSourceConfig`. The data source SHALL
use a short connection timeout (5 s) so that `GET /healthz` fails fast
(503) when the database is unreachable.

#### Scenario: service starts from DATABASE_URL without extra properties

- Given the environment variable
  `DATABASE_URL=postgres://orders:orders@postgres:5432/orders?sslmode=disable`
  and no `spring.datasource.*` properties
- When the application starts
- Then a single `DataSource` bean is created and `JdbcTemplate` is
  auto-configured from it
- And `GET /healthz` responds `200`

#### Scenario: conversion preserves host, port, path and query

- Given `DATABASE_URL=postgres://orders:orders@postgres:5432/orders?sslmode=disable`
- When the conversion is applied
- Then the JDBC URL is
  `jdbc:postgresql://postgres:5432/orders?sslmode=disable`
- And the username is `orders` and the password is `orders`

#### Scenario: conversion without password and without query

- Given `DATABASE_URL=postgres://user@host:5432/db`
- When the conversion is applied
- Then the JDBC URL is `jdbc:postgresql://host:5432/db`
- And the username is `user` and the password is empty

#### Scenario: healthz fails fast on a dead database

- Given the database is unreachable
- When `GET /healthz` is called
- Then it responds `503` in under the data source connection timeout (5 s)

### Requirement: Money values stay integer cents

Order and payment amounts SHALL remain `long` cents end to end (DTOs,
domain, repository, `amount_cents BIGINT` column and the `int64` contract
fields), per the engineering standard «все денежные величины — long
(центы)». `BigDecimal` SHALL NOT be introduced for amount fields.

#### Scenario: amount flows as integer cents

- Given a request with items `{qty: 2, price_cents: 49900}`
- When the order is created and later charged
- Then `amount_cents` is `99800` in `GET /orders/{id}` and in the
  `POST /payments` body, as an integer
