# Change: refactor-order-flow

## Why

Код проходит все сценарии, но разграничение ответственности размывает: один класс
выполняет две разные работы, контроллер содержит бизнес-логику, статусы — строки в двух
регистрах, по коду размазаны мёртвые параметры и зависимости.

Конкретно:

1. **`OrderProcessor` несёт две ответственности.** `processOnce` — синхронный шаг
   «резервирование в рамках бюджета» (использует только `InventoryClient`; параметр
   `amountCents` не используется; payment/breaker не нужны). `processUntilTerminal` —
   фоновая charge-машина (payment + breaker + release + transition). Каждый метод
   использует подмножество коллаборейтов; класс меняется по двум разным причинам.
2. **Контроллер делает бизнес.** Считает сумму заказа (`Σ qty × price_cents` —
   бизнес-правило), сериализует items в JSON, **сам вызывает
   `orderRepository.transition(orderId, "rejected")`** — второй путь изменения статуса
   мимо flow-слоя.
3. **Статусы — строки в двух регистрах** (`"pending"/"paid"/"rejected"` в БД/API и
   `"PENDING"/"REJECTED"` в `processOnce`) в 5 файлах; опечатка = тихий баг. Требование
   «Order status modeled as enum» записано в дельте `2026-10-07-harden-observability`,
   но не попало в её tasks и не реализовано.
4. **Мёртвое и дублирующееся:** `itemsJson` сериализуется дважды (контроллер и
   репозиторий); `itemsJson` в `processUntilTerminal` — мёртвый параметр (не
   используется в теле метода); второй идентичный `HttpClient` в `PaymentClient`;
   **Lombok в `pom.xml`, но не используется ни в одном классе** (нарушает 04-standards:
   «каждая зависимость должна быть обоснована задачей»); stale-стаб `"INCOMPLETE"` в
   `OrdersControllerTest` (остаток удалённой PAID-ветки).
5. **Имена DTO:** `user_id` и `amount_cents` — имена компонентов record в snake_case;
   Java-конвенция нарушена, хотя контракт (`@JsonProperty`) не страдает.

## What changes

### 1. Разрез OrderProcessor (design D1)

- `processUntilTerminal` + хелперы → новый класс **`OrderFulfiller`**
  (`orders/flow`): единственная charge-машина, единственный класс, говорящий с
  `PaymentClient`. Коллаборейты: `PaymentClient`, `InventoryClient`,
  `OrderRepository`, `PaymentCircuitBreaker`, тайм-ауты.
- `processOnce` (резерв в рамках бюджета) → в **`CreateOrderService`** (синхронный
  use case, design D2).
- `OrderProcessor` удаляется.

### 2. `CreateOrderService` — единственный синхронный use case (design D2)

`createOrder(OrderRequest) → (id, status)`: валидация уже в контроллере →
`insertPending` → `reserve` → не удалось зарезервировать: `transition(REJECTED)`,
ответ `rejected`; удалось: `enqueue`, ответ `pending`. Контроллер становится тонким:
вызывает use case и мапит результат в HTTP-ответ. Сумма заказа — вычисляемый
`OrderRequest.amountCents()`.

### 3. `enum OrderStatus` (design D3)

`enum OrderStatus { PENDING, PAID, REJECTED }` с `value()` (lowercase для БД/API).
`OrderRepository.transition(id, OrderStatus)`. DTO-граница API остаётся строкой —
контракт не меняется. Требование переносится из дельты
`2026-10-07-harden-observability` (там его при архивации исключить).

### 4. Типизированный InventoryClient (design D4)

`reserve(orderId, List<Item>, timeout)` — клиент сам собирает тело запроса из
типизированных items; `itemsJson`-строки перестают гулять через слои. Мёртвый
параметр `itemsJson` в фоне убирается; items сериализуется один раз (в
репозитории, для хранения).

### 5. Lombok для конструкторов (design D5)

- `@RequiredArgsConstructor` — `OrdersController` (после рефакторинга только
  final-поля: `CreateOrderService`, `OrderRepository`; `@Value` в контроллер не
  уходит — `budgetMs` переезжает в `CreateOrderService`).
- Классы с `@Value`-параметрами (`CreateOrderService`, `OrderFulfiller`,
  `PaymentCircuitBreaker`, `PaymentClient`, `InventoryClient`,
  `BackgroundWorker`) — **явный** конструктор: Spring не видит `@Value` в
  параметрах Lombok-сгенерированного конструктора без `lombok.config` с
  `copyableAnnotations` (не вводим).
- Lombok уже в `pom.xml` (scope `provided`) — теперь используется, зависимость
  обоснована задачей.

### 6. Чистка (design D5)

- Удалить второй `HttpClient` в `PaymentClient` (`lookupClient` — идентичен
  `httpClient`).
- Имена DTO: `user_id` → `userId`, `Order.amount_cents` → `amountCents`
  (`@JsonProperty` сохраняют контракт).
- Исправить stale-стаб `"INCOMPLETE"` в `OrdersControllerTest` → `"PENDING"`.

### 7. Глобальная обработка исключений и readiness (design D6)

- `GlobalExceptionHandler` (`@RestControllerAdvice`, `orders/api`):
  `MethodArgumentNotValidException` → `400 {"error","message"}` (заменяет
  `ValidationExceptionHandler`), `DataAccessException` → `500`, прочее → `500`
  с логированием (WARN с контекстом).
- `GET /healthz` → `SELECT 1` к БД: `200` только когда сервис реально готов
  принимать заказы (инвариант 03-architecture), иначе `503`.
- Граница по спеке: 5xx на `POST /orders` невозможен по отказах Payment/Inventory —
  все их ошибки закрываются внутри flow-слоя (`pending`/`rejected`). 500 с POST
  возможны только при сбое локальной БД, что вне матрицы отказов задания
  (Postgres живёт с сервисом; раннер отказывает только Payment).

## Scope

- `orders/src/main/java/orders/flow/` — `OrderProcessor.java` (удалён),
  `OrderFulfiller.java`, `CreateOrderService.java`, `OrderStatus.java` (новые),
  `BackgroundWorker.java` (вызов `OrderFulfiller`)
- `orders/src/main/java/orders/api/OrdersController.java` (healthz с проверкой БД),
  `orders/src/main/java/orders/api/GlobalExceptionHandler.java` (новый),
  `orders/src/main/java/orders/api/ValidationExceptionHandler.java` (удалён),
  `orders/src/main/java/orders/api/dto/OrderRequest.java`, `.../dto/Order.java`
- `orders/src/main/java/orders/inventory/InventoryClient.java`
- `orders/src/main/java/orders/payment/PaymentClient.java` (удаление 2-го клиента)
- `orders/src/main/java/orders/store/OrderRepository.java` (transition по enum)
- Соответствующие тесты

## Non-goals

- Изменение семантики отказов, брейкера, идемпотентности — это
  `2026-10-08-fix-circuit-breaker-probe` (сначала он, потом этот change:
  `OrderFulfiller` пишется сразу на новом API брейкера).
- Домен-пакет с агрегатом `Order` (YAGNI: 1 таблица, 1 правило суммы).
- `lombok.config` с `copyableAnnotations` — явные конструкторы там, где `@Value`.
- Замена `JdbcTemplate` на JPA, `HttpClient` на `RestClient` (см. Non-goals
  `2026-10-07-spring-idiom-refactor` — решения те же).
- Изменения API-контракта (JSON-тела byte-идентичны), поведения при отказах.
- Изменения вне `orders/`.
