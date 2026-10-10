## 1. Error → ApiError (api/dto)

- [x] 1.1 Переименовать файл `orders/src/main/java/orders/api/dto/Error.java` → `ApiError.java`, `record Error(String error, String message)` → `record ApiError(...)`.
- [x] 1.2 `GlobalExceptionHandler`: импорт `orders.api.dto.Error` → `ApiError`; 4 сигнатуры `ResponseEntity<Error>` → `ResponseEntity<ApiError>`; 4 вхождения `new Error(...)` → `new ApiError(...)`. JSON-тела (`"error"`, `"message"`) — без изменений.
- [x] 1.3 `GlobalExceptionHandlerTest`: импорт и все ссылки `Error` → `ApiError`.
- [x] 1.4 Тесты: `cd orders && mvn -q test` — зелёные (73/73; `GlobalExceptionHandlerTest` проверяет 400/404/500-тела).
- [x] 1.5 (утверждено пользователем, вне скоупа) Починить 2 предсуществующих красных теста от незакоммиченной BigDecimal-миграции: `OrderRepositoryTest.insertAndFindById` — сравнение через `compareTo` (NUMERIC(19,4) → scale 4); мок `InventoryClientTest` — ожидание `"price":1}` (valueToTree нормализует BigDecimal).
- [x] 1.6 Спот-чек: `make scenarios ONLY=S5 WAIT=5` — PASS.
- [x] 1.7 (утверждено пользователем, вне скоупа) Дочинить незакоммиченную BigDecimal-миграцию под read-only контракт: wire/БД — центы integer (`price_cents`, `amount_cents`, колонка `amount_cents BIGINT`), внутри — BigDecimal scale 0; `InventoryClient.reserve` шлёт только `sku`+`qty`; S5 PASS (20/20 paid, 0 двойных списаний).

## 2. OrderRepository.Order → StoredOrder (store)

- [x] 2.1 `OrderRepository`: `record Order(...)` → `record StoredOrder(...)`; тип возврата `findById`.
- [x] 2.2 `OrderQueryService.getOrder`: `OrderRepository.Order` → `OrderRepository.StoredOrder`.
- [x] 2.3 `OrderFulfiller` (имя пока старое): `OrderRepository.Order order = orderRepository.findById(orderId)` → `OrderRepository.StoredOrder`.
- [x] 2.4 Тесты: `OrderRepositoryTest` (3 ссылки), `OrderQueryServiceTest` (1), `OrderFulfillerTest` (17) — `OrderRepository.Order` → `OrderRepository.StoredOrder`.
- [x] 2.5 Тесты: `cd orders && mvn -q test` — зелёные.
- [x] 2.6 Спот-чек: `make scenarios ONLY=S5 WAIT=5` — PASS.

## 3. databaseUrl → parseDatabaseUrl (store)

- [x] 3.1 `DataSourceConfig`: `static ParsedUrl databaseUrl(String)` → `static ParsedUrl parseDatabaseUrl(String)`; вызов в `dataSource()` — `parseDatabaseUrl(databaseUrl)`.
- [x] 3.2 `DataSourceConfigTest`: имена 3 методов `databaseUrl_*` → `parseDatabaseUrl_*`; 3 вызова `DataSourceConfig.databaseUrl(` → `DataSourceConfig.parseDatabaseUrl(`.
- [x] 3.3 Тесты: `cd orders && mvn -q test` — зелёные (`DataSourceConfigTest` покрывает конвертацию с паролем/без, с query/без).
- [x] 3.4 Спот-чек: `make scenarios ONLY=S5 WAIT=5` — PASS.

## 4. OrderFulfiller → OrderCharger, fulfill → charge, maxDeadline → timeBudget (flow)

- [x] 4.1 Переименовать файл `orders/src/main/java/orders/flow/OrderFulfiller.java` → `OrderCharger.java`: класс, `LoggerFactory.getLogger(OrderCharger.class)`, конструктор. Javadoc уточнить: `charge` возвращает `true` — заказ в terminal-статусе, `false` — бюджет исчерпан (повторит reaper).
- [x] 4.2 Метод `fulfill(String orderId, Duration maxDeadline)` → `charge(String orderId, Duration timeBudget)`; в теле `maxDeadline.toMillis()` → `timeBudget.toMillis()` (абсолютный `long deadline` остаётся `deadline`).
- [x] 4.3 `BackgroundWorker`: поле и параметр `OrderFulfiller orderFulfiller` → `OrderCharger orderCharger`; в `workerLoop()` локальная `Duration deadline = Duration.ofMillis(deadlineMs)` → `Duration timeBudget = ...`; вызов `orderFulfiller.fulfill(orderId, deadline)` → `orderCharger.charge(orderId, timeBudget)`. Поле `deadlineMs` и свойство `orders.worker.deadline-ms` — без изменений.
- [x] 4.4 Переименовать `OrderFulfillerTest.java` → `OrderChargerTest.java`: класс, поле `OrderFulfiller fulfiller` → `OrderCharger charger`, локальные `OrderFulfiller f` → `OrderCharger c`, все вызовы `.fulfill(` → `.charge(`, префиксы имён тестов `fulfill_*` → `charge_*`.
- [x] 4.5 `BackgroundWorkerTest`: `mock(OrderFulfiller.class)` → `mock(OrderCharger.class)`, переменные `fulfiller` → `charger`, `.fulfill(` → `.charge(`, комментарий «blocked in fulfill» → «blocked in charge».
- [x] 4.6 Тесты: `cd orders && mvn -q test` — зелёные (`OrderChargerTest` — полная машина состояний, `BackgroundWorkerTest` — single-flight).
- [x] 4.7 Спот-чек: `make scenarios ONLY=S5 WAIT=5` — PASS (потерянный ответ → charge + reconcile → paid через переименованную машину).

## 5. Финальная верификация

- [x] 5.1 `cd orders && mvn -q test` — все тесты зелёные.
- [x] 5.2 `make scenarios TASK=1` — все 5 сценариев `PASS` (уровень «полностью»: нет `pending` в конце, нет двойных списаний).
- [x] 5.3 `git status` / `git diff --name-only` — изменены только файлы в `orders/` (плюс артефакты этого change в `openspec/changes/`).
