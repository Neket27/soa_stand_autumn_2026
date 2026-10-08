# Tasks: fix-circuit-breaker-probe

Порядок: Task 1 → Task 2 → Task 3 → Task 4. Task 1 и 2 компиляционно связаны
(переименование API + новая ветка NETWORK), но логически независимы; делать по порядку.
Сборка/тесты: `cd orders && JAVA_HOME=~/.jdks/temurin-21.0.12.1 mvn -q test`.

- [x] Task 1: PaymentCircuitBreaker — onReachable/onUnavailable + watchdog HALF_OPEN
- [x] Task 2: PaymentClient.reconcile 3 состояния + ветка NETWORK (до-GET открытие, UNKNOWN → pending)
- [x] Task 3: Доклад исходов 402/500 + окно 15 с в application.properties
- [x] Task 4: Интеграционная проверка (mvn test + make scenarios TASK=1)

---

## Task 1: PaymentCircuitBreaker — onReachable/onUnavailable + watchdog HALF_OPEN

**Files:**
- `orders/src/main/java/orders/flow/PaymentCircuitBreaker.java`
- `orders/src/main/java/orders/flow/OrderProcessor.java` (только переименование вызовов)
- `orders/src/test/java/orders/flow/PaymentCircuitBreakerTest.java`

**Changes:**
1. Переименовать `onSuccess()` → `onReachable()`, `onTimeoutOrConnError()` →
   `onUnavailable()`. Логика переходов пока прежняя (HALF_OPEN → CLOSED / OPEN).
2. Добавить поле `long halfOpenSince`. При переходе OPEN → HALF_OPEN в `allow()` —
   фиксировать `halfOpenSince = now`.
3. Watchdog: в `allow()` при `state == HALF_OPEN` — если `now - halfOpenSince >
   probeWindowMs`, то `state = OPEN`, `openSince = halfOpenSince`, `return false`.
   Аналогично в `getState()` (отчётное состояние).
4. Конструктор: `(@Value("${CIRCUIT_BREAKER.OPEN_FOR_MS:15000}") long openForMs,
   @Value("${CIRCUIT_BREAKER.PROBE_WINDOW_MS:10000}") long probeWindowMs)`.
5. `OrderProcessor` — заменить 4 вызова на новые имена (логика веток не меняется,
   это Task 2/3).

**Tests:** `PaymentCircuitBreakerTest` (обновить существующие под новые имена + новые):
- `halfOpen_onReachable_closes` — OPEN → (окно) → HALF_OPEN → `onReachable()` → CLOSED.
- `halfOpen_onUnavailable_reopens` — HALF_OPEN → `onUnavailable()` → OPEN,
  `openSince` обновлён.
- `stuckProbe_watchdogReopens` — HALF_OPEN, `sleep(probeWindowMs + ε)` → `allow()`
  → `false` и `getState() == OPEN` (тест — с малыми `openForMs`/`probeWindowMs`,
  реальный sleep, стиль как в текущих тестах).

**Verification:** `mvn -q test` — green (поведение неизменно, вызовы переименованы).
`make scenarios TASK=1 WAIT=5` — 5/5 PASS (regression-проверка переименования).

---

## Task 2: PaymentClient.reconcile 3 состояния + ветка NETWORK

**Files:**
- `orders/src/main/java/orders/payment/PaymentClient.java`
- `orders/src/main/java/orders/flow/OrderProcessor.java` (ветка NETWORK)
- `orders/src/test/java/orders/payment/PaymentClientTest.java`
- `orders/src/test/java/orders/flow/OrderProcessorTest.java`

**Changes:**
1. `PaymentClient`:
   - Новый `public enum ReconciliationResult { CHARGED, NOT_CHARGED, UNKNOWN }`.
   - Заменить `hasCharges(String, Duration): boolean` на
     `reconcile(String, Duration): ReconciliationResult`:
     200 + `isArray() && size() > 0` → `CHARGED`; 200 + пустой массив → `NOT_CHARGED`;
     не-200, `IOException`, `InterruptedException` (с восстановлением флага),
     `Exception` при разборе → `UNKNOWN`.
2. `OrderProcessor`, ветка NETWORK — новый порядок (design D2/D3):
   ```java
   case NETWORK:
       if (!decideAfterNetwork(order)) break;   // break из switch: цикл продолжается
       return true;                              // decided (paid/rejected)
   ```
   с приватным хелпером:
   ```java
   private boolean decideAfterNetwork(OrderRepository.Order order) {
       circuitBreaker.onUnavailable();           // ДО reconcile (D2)
       ReconciliationResult r = paymentClient.reconcile(order.id(), chargeTimeout);
       log.info("order={} reconciliation={}", order.id(), r);
       switch (r) {
           case CHARGED:
               circuitBreaker.onReachable();
               return transitionToPaid(order.id());
           case NOT_CHARGED:
               if (!release(order.id())) log.warn("order={} release failed", order.id());
               return transitionToRejected(order.id());
           case UNKNOWN:
               return false;                     // остаётся pending (D3)
       }
   }
   ```
   `transitionToPaid`/`transitionToRejected` — вынос текущих `transition(...) > 0`
   проверок (включая повторный `findById` при конкурентном переходе) в приватные методы.

