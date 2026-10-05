# Design: resilient-order-payment

Стек зафиксирован ADR-001 (ревизия 2026-10-05): **Java 21 (Temurin), Spring Boot 3.3+, Maven,
PostgreSQL 16, Docker multi-stage**. Инварианты — 03-architecture.md; запреты —
07-anti-patterns.md; факты заглушек — проверены по коду `mocks/` и `runner/`.

**Виртуальные потоки (Java 21):** используются на стороне обработки HTTP-запросов
(`spring.threads.virtual.enabled=true`) — каждый `POST /orders` идёт на виртуальном потоке.
Фоновая обработка **намеренно остаётся ограниченным пулом**: неограниченные «поток на каждый
pending-заказ» в блэк-холе S4 одновременно били бы в Payment и слили бы лимит запросов.
Ограниченный пул + circuit breaker дают этот лимит естественно.

## Ключевые решения

### D1. Идемпотентность через Idempotency-Key (главное)

Каждый `POST /payments` несёт заголовок `Idempotency-Key: <order_id>` и **фиксированное** тело
`{order_id, amount_cents, currency: "RUB"}`. По контракту `payment.yaml`:

- первый успешный запрос с ключом выполняет списание (`201`);
- повтор с тем же ключом **и тем же телом** возвращает ту же операцию (`200` + тот же `charge_id`,
  или снова `402`) **без нового списания**;
- тот же ключ с другим телом — `422` (у нас тело постоянно — этого не будет);
- `500` ключ **не** регистрирует (операция не выполнена).

**Следствие:** сколько бы раз мы ни ретраили (500, тайм-аут, потеря ответа), по одному заказу будет
**≤ 1 списания**. Двойное списание исключено на уровне контракта Payment, а не нашим кодом — это
прямой ответ на вопрос защиты «откуда вы знаете, что деньги не спишутся дважды».

### D2. Быстрый/медленный путь (бюджет 1 с)

`POST /orders`:

1. валидация → `400` на некорректное тело (единственный не-201 ответ);
2. генерация `order_id` (UUID v4), `amount_cents = Σ qty × price_cents` (целые, `long`);
3. `INSERT orders (status='pending')` — **до** сайд-эффектов: заказ сразу виден в `GET /orders/{id}`,
   а статус сходится в БД;
4. попытка довести до terminal в рамках бюджета (~900 мс с запасом до 1 с):
   `reserve` (тайм-аут 200 мс) → `charge` (тайм-аут = остаток бюджета);
   - `201/200` → `paid`;
   - `402` → `release` + `rejected`;
5. если terminal не достигнут (тайм-аут / 500 / сеть / breaker открыт) → ответ `201 {status: pending}`
   и заказ уходит в фоновую обработку.

**Бюджет 1 с соблюдается**, потому что на синхронном пути у каждого исходящего вызова явный
тайм-аут, сумма которых < 1 с; медленный/зависший Payment превращается в `pending`, а не в
ожидание. Никакого 5xx: недоступность Payment/Inventory — не ошибка, а повод ответить `pending`.

### D3. Фоновая обработка (нет pending в конце; жива после рестарта)

- Пул из **W фиксированных** воркеров (осознанно ограниченный — бюджет S4; не «вирт-поток на
  каждый заказ») + очередь `LinkedBlockingQueue<String>` + множество `inFlight` (дедупликация).
- **Reaper** (один scheduled-тред, тик ~500 мс): `SELECT id FROM orders WHERE status='pending'`
  → для каждого id не в `inFlight` делает `putIfAbsent` + enqueue. **На старте процесса reaper
  подхватывает все `pending` из БД** → рестарт процесса не теряет заказы (требование 03-architecture).
- Воркер: `processUntilTerminal(id)` — цикл с backoff до terminal или дедлайна на заказ (~25 с);
  затем отпускает (удаляет из `inFlight`), reaper подхватит снова на следующем тике.
- Переход статуса — `UPDATE orders SET status=? WHERE id=? AND status='pending'`: атомарно,
  выигрывает один, нет гонки fast/background.

**Почему БД как источник pending (а не чисто in-memory):** требование — после рестарта в рамках
прогона заказы не теряются. In-memory очередь при рестарте теряет `pending`; reaper по БД — нет.

### D4. Реконсиляция (lost response → paid)

После неопределённого исхода (`charge` завершился сетевой ошибкой/тайм-аутом — «нет ответа» ≠
«деньги не списаны», S5):

- `GET /payments?order_id=<id>` — авторитетный способ узнать, есть ли списание (в заглушке **не
  задерживается**, в отличие от `POST`);
- если списание есть → `paid` (без повторного списания);
- если нет → повтор `charge` с тем же Idempotency-Key (безопасно: либо новое списание, либо `200`
  с тем же `charge_id`).

**Заказ с потерянным ответом становится `paid`** через реконсиляцию (или через идемпотентный
повтор, который Payment вернёт как `200` + то же `charge_id`). Ни то, ни другое не создаёт второго
списания.

### D5. Circuit breaker (бюджет S4)

