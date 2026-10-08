# Tasks: fix-interrupt-and-jsonb

## Task 1: Fix interrupt flag in PaymentClient

**Files:**
- `orders/src/main/java/orders/payment/PaymentClient.java`

**Changes:**
1. In `charge()` catch block for `java.io.IOException` — remove `Thread.currentThread().interrupt()`.
2. In `hasCharges()` catch block for `InterruptedException` — keep `Thread.currentThread().interrupt()`.
3. In `hasCharges()` catch block for `Exception` — do NOT set interrupt flag.

**Tests:** `mvn test` — all 47 tests pass.

**Verification:** `make scenarios TASK=1 WAIT=10` — S1–S5 PASS.

## Task 2: Cross-dialect SQL in OrderRepository

**Files:**
- `orders/src/main/java/orders/store/OrderRepository.java`

**Changes:**
1. Add constructor injection via `JdbcTemplate` only (Spring auto-wires `DataSource` from it).
2. Add `detectPostgres(DataSource)` method — checks `connection.getMetaData().getDatabaseProductName()`.
3. In `insertPending()` — branch on `usingPostgres`:
   - PostgreSQL: `CAST(? AS JSONB)`
   - H2: plain `?`

**Tests:** `mvn test` — `OrderRepositoryTest` passes with H2.

**Verification:** `make scenarios TASK=1 WAIT=10` — all `POST /orders` return 201 (not 500).

## Task 3: Circuit breaker state before reject in OrderProcessor

**Files:**
- `orders/src/main/java/orders/flow/OrderProcessor.java`

**Changes:**
1. In `processUntilTerminal()` `case NETWORK` — move `circuitBreaker.onTimeoutOrConnError()` before `release()` and `transition()` calls.

**Tests:** `OrderProcessorTest.processUntilTerminal_networkNoHasCharges_opensBreaker` — asserts `State.OPEN`.

**Verification:** S4 — breaker opens during blackhole, requests stay within budget.
