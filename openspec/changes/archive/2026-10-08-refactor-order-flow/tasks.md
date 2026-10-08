# Tasks: refactor-order-flow

Зависимость: **сначала** реализовать `2026-10-08-fix-circuit-breaker-probe`
(`OrderFulfiller` пишется сразу на новом API брейкера: `onReachable`/`onUnavailable`,
`ReconciliationResult`).
Сборка/тесты: `cd orders && JAVA_HOME=~/.jdks/temurin-21.0.12.1 mvn -q test`.
Каждая задача оставляет компилируемое состояние с зелёными тестами.

- [x] Task 1: enum OrderStatus + transition по enum
- [x] Task 2: OrderFulfiller (вынос charge-машины из OrderProcessor)
- [x] Task 3: CreateOrderService + типизированный InventoryClient + тонкий контроллер
- [x] Task 4: Имена DTO + stale-стаб
- [x] Task 5: GlobalExceptionHandler + healthz с проверкой БД
- [x] Task 6: Интеграционная проверка (mvn test + make scenarios TASK=1)

---

## Task 1: enum OrderStatus + transition по enum

**Files:**
- Новый: `orders/src/main/java/orders/flow/OrderStatus.java`
- `orders/src/main/java/orders/store/OrderRepository.java`
- `orders/src/main/java/orders/flow/OrderProcessor.java` (вызовы transition)
- `orders/src/main/java/orders/api/OrdersController.java` (вызов transition)
- Тесты: `OrderRepositoryTest`, `OrderProcessorTest`, `OrdersControllerTest`

**Changes:**
1. `enum OrderStatus { PENDING("pending"), PAID("paid"), REJECTED("rejected") }` с
   `value()` (design D3).
2. `OrderRepository.transition(String id, OrderStatus to)`: параметр enum,
   `SET status = ? ... AND status = 'pending'` через `to.value()`.
3. Все текущие вызовы `transition(id, "paid"|"rejected")` — на enum.
4. Сравнения статусов в `OrderFulfiller`-будущем (`"paid".equals(order.status())`) —
   на `OrderStatus` (record `OrderRepository.Order.status()` пока остаётся `String` —
   сравнение через `OrderStatus.PAID.value().equals(...)` или helper
   `OrderStatus.fromValue(String)`; выбрать одно, зафиксировать в коде).

**Tests:**
- `OrderRepositoryTest`: transition по enum обновить; новый тест
  `transition_usesLowercaseValue` — после `transition(id, PAID)` в БД лежит `'paid'`
  (H2-схема: items VARCHAR, статус TEXT — проверка `SELECT status`).
- `OrderProcessorTest`/`OrdersControllerTest`: обновить стабы/утверждения под enum.

**Verification:** `mvn -q test` — green.
`make scenarios TASK=1 WAIT=5` — 5/5 PASS (поведение неизменно).

---

## Task 2: OrderFulfiller (вынос charge-машины)

**Files:**
- Новый: `orders/src/main/java/orders/flow/OrderFulfiller.java`
- `orders/src/main/java/orders/flow/OrderProcessor.java` (останется `processOnce`
  до Task 3, затем файл удалится)
- `orders/src/main/java/orders/flow/BackgroundWorker.java` (вызов fulfill)
- Тесты: `OrderProcessorTest` → `OrderFulfillerTest` (перенос)

**Changes:**
1. `OrderFulfiller` (`@Service`): метод `fulfill(String orderId, Duration maxDeadline):
   boolean` — текущее тело `processUntilTerminal` (включая `decideAfterNetwork`,
   `transitionToPaid`/`transitionToRejected`, `release` из
   fix-circuit-breaker-probe). Явный конструктор (есть `@Value` — design D5):
   `PaymentClient`, `InventoryClient`, `OrderRepository`, `PaymentCircuitBreaker`
   + `chargeTimeoutMs`, `breakerWaitMs` — как сейчас в `OrderProcessor`.
2. `BackgroundWorker.workerLoop`: `orderProcessor.processUntilTerminal(orderId,
   itemsJson, deadline)` → `orderFulfiller.fulfill(orderId, deadline)`; параметр
   `itemsJson` (мёртвый) убрать из вызова и из тела.
3. `processOnce` остаётся в `OrderProcessor` до Task 3 (контроллер ещё на нём).

**Tests:**
- `OrderFulfillerTest` — перенос всех `processUntilTerminal_*` тестов из
  `OrderProcessorTest` без изменения утверждений (это регрессионный щит: идемпотентность,
  порядок breaker-вызовов, single-charge, deadline).
- `BackgroundWorkerTest` — обновить стаб processor → fulfiller.

**Verification:** `mvn -q test` — green.
`make scenarios TASK=1 WAIT=5` — 5/5 PASS.

---

## Task 3: CreateOrderService + типизированный InventoryClient + тонкий контроллер

**Files:**
- Новый: `orders/src/main/java/orders/flow/CreateOrderService.java`
  (`record CreateResult(String orderId, OrderStatus status)`)
- `orders/src/main/java/orders/api/OrdersController.java`
- `orders/src/main/java/orders/api/dto/OrderRequest.java` (`amountCents()`)
- `orders/src/main/java/orders/inventory/InventoryClient.java`
- `orders/src/main/java/orders/flow/OrderProcessor.java` (**удалён**)
- `orders/src/main/java/orders/store/OrderRepository.java` (insertPending — без
  изменений сигнатуры: `insertPending(userId, List<Item>, amountCents)`)
- Тесты: `OrdersControllerTest`, `InventoryClientTest`, новый `CreateOrderServiceTest`