Свой breaker (~50 строк), состояния `CLOSED → OPEN → HALF_OPEN`:

- срабатывает **только** на тайм-аут / ошибку соединения (**не** на `500` — иначе в S2 закрыл бы зря);
- `CLOSED`: запросы идут; первый тайм-аут → `OPEN` на `openFor` (~5–10 с);
- `OPEN`: новые заказы не ходят в Payment (сразу `pending`), фон не шлёт запросов;
- по истечении `openFor` → `HALF_OPEN`: пускает ровно **1** «пробу» (charge queued-заказа);
  успех → `CLOSED`, сбой → `OPEN` снова.

**Нагрузка на Payment в блэк-холе ограничена**: за 60 с отказа ≈ 1 (первый, открывший breaker) +
~6–8 «проб» ≪ 30 (открытый лимит S4). `GET /payments` в блэк-холе **не шлём** (breaker открыт) —
важно, т.к. `downRequests` в раннере считает и `POST`, и `GET`.

## Обработка одного заказа (state machine)

```
                reserve ───────────────► charge(key = order_id)
                  │                           │
                  │                    ┌──────┼──────────────┬──────────────┐
                  │                 201/200   402            500         сеть/тайм-аут
                  │                    │         │             │               │
                  │                   paid    release      retry(500)    GET /payments?order_id=
                  │                        │            │              │             │
                  │                      rejected       │         есть? ─┬──── нет ──┤
                  │                                     │             paid        retry
                  └─ reserve идемпотентен по order_id; повтор всей цепочки безопасен
```

Terminal: `paid`, `rejected`. Всё остальное — `pending` + повтор в фоне. Порядок `reserve → charge`
выбран, чтобы никогда не списать деньги без резерва (разрешения на «возврат денег» в контракте нет).

## Схема БД

```sql
CREATE TABLE IF NOT EXISTS orders (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending','paid','rejected')),
    amount_cents BIGINT NOT NULL,
    items        JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Создаётся при старте (`schema.sql` + `spring.sql.init.mode=always`); база на каждый прогон пустая
(tmpfs). Хранить `charge_id` не обязательно — истину о списании даёт Payment; достаточно статуса.

## Обвязка (Java / Spring)

Базовый пакет `orders`, package-by-feature (04-engineering-standards):

| Класс | Ответственность |
|---|---|
| `OrdersApplication` | `@SpringBootApplication`, старт reaper + воркеров |
| `api/OrdersController` | 3 эндпоинта; `POST /orders` — fast-path + передача в фон |
| `api/dto/*` | `OrderRequest`, `Item`, `OrderCreated`, `Order` (record) |
| `store/OrderRepository` | `JdbcTemplate`: `insertPending`, `transition` (guarded), `findById`, `findPendingIds` |
| `store/DataSourceConfig` | конвертер `DATABASE_URL` (`postgres://…`) → `DataSource` (jdbc url + user/pass) |
| `payment/PaymentClient` | `java.net.http.HttpClient`: `charge(orderId, amount)` (Idempotency-Key, тайм-аут), `lookupCharges(orderId)` (GET) |
| `inventory/InventoryClient` | `reserve(orderId, items)`, `release(orderId)` — идемпотентны |
| `flow/OrderProcessor` | `processOnce(order, budget)` → `PAID` / `REJECTED` / `INCOMPLETE`; общий для fast/slow |
| `flow/BackgroundWorker` | пул воркеров + reaper + `inFlight` |
| `flow/PaymentCircuitBreaker` | `CLOSED/OPEN/HALF_OPEN`; `allow()`, `onSuccess()`, `onTimeoutOrConnError()` |

Исходящий HTTP — один общий `HttpClient` (боб), тайм-ауты задаются на запрос. Логирование:
`order_id`, статус, сумма; без токенов и строк подключения.

## Тайминги (значения по умолчанию — не подгоняются под открытые числа)

| Вызов / параметр | Тайм-аут / значение |
|---|---|
| fast-path: `reserve` | 200 мс |
| fast-path: `charge` | остаток до 900 мс |
| background: `reserve` | 1 с |
| background: `charge` | 5 с |
| background: `lookup` (GET) | 2 с |
| backoff между повторами | 0.5 → 1 → 2 с (кап); дедлайн на заказ ~25 с |
| breaker `openFor` | 5 с |
| reaper tick | 500 мс |
| пул воркеров | 8 |

## Риски и запас

- **Скрытые параметры** (другие доли/задержки/лимиты): breaker и backoff дают запас — за любой
  разумный блэк-хол число запросов к Payment остаётся малым; фоновая обработка укладывается в паузу
  (открыто 30 с) с большим запасом при пуле 8 воркеров и десятках заказов.
- **Конкурентность**: раннер шлёт заказы параллельно (2–10/с). `inFlight` + guarded `UPDATE`
  `WHERE status='pending'` + идемпотентные вызовы исключают и двойную обработку, и двойные списания.
- **Сборка образа**: multi-stage Maven ходит в Maven Central (в CI есть сеть); старт Spring Boot
  ~3 с — в пределах 2-минутного лимита раннера на `/healthz`.
