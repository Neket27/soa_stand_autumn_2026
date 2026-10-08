# Tasks: contract-dto-responses

Сборка/тесты: `cd orders && JAVA_HOME=~/.jdks/temurin-21.0.12.1 mvn -q test`.
Каждая задача оставляет компилируемое состояние с зелёными тестами и
прогоном `make scenarios ONLY=... WAIT=5` (сценарий, который её проверяет).
Изменения только в `orders/`.

- [x] Task 1: OrderNotFoundException + маппинг 404 в GlobalExceptionHandler
- [x] Task 2: OrderQueryService + контроллер без null-чека (201 аннотацией, 404 исключением)
- [x] Task 3: Интеграционная проверка (mvn test + make scenarios TASK=1)

---

## Task 1: OrderNotFoundException + маппинг 404 в GlobalExceptionHandler

**Files:**
- Новый: `orders/src/main/java/orders/api/OrderNotFoundException.java`
- `orders/src/main/java/orders/api/GlobalExceptionHandler.java`
- Новый: `orders/src/test/java/orders/api/GlobalExceptionHandlerTest.java`

**Changes:**
1. `OrderNotFoundException extends RuntimeException` с `order_id` в
   конструкторе и `message` вида `"order not found: <id>"`.
2. `GlobalExceptionHandler.handleNotFound(OrderNotFoundException)` →
   `404` + `new Error("not_found", "order not found")` (design D2).

**Tests:** `GlobalExceptionHandlerTest` (без Spring-контекста):
- `handleNotFound_returns404WithErrorBody` — статус 404, тело
  `{"error":"not_found","message":"order not found"}`.

**Verification:** `mvn -q test` — green.
`make scenarios ONLY=S1 WAIT=5` — PASS (исключение ещё не бросается, поведение
неизменно).

---

## Task 2: OrderQueryService + контроллер без null-чека

**Files:**
- Новый: `orders/src/main/java/orders/api/OrderQueryService.java`
- `orders/src/main/java/orders/api/OrdersController.java`
- Новый: `orders/src/test/java/orders/api/OrderQueryServiceTest.java`
- `orders/src/test/java/orders/api/OrdersControllerTest.java`

**Changes:**
1. `OrderQueryService` (`@Service`, `orders.api`, `@RequiredArgsConstructor`):
   `getOrder(String id) → Order` — lookup по `OrderRepository.findById`,
   null → `throw new OrderNotFoundException(id)`, иначе маппинг в API-DTO
   `Order` (design D2).
2. `createOrder` — возвращает `OrderCreated`; `@ResponseStatus(HttpStatus.CREATED)`
   на методе; `ResponseEntity` и импорт убрать (design D1).
3. `getOrder` — одна строка: `return orderQueryService.getOrder(id)`;
   null-чек, маппинг и `ResponseEntity` из контроллера убраны (design D2).
   Поле `OrderRepository` в контроллере остаётся только для `healthz`
   (isReady).
4. Тесты: `MockMvcBuilders.standaloneSetup(controller)` +
   `.setControllerAdvice(new GlobalExceptionHandler())`;
   `getOrder_notFound_returns404` добавить проверку тела
   (`jsonPath("$.error").value("not_found")`); тесты 201/400 без изменения
   утверждений.

**Tests:**
- `OrderQueryServiceTest` (новый):
  - `getOrder_found_returnsMappedDto` — id/status/amount_cents перенесены из
    `OrderRepository.Order` в `Order`.
  - `getOrder_notFound_throwsOrderNotFoundException` — findById → null →
    исключение с id.
- `OrdersControllerTest`:
  - `createOrder_pending/rejected` — 201 + тело (аннотация статуса работает
    в standalone MockMvc).
  - `getOrder_found_returns200` — стаб `orderQueryService.getOrder` →
    DTO; тело `Order` byte-по-полям.
  - `getOrder_notFound_returns404` — стаб сервиса бросает
    `OrderNotFoundException` → 404 + Error-тело (через advice).
  - 400-тесты валидации — без изменений.

**Verification:** `mvn -q test` — green.
`make scenarios ONLY=S1,S5 WAIT=5` — PASS (POST/GET-путь, byte-контракты).

---

## Task 3: Интеграционная проверка

**Files:** — (проверка, не изменение)

**Changes:**
1. `cd orders && JAVA_HOME=~/.jdks/temurin-21.0.12.1 mvn -q test` — все тесты
   зелёные.
2. `make scenarios TASK=1 WAIT=10` — S1–S5 `PASS`, уровень «полностью»;
   сверка с результатами до change: те же статусы, двойных списаний нет,
   «ответы Orders по контракту» (раннер проверяет 200/201/400-ответы по
   контракту; 404-тело раннер не порождает — безопасность добавления тела
   обоснована design D2).
3. Визуальный check: в `OrdersController` нет `ResponseEntity` кроме
   `healthz`, нет null-чека и `findById`; `GlobalExceptionHandler` —
   единственный поставщик 400/404/500.

**Rollback:** `git revert` по задаче; поведение до change — в коммите,
прошедшем `make scenarios TASK=1`.
