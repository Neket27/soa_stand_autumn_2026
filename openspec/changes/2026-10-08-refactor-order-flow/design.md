# Design: refactor-order-flow

Реконструкция без изменения поведения: все инварианты 03-architecture.md и требования
основной спеки сохраняются (семантические изменения — в change
`2026-10-08-fix-circuit-breaker-probe`, он реализуется первым).

## Целевая раскладка

Сейчас:

```
OrdersController ── считает сумму, сериализует items, transition("rejected") в БД
       │
       ▼
OrderProcessor ── processOnce (только reserve)  +  processUntilTerminal (charge-машина)
       ▲
BackgroundWorker ── очередь, inFlight, пул, reaper
PaymentCircuitBreaker ── изолирован ✓
```

Цель — один класс на одну причину меняться:

```
api/OrdersController         — тонкий: @Valid, вызов use case, маппинг ответа
flow/CreateOrderService      — sync-путь: insert → reserve → (rejected | enqueue)
flow/OrderFulfiller          — charge-машина: breaker gate → charge → reconcile →
                               transition. ЕДИНСТВЕННЫЙ класс, говорящий с PaymentClient
flow/BackgroundWorker        — механика: очередь, inFlight, пул, reaper («сколько и как
                               часто», не «что»)
flow/PaymentCircuitBreaker   — изолирован ✓ (после fix-circuit-breaker-probe —
                               переиспользуемый «гейт удалённого вызова»: события
                               onReachable/onUnavailable абстрактны)
flow/OrderStatus             — enum, единый источник статусов
payment/PaymentClient        — чистый HTTP (один клиент), 3-состоянная реконсиляция
inventory/InventoryClient    — чистый HTTP, типизированные items
store/OrderRepository        — чистый data access, transition по enum
```

Критерий проверки раскладки (можно сказать на защите): «какую библиотеку я поменяю —
какие классы трону? Payment-контракт → только `PaymentClient` (+ `OrderFulfiller` на
границе); Inventory-контракт → только `InventoryClient`; схема БД → только
`OrderRepository`/`schema.sql`; API → только `api/`. Ни один класс не трогается
по двум причинам сразу».

## D1. Разрез OrderProcessor

### Проблема

`processOnce` и `processUntilTerminal` — две разные работы над двумя разными
«времями»: sync-путь (бюджет 900 мс, только Inventory) и фоновый путь (минуты,
Payment + breaker). Класс тащит 4 коллаборейта, каждый метод использует своё
подмножество; `amountCents` в `processOnce` — мёртвый параметр.

### Решение

- **`OrderFulfiller`** (`@Service`): `fulfill(String orderId, Duration maxDeadline):
  boolean` — текущее тело `processUntilTerminal` (включая хелперы
  `decideAfterNetwork`/`transitionToPaid`/`transitionToRejected`/`release` из
  fix-circuit-breaker-probe). Коллаборейты: `PaymentClient`, `InventoryClient`,
  `OrderRepository`, `PaymentCircuitBreaker`, `chargeTimeoutMs`.
- **`processOnce`** растворяется в `CreateOrderService` (D2) — reserve становится
  одним из шагов use case, отдельного класса под один вызов не оправдано.
- `BackgroundWorker` заменяет `OrderProcessor` на `OrderFulfiller` (сигнатура вызова
  та же, минус мёртвый `itemsJson`).
- Имя `OrderFulfiller`: «доводит заказ до terminal-статуса» — отличимо от
  «механики» в `BackgroundWorker`.

## D2. CreateOrderService как sync-use case

```java
public record CreateResult(String orderId, OrderStatus status) {}

@Service
public class CreateOrderService {
    public CreateResult createOrder(OrderRequest request) {
        long amount = request.amountCents();                      // бизнес-правило рядом с данными
        String id = orderRepository.insertPending(request.userId(), request.items(), amount);
        Duration reserveTimeout = Duration.ofMillis(Math.max(100, budgetMs - 10));
        boolean reserved = inventoryClient.reserve(id, request.items(), reserveTimeout);
        if (!reserved) {
            orderRepository.transition(id, OrderStatus.REJECTED);
            return new CreateResult(id, OrderStatus.REJECTED);
        }
        backgroundWorker.enqueue(id);
        return new CreateResult(id, OrderStatus.PENDING);
    }
}
```

Последствия:
- Контроллер: `createOrder(@Valid @RequestBody OrderRequest r)` →
  `CreateResult res = createOrderService.createOrder(r)` → 201 с `res.status().value()`.
  Ни бизнес-логики, ни прямых `transition`, ни сериализации items.
- **Правило суммы** `Σ qty × price_cents` — вычисляемый метод
  `OrderRequest.amountCents()`: одно правило, рядом с данными, тестируется без HTTP.
  Отдельный домен-пакет с агрегатом — YAGNI при одной таблице (Non-goal).
- Второй путь изменения статуса (transition из контроллера) исчезает: все переходы —
  в flow-слое (`CreateOrderService`, `OrderFulfiller`).

## D3. enum OrderStatus

```java
public enum OrderStatus {
    PENDING("pending"), PAID("paid"), REJECTED("rejected");
    private final String value;   // value() — для БД/API
}
```

- `OrderRepository.transition(String id, OrderStatus to)`: `SET status = ? WHERE id = ?
  AND status = 'pending'` — lowercase-литерал через `to.value()`, guard без изменений.
- `OrderRepository.Order` (record) — поле `status` остаётся `String` (это DTO
  репозитория, строка из БД); сравнения в `OrderFulfiller`/`CreateOrderService` —
  через `OrderStatus.valueOf`/`==`. (Полный переход record на enum — опционально,
  не усложняем: граница SQL/домен и так на `value()`.)