**Changes:**
1. `OrderRequest.amountCents()`:
   `items.stream().mapToLong(i -> (long) i.qty() * i.priceCents()).sum()` —
   бизнес-правило рядом с данными (design D2).
2. `InventoryClient.reserve(String orderId, List<Item> items, Duration timeout)` —
   тело `{order_id, items}` из типизированных items (design D4).
3. `CreateOrderService.createOrder(OrderRequest) → CreateResult` (design D2):
   `insertPending` → `reserve` (тайм-аут `max(100, budgetMs − 10)`) →
   `transition(REJECTED)` | `enqueue` + `PENDING`.
4. `OrdersController.createOrder`: `@Valid @RequestBody` → `createOrderService.
   createOrder(r)` → `201 new OrderCreated(res.orderId(), res.status().value())`.
   Удалить: подсчёт суммы, `buildItemsJson`, прямой `transition`.
5. Удалить `OrderProcessor.java` (оба метода переехали).

**Tests:**
- `CreateOrderServiceTest` (новый): reserve ok → PENDING + enqueue; reserve fail →
  REJECTED + transition(REJECTED) + без enqueue; `amountCents` на двух-item запросе
  (2×49900 + 1×1000 = 100800).
- `OrdersControllerTest`: стабы `orderProcessor.processOnce` → `createOrderService.
  createOrder` (возвращает `CreateResult`); утверждения ответов те же (201, id, status).
- `InventoryClientTest`: reserve с `List<Item>` — тело запроса содержит `order_id`
  и items (проверить по боди на тестовом HttpServer).

**Verification:** `mvn -q test` — green.
`make scenarios TASK=1 WAIT=5` — 5/5 PASS; JSON-ответы byte-идентичны
(`{"id": ..., "status": "pending"|"rejected"}`).

---

## Task 4: Имена DTO + Lombok на контроллере + stale-стаб

**Files:**
- `orders/src/main/java/orders/api/dto/OrderRequest.java`, `.../dto/Order.java`
- `orders/src/main/java/orders/api/OrdersController.java`
- `orders/src/test/java/orders/api/OrdersControllerTest.java`

**Changes:**
1. Имена DTO (контракт не меняется за счёт `@JsonProperty`):
   `OrderRequest.user_id` → `userId`; `Order.amount_cents` → `amountCents`
   (`@JsonProperty("amount_cents")`).
2. `OrdersController` — `@RequiredArgsConstructor` (final-поля
   `CreateOrderService`, `OrderRepository`), ручной конструктор удалить (design D5).
3. `OrdersControllerTest`: стаб `"INCOMPLETE"` → `"PENDING"`.

**Tests:** `OrdersControllerTest` (MockMvc): ключи JSON в ответах
(`user_id` в запросе, `amount_cents` в ответе) не изменились — утверждения
`jsonPath("$.amount_cents")` уже проверяют; `@InjectMocks`/ручной конструктор —
под Lombok-конструктор.

**Verification:** `mvn -q test` — green.
`make scenarios TASK=1 WAIT=5` — 5/5 PASS.

---

## Task 5: GlobalExceptionHandler + healthz с проверкой БД

**Files:**
- Новый: `orders/src/main/java/orders/api/GlobalExceptionHandler.java`
- `orders/src/main/java/orders/api/ValidationExceptionHandler.java` (удалён)
- `orders/src/main/java/orders/api/OrdersController.java` (healthz)
- `orders/src/main/java/orders/store/OrderRepository.java` (`isReady()`)
- `orders/src/test/java/orders/api/OrdersControllerTest.java`,
  `orders/src/test/java/orders/store/OrderRepositoryTest.java`

**Changes:**
1. `GlobalExceptionHandler` (`@RestControllerAdvice`) — design D6:
   `MethodArgumentNotValidException` → `400 {"error":"validation_error",
   "message":"invalid request body"}`; `DataAccessException` → `500
   {"error":"db_error",...}` + `log.warn`; прочее `Exception` → `500
   {"error":"internal",...}` + `log.warn` (с `order_id`, если известен).
2. Удалить `ValidationExceptionHandler`.
3. `OrderRepository.isReady(): boolean` — `SELECT 1` (true/false, без исключений).
4. `OrdersController.healthz()` — `orderRepository.isReady()` ? `200` : `503`.

**Tests:**
- `OrdersControllerTest`: healthz при `isReady()=true` → 200; `isReady()=false` →
  503; валидация → 400 (уже есть).
- `OrderRepositoryTest`: `isReady` на живой H2 → true.

**Verification:** `mvn -q test` — green.
`make scenarios TASK=1 WAIT=5` — 5/5 PASS (healthz отвечает 200 на поднятом стенде).

---

## Task 6: Интеграционная проверка

**Files:** — (проверка, не изменение)

**Changes:**
1. `cd orders && JAVA_HOME=~/.jdks/temurin-21.0.12.1 mvn -q test` — все тесты зелёные.
2. `make scenarios TASK=1 WAIT=10` — S1–S5 `PASS`, уровень «полностью»; сверить с
   результатами до change: те же статусы заказов, двойных списаний нет, JSON-ответы
   по контракту (проверка «ответы Orders по контракту» раннера — byte-уровень).
3. Визуальный check раскладки (критерий из design): каждый класс — одна причина
   меняться; `OrdersController` не зависит от `OrderRepository` для переходов.

**Rollback:** `git revert` по задаче; поведение до change — в коммите, прошедшем
`make scenarios TASK=1`.
