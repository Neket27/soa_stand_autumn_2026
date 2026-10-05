# Tasks: resilient-order-payment

Правила (из 04/07): изменения **только в `orders/`**; deprecated-паттерны 07-anti-patterns.md
запрещены; в каждой задаче — тест; после каждой задачи — `make scenarios ONLY=… WAIT=5` (если задача
влияет на поведение по сценарию) и `cd orders && mvn -q test`.

Стек: Java 21, Spring Boot 3.3+, Maven, `java.net.http.HttpClient`, `JdbcTemplate` + HikariCP.
Базовый пакет `orders`. Виртуальные потоки — только для обработки HTTP-запросов
(`spring.threads.virtual.enabled=true`); фоновая обработка — ограниченный пул (бюджет S4).
Для локальных `mvn test`/IDEA нужен JDK 21 (сборка в Docker — `Dockerfile`, от локального JDK не зависит).

## 1. Каркас Maven-проекта + Dockerfile

- Удалить содержимое `orders/` (Go-пример). Создать `orders/pom.xml` (Spring Boot 3.3.x parent,
  `spring-boot-starter-web`, `spring-boot-starter-jdbc`, `org.postgresql:postgresql`,
  `spring-boot-starter-test`), `OrdersApplication`, `src/main/resources/application.properties`
  (`server.port=${PORT:8080}`, `spring.sql.init.mode=always`).
- `orders/Dockerfile` — multi-stage: сборка `maven:3.9-eclipse-temurin-21`, рантайм
  `eclipse-temurin:21-jre`. `pom.xml` — `<java.version>21</java.version>`.
  Собирается `docker build orders/` без аргументов.
- В `application.properties`: `spring.threads.virtual.enabled=true` (виртуальные потоки
  на обработку запросов). Фон — ограниченный пул (см. задачу 6).
- Тест: `mvn -q test` зелёный (smoke-тест контекста).
- Готово когда: `make up` поднимает сервис, `GET /healthz` → `200` в пределах 2 минут.
- Проверка: `make scenarios ONLY=S1 WAIT=5` (остальные сценарии могут FAIL — дальше нет логики).

## 2. Схема БД + OrderRepository

- `schema.sql`: таблица `orders` (id, user_id, status CHECK in pending/paid/rejected, amount_cents,
  items JSONB, created_at) — `CREATE TABLE IF NOT EXISTS`.
- `store/DataSourceConfig`: конвертер `DATABASE_URL` (`postgres://user:pass@host:port/db`) →
  `DataSource` (jdbc url + user + pass). Чтение из `DATABASE_URL`.
- `store/OrderRepository` (`JdbcTemplate`): `insertPending(order)`, `transition(id, toStatus)` —
  `UPDATE … WHERE id=? AND status='pending'` (возвращает, сколько строк), `findById(id)`,
  `findPendingIds()` — `SELECT id WHERE status='pending' ORDER BY created_at`.
- Тесты (встроенный/тестовый Postgres или H2-совместимый слайс + реальный SQL): insert → findById;
  transition pending→paid применяется один раз (повторный transition не меняет статус и возвращает 0);
  findPendingIds возвращает только pending.
- Готово когда: `mvn -q test` зелёный.
- Проверка: `make scenarios ONLY=S1 WAIT=5`.

## 3. PaymentClient + InventoryClient

- `payment/PaymentClient` на общем `HttpClient` (боб):
  - `charge(orderId, amount, timeout)` → `POST /payments` с `Idempotency-Key: <orderId>`, телом
    `{order_id, amount_cents, currency:"RUB"}`; возвращает результат: `CHARGED` / `DECLINED`(402) /
    `SERVER_ERROR`(500) / `NETWORK`(тайм-аут, сброс соединения).
  - `lookupCharges(orderId, timeout)` → `GET /payments?order_id=` → список списаний.
- `inventory/InventoryClient`: `reserve(orderId, items, timeout)` → `POST /reservations`
  (200/201 ок); `release(orderId, timeout)` → `DELETE /reservations/{orderId}` (204 ок, идемпотентно).
- Явный тайм-аут на каждый запрос (параметр). Общий `HttpClient` конфигурируется один раз.
- Тесты: локальный mock-HTTP (WireMock/`com.sun.net.httpserver` или `MockWebServer`): charge шлёт
  Idempotency-Key и фикс. тело; 402→DECLINED; 500→SERVER_ERROR; тайм-аут→NETWORK; lookup парсит
  список; reserve/release корректные коды.
- Готово когда: `mvn -q test` зелёный.
- Проверка: `make scenarios ONLY=S1 WAIT=5`.

## 4. OrderProcessor (логика одного заказа)

- `flow/OrderProcessor.processOnce(order, budget)` → `PAID` / `REJECTED` / `INCOMPLETE`:
  1. `reserve`; 2. `charge` (в рамках budget); по результату: `CHARGED`→`PAID`; `DECLINED`→
     `release`+`REJECTED`; `SERVER_ERROR`→`INCOMPLETE`; `NETWORK`→`lookupCharges`: есть списание→`PAID`,
     нет→`INCOMPLETE`.