- `ProcessResult`-строки `"PENDING"/"REJECTED"` исчезают вместе с `processOnce`:
  результат use case — `CreateResult` с `OrderStatus`.
- DTO API (`Order`, `OrderCreated`) — строки, контракт не меняется.

Требование «Order status modeled as enum» — из дельты
`2026-10-07-harden-observability` (перенесено сюда; при архивации того change из его
дельты исключить).

## D4. Типизированный InventoryClient

`reserve(String orderId, String itemsJson, Duration)` → `reserve(String orderId,
List<Item> items, Duration)`: тело запроса `{order_id, items: [...]}` собирается
клиентом из типизированных items (ObjectNode). `itemsJson`-строка перестаёт
проходить через слои:

- `CreateOrderService`: один `items`-объект → и в `insertPending` (репозиторий сам
  сериализует для JSONB-колонки — storage concern), и в `reserve` (клиент сам
  сериализует HTTP-тело). Две сериализации — в два разных формата (БД vs HTTP),
  дублирования одной строки больше нет.
- `OrderFulfiller.fulfill(orderId, maxDeadline)` — без `itemsJson` (был мёртвым).

## D5. Lombok для конструкторов и чистка

`@RequiredArgsConstructor` — `OrdersController`: после рефакторинга у него только
final-поля (`CreateOrderService`, `OrderRepository`) и нет `@Value`
(`budgetMs` переезжает в `CreateOrderService`). Классы с `@Value`-параметрами
(`CreateOrderService`, `OrderFulfiller`, `PaymentCircuitBreaker`, `PaymentClient`,
`InventoryClient`, `BackgroundWorker`) — **явные** конструкторы: Spring не видит
`@Value` в параметрах Lombok-сгенерированного конструктора без `lombok.config` с
`copyableAnnotations` (не вводим — Non-goal). Lombok уже в `pom.xml` (`provided`):
теперь каждая зависимость обоснована задачей (04-standards).

Остальная чистка:

| Что | Почему | Где |
|---|---|---|
| 2-й `HttpClient` в `PaymentClient` | `lookupClient` идентичен `httpClient`; один клиент — одна настройка (сделано в fix-circuit-breaker-probe) | `PaymentClient` |
| `user_id` → `userId`, `amount_cents` → `amountCents` | snake_case-имена record-компонентов; `@JsonProperty` держит контракт | DTO |
| Стаб `"INCOMPLETE"` в тесте | остаток удалённой PAID-ветки (a9f934d); controller проверяет только REJECTED | `OrdersControllerTest` |

## D6. Глобальная обработка исключений и readiness

### GlobalExceptionHandler

`ValidationExceptionHandler` обобщается до `GlobalExceptionHandler`
(`@RestControllerAdvice`, `orders/api`):

| Исключение | Ответ | Почему |
|---|---|---|
| `MethodArgumentNotValidException` | `400 {"error":"validation_error","message":...}` | единственный не-201 на `POST /orders` по контракту |
| `DataAccessException` | `500 {"error":"db_error",...}` + WARN-лог | сбой БД — честная системная ошибка, не маскируем 404-м |
| прочее `Exception` | `500 {"error":"internal",...}` + WARN-лог | нет whitelabel-страниц; тело — JSON по образцу контракта |

**Граница по спеке.** Требование «нет 5xx на `POST /orders`» защищено структурой:
все отказы Payment/Inventory закрываются внутри flow-слоя (`CreateOrderService` —
`reserve` с тайм-аутом; `OrderFulfiller` — только в фоне), наружу уходит только
`201 pending/rejected/paid`. 500 с POST возможен лишь при сбое **локальной** БД
(insert) — вне матрицы отказов задания (раннер отказывает только Payment; Postgres
живёт с сервисом в одном compose). Фиксировать это поведение «201 pending при
смерти БД» нельзя: заказ в БД не будет, ответ был бы ложью.

### healthz с проверкой готовности

`GET /healthz` → `SELECT 1` через `JdbcTemplate`: `200` когда БД доступна, иначе
`503`. Инвариант 03-architecture: «`GET /healthz` отвечает 200, когда сервис
**готов** принимать заказы». Раннер ждёт healthz до 2 минут после `make up` —
проверка БД добавляет десятки миллисеконд на запрос, для старта это безразлично,
а «200 на мёртвом сервисе» исключено.

## Обязательные вопросы дизайна

**Как соблюдается бюджет 1 с.** Неизменно: sync-путь — `insert` + `reserve`
(тайм-аут ≤ budget−10 мс, бюджет 900 мс из `orders.process.budget-ms`); charge —
только в фоне. Разрез классов не добавляет вызовов в sync-путь (шаги те же,
выполняются тем же потоком-обработчиком HTTP-запроса, виртуальные потоки без
изменений).

**Как исключено двойное списание.** Неизменно: Idempotency-Key и тело — те же;
`OrderFulfiller` повторяет логику `processUntilTerminal` дословно (включая
исправления fix-circuit-breaker-probe). Регрессионный щит — существующие тесты
`OrderProcessorTest`, перенесённые на `OrderFulfiller` без изменения утверждений
(в т.ч. «нет reconcile до NETWORK», «один charge на заказ»).

**Как заказ с потерянным ответом станет paid.** Неизменно: POST-first + idempotent
retry + трёхсостоянная реконсиляция (fix-circuit-breaker-probe). Путь
pending → reaper → `OrderFulfiller.fulfill` — тот же, что был через
`OrderProcessor`.

**Как ограничивается нагрузка на Payment в блэк-холе.** Неизменно: пул 8 воркеров,
breaker-gate в `OrderFulfiller`, reaper пропускает при OPEN. Механика
`BackgroundWorker` (очередь, inFlight, single-flight) не меняется.