**Tests:**
- `PaymentClientTest`: `reconcile_charged` (существующий `test-charged`),
  `reconcile_empty` (существующий `test-empty` → `NOT_CHARGED`),
  `reconcile_unknown_onError` — запрос к закрытому порту (connect timeout) → `UNKNOWN`;
  `reconcile_unknown_on500` — маркерный order_id с ответом 500 → `UNKNOWN`.
- `OrderProcessorTest`: обновить существующие NETWORK-тесты (`hasCharges` →
  `reconcile`), новые:
  - `processUntilTerminal_networkUnknown_staysPending` — charge NETWORK + reconcile
    UNKNOWN: нет `transition`, нет `release`, брейкер OPEN, `processUntilTerminal`
    с малым дедлайном → `false` (заказ pending, reaper подхватит).
  - `processUntilTerminal_networkNotCharged_rejects` — reconcile NOT_CHARGED →
    release + transition rejected + брейкер OPEN.
  - `processUntilTerminal_networkCharged_pays` — reconcile CHARGED → paid + брейкер CLOSED.
  - `breakerOpenedBeforeReconcile` — верификация порядка вызовов:
    `InOrder(circuitBreaker, paymentClient)`: `onUnavailable` строго до `reconcile`.
    (Брейкер для InOrder — spy, или фиксированный OPEN-счётчик вызовов; выбрать
    минимальный способ, читаемый в тесте.)

**Verification:** `mvn -q test` — green.
`make scenarios ONLY=S4,S5 WAIT=5` — S4: во время отказа ≤ 30 запросов, после
восстановления все `paid`, в конце нет `pending` (5 заказов блэк-хола теперь `paid`);
S5: 20 `paid`, двойных списаний нет.

---

## Task 3: Доклад исходов 402/500 + окно 15 с

**Files:**
- `orders/src/main/java/orders/flow/OrderProcessor.java`
- `orders/src/main/resources/application.properties`
- `orders/src/test/java/orders/flow/OrderProcessorTest.java`

**Changes:**
1. Ветка `DECLINED`: первым действием — `circuitBreaker.onReachable()` (ответ получен →
   Payment доступен).
2. Ветка `SERVER_ERROR` (после инлайн-ретрая):
   - retry `CHARGED` → `onReachable()` (уже есть, через текущий onSuccess);
   - retry `DECLINED` → `onReachable()`;
   - retry `SERVER_ERROR` → `onReachable()` (ответ есть — доступно), `break` (цикл);
   - retry `NETWORK` → `if (!decideAfterNetwork(order)) break; return true;`
     (общий путь с основной веткой NETWORK).
3. `application.properties`:
   ```properties
   CIRCUIT_BREAKER.OPEN_FOR_MS=15000
   CIRCUIT_BREAKER.PROBE_WINDOW_MS=10000
   ```
   (сейчас `OPEN_FOR_MS` задан только дефолтом в `@Value`; сделать явным.)

**Tests:** `OrderProcessorTest`:
- `processUntilTerminal_declined_closesBreaker` — до теста брейкер довести до HALF_OPEN
  (OPEN + sleep до окна), charge → DECLINED → после возврата `getState() == CLOSED`.
- `processUntilTerminal_serverErrorTwice_closesBreaker` — HALF_OPEN, 500, 500 →
  CLOSED после итерации.
- `processUntilTerminal_serverErrorThenNetwork_reconciles` — 500, затем NETWORK →
  вызван `reconcile` ровно один раз.

**Verification:** `mvn -q test` — green.
`make scenarios ONLY=S4 WAIT=5` — PASS; в журнале фактов запросов к Payment во время
отказа ≈ 22 (таблица design D4), ≤ 30.

---

## Task 4: Интеграционная проверка

**Files:** — (проверка, не изменение)

**Changes:**
1. `cd orders && JAVA_HOME=~/.jdks/temurin-21.0.12.1 mvn -q test` — все тесты зелёные.
2. `make scenarios TASK=1 WAIT=10` — S1–S5 `PASS`, уровень «полностью»:
   - ни одного заказа с двумя списаниями;
   - S4: запросы во время отказа ≤ 30, «заказы после восстановления оплачены»,
     в конце нет `pending`;
   - S5: все заказы с деньгами `paid`.
3. Зафиксировать в `results/task_1.txt` числа S4 (запросы во время отказа) для
   описания PR.

**Rollback:** изменения изолированы в брейкере, клиенте Payment и ветках
`OrderProcessor`; `git revert` по задаче. Поведение до change — в коммите
`a9f934d` (S4: 10 запросов в блэк-холе).