- Порядок `reserve → charge` (не списывать без резерва). Тело charge постоянно (нет 422).
- Тесты (mock клиентов): каждый ветвящий результат → корректный исход; DECLINED вызывает release;
  NETWORK + есть списание → PAID без повторного charge; NETWORK + нет списания → INCOMPLETE;
  charge всегда с тем же Idempotency-Key.
- Готово когда: `mvn -q test` зелёный.
- Проверка: `make scenarios ONLY=S1 WAIT=5`.

## 5. REST-контроллер + быстрый путь

- `api/OrdersController`: `GET /healthz`→200 (готовность: БД доступна); `POST /orders`;
  `GET /orders/{id}`→200 `{id,status,amount_cents}` / 404.
- `POST /orders`: валидация (некорректно→400); `insertPending`; fast-path `processOnce` с бюджетом
  ~900 мс (reserve 200 мс + charge остаток); terminal→ответ 201 со статусом; иначе 201 `pending` +
  передать в фон. **Никаких 5xx.**
- `api/dto/*` — record-ы по контракту.
- Тесты: `@WebMvcTest`/MockMvc — валидация (400), 201 pending при недоступном Payment (mock-клиент
  бросает NETWORK), 201 paid при успешном, 404 для неизвестного id; `amount_cents = Σ qty×price`.
- Готово когда: `mvn -q test` зелёный; локальный smoke `curl POST /orders` отвечает ≤1 с.
- Проверка: `make scenarios ONLY=S3 WAIT=5` (бюджет при медленном Payment) и `ONLY=S1 WAIT=5`.

## 6. Фоновые воркеры (restart-safe)

- `flow/BackgroundWorker`: пул из 8 фиксированных воркеров; `LinkedBlockingQueue<String>`;
  `inFlight` (ConcurrentHashMap); reaper (scheduled, тик 500 мс) → `findPendingIds()` → для id не в
  `inFlight` `putIfAbsent`+enqueue. Воркер: `processUntilTerminal(id)` — цикл `processOnce` с
  backoff (0.5→1→2 с, кап) до terminal или дедлайна ~25 с, затем `inFlight.remove` (reaper подхватит).
- Запуск reaper + воркеров при старте приложения (после готовности БД).
- Тесты: reaper подхватывает pending из (тестовой) БД; дедупликация через `inFlight`; заказ доводится
  до terminal; после «рестарта» (новый воркер-инстанс) pending из БД снова обрабатывается.
- Готово когда: `mvn -q test` зелёный.
- Проверка: `make scenarios ONLY=S5 WAIT=5` (lost response доводится до paid в фоне).

## 7. PaymentCircuitBreaker

- `flow/PaymentCircuitBreaker`: `CLOSED/OPEN/HALF_OPEN`; `allow()`; `onSuccess()`;
  `onTimeoutOrConnError()`. Срабатывает **только** на NETWORK (тайм-аут/сброс), не на 500. OPEN на
  `openFor` (~5 с); HALF_OPEN пускает 1 «пробу»; успех→CLOSED, сбой→OPEN.
- Интеграция: `OrderProcessor`/fast-path перед `charge` проверяют `allow()`; если OPEN → `INCOMPLETE`
  (заказ в фон, без запроса в Payment). Fon-воркер при OPEN не ходит в Payment, ждёт.
- Тесты: переходы состояний; первый NETWORK открывает; в OPEN запросы не идут; HALF_OPEN 1 проб;
  500 не открывает breaker.
- Готово когда: `mvn -q test` зелёный.
- Проверка: `make scenarios ONLY=S4 WAIT=5` (лимит запросов в блэк-холе; после подъёма — paid).

## 8. Сборка в Spring-контекст + e2e локально

- Провести бобы: `HttpClient`, `PaymentClient`, `InventoryClient`, `OrderRepository`,
  `OrderProcessor`, `BackgroundWorker`, `PaymentCircuitBreaker`, `OrdersController`; чтение
  `PAYMENT_URL`, `INVENTORY_URL`, `DATABASE_URL`, `PORT` из окружения.
- Локальный e2e: `make up`, ручные заказы (дашборд `make ui` / curl) — paid/rejected/pending корректны.
- Тесты: интеграционный тест полного контекста (context loads) — зелёный.
- Готово когда: `mvn -q test` зелёный; `make up` + smoke-заказы работают.
- Проверка: `make scenarios TASK=1` (полный прогон, пауза 30 с) — фиксировать статусы S1–S5.

## 9. Добить до «полностью» + запас на скрытые параметры

- Разобрать каждый FAIL по `results/task_1.html` (журнал фактов, время ответов, запросы к Payment).
- Убедиться: нет двойных списаний; нет `rejected` с деньгами; нет 5xx; в конце нет `pending`;
  S4 ≤ лимита запросов. При необходимости подкрутить тайминги/backoff/openFor (не под открытые числа,
  а для запаса).
- Один контрольный прогон с изменёнными параметрами (`make scenarios PARAMS=… WAIT=5`) для проверки
  устойчивости (не формально, а как страховка).
- Готово когда: `make scenarios TASK=1` — все 5 сценариев `PASS`, уровень «полностью»;
  `git status`/`git diff` — изменены только файлы в `orders/` (+ `.gitignore`).
