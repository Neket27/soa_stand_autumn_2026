## Context

Мотивация — в proposal.md (Why). Состояние: сервис Orders (`orders/`) написан,
все S1–S5 проходят. Рецензия выявила 5 идентификаторов, которые вводят в
заблуждение (уровень 1): `OrderFulfiller`/`fulfill`/`maxDeadline`,
`OrderRepository.Order`, `api.dto.Error`, `DataSourceConfig.databaseUrl()`.
Ограничения: контракт `contracts/*.yaml` и HTTP-форма ответов неизменны
(инвариант 7, 03-architecture.md); меняется только `orders/`; любое изменение
поведения — вне скоупа.

## Goals / Non-Goals

**Goals:**
- Идентификаторы описывают то, что делают: по коду без объяснений понятно, что
  происходит.
- Убрать два столкновения: два класса `Order` в разных слоях и `Error`
  против `java.lang.Error`.
- Ноль изменения поведения: S1–S5, тайминги, логика, конфиг — как были.

**Non-Goals:**
- Ренеймы уровня 2 (`reaper`, `allow`, `transition`, `budgetMs`, `deadlineMs`,
  `decideAfterNetwork`, `OrderCreated`) и перенос `OrderStatus` из `flow`.
- Изменение конфигурационных свойств (`orders.worker.deadline-ms` не
  переименовывается — это нарушило бы требование
  «Tunable timing and worker configuration»).

## Decisions

### 1. `OrderFulfiller` → `OrderCharger`, `fulfill` → `charge`

**Решение:** Класс, который гоняет платёжную машину состояний заказа
(charge → retry → reconcile → paid/rejected), называется `OrderCharger`;
входящий метод — `charge(orderId, Duration timeBudget)`.

**Обоснование:**
- «Fulfillment» в e-commerce — склад, упаковка, доставка. Читатель ищет
  логику доставки и не находит её: класс делает только оплату. `Charger`
  честно говорит «про деньги» и зеркалит `PaymentClient.charge` — тот же
  глагол на двух уровнях (вызов API и процесс над заказом).
- Метод `charge` читается как «списать заказ (довести до terminal-статуса) в
  пределах бюджета».

**Альтернативы:**
- `PaymentProcessor` — хуже: «processor» не несёт доменного смысла и
  сливается с `CreateOrderService` как «ещё один сервис».
- `OrderFulfiller` + метод `charge` (частичный ренейм) — отклонено: имя
  класса остаёт вводящим в заблуждение.
- `charge` → `process` — отклонено: «process» не говорит, чем именно
  занимается класс.

### 2. Параметр `maxDeadline` → `timeBudget`

**Решение:** `charge(String orderId, Duration timeBudget)`; внутри
`long deadline = System.currentTimeMillis() + timeBudget.toMillis()` —
абсолютный `deadline` остаётся абсолютным.

**Обоснование:** `Duration`-параметр с именем `maxDeadline` читается как
точка во времени, хотя это длительность. После ренейма в одном методе два
разных понятия — `timeBudget` (длительность) и `deadline` (момент) —
перестают сливаться. Локальная переменная `Duration deadline` в
`BackgroundWorker.workerLoop()` переименовывается в `timeBudget`
(это тоже длительность); поле `deadlineMs` и свойство
`orders.worker.deadline-ms` не трогаются (уровень 2 / требование к
конфигурации).

**Альтернативы:** `maxDuration`, `budget` — хуже: `budget` без привязки к
времени, `maxDuration` не объясняет, что это бюджет попыток.

### 3. `OrderRepository.Order` → `OrderRepository.StoredOrder`

**Решение:** Record строки из БД называется `StoredOrder`; API-DTO
`orders.api.dto.Order` не трогается (это контракт).

**Обоснование:** Два класса `Order` с пересекающимися полями в одном
сервисе — читатель (и IDE) не гарантированно поймут, какой `Order` где.
`StoredOrder` прямо говорит «это строка хранилища», а `Order` остаётся
именем контрактного DTO.

**Альтернативы:** `OrderRow` — допустимо, но «row» — лексика SQL, а record
несёт ещё и маппинг из `ResultSet`; `StoredOrder` описывает роль в системе,
а не формат хранения. Ренейм API-стороны — запрещён: это контракт.

### 4. `api.dto.Error` → `api.dto.ApiError`

**Решение:** Record тела ошибки API — `ApiError`; JSON-форма
`{"error": ..., "message": ...}` не меняется.

**Обоснование:** `Error` затеняет `java.lang.Error`: `new Error(...)` в
`GlobalExceptionHandler` читается как создание JVM-ошибки, а импорты в
файлах, где есть и то и другое, требуют унылых `orders.api.dto.Error`.
`ApiError` — стандартное имя для такого DTO (Spring-конвенции).

**Альтернативы:** `ErrorResponse` — описывает HTTP-ответ целиком, а record
несёт только тело; `ErrorBody` — допустимо, но `ApiError` короче.

### 5. `DataSourceConfig.databaseUrl(String)` → `parseDatabaseUrl(String)`

**Решение:** Статический метод переименовывается в `parseDatabaseUrl`;
параметр `databaseUrl` (строка из `DATABASE_URL`) и `ParsedUrl` остаются.

**Обоснование:** Метод с именем существительного, равным имени параметра,
читается как поле/геттер; глагол `parse` показывает, что строка
преобразуется в `ParsedUrl`.

**Альтернативы:** `convertDatabaseUrl` — хуже: «convert» не указывает
источник/результат; `splitDatabaseUrl` — неточно (строка не
«расщепляется», а разбирается в структуру).

## Инварианты: как они соблюдены после изменения

Требование 03-architecture.md — по каждому инварианту: изменение не трогает
механизм, потому что меняется только имя идентификатора, а не логика,
тайминги и потоки данных.

| Инвариант | Механизм (не меняется) | Влияние ренейма |
|---|---|---|
| Бюджет 1 с на `POST /orders` | Синхронный путь: insert + reserve с `orders.process.budget-ms` (900 мс), дальше `201 pending` + фон | Нет: `CreateOrderService`, тайминги и свойства не трогаются |
| Не больше одного списания | `Idempotency-Key = order_id` на каждом `POST /payments` + идемпотентный ответ 200 на повтор | Нет: `PaymentClient.charge` и заголовок не трогаются |
| Запрос с потерянным ответом → `paid` | После `NETWORK` — реконсиляция `GET /payments?order_id=` (`CHARGED → paid`, `UNKNOWN → pending`), цикл до бюджета | Нет: ветки `NETWORK`/`decideAfterNetwork` не трогаются, меняется только имя класса-носителя (`OrderCharger.charge`) |
| Нагрузка на Payment в блэк-холе | Circuit breaker (`OPEN` на первом `onUnavailable`, HALF_OPEN-проба, окна `openForMs`/`probeWindowMs`) + `reaper` не гонит в OPEN | Нет: `PaymentCircuitBreaker` и `BackgroundWorker` логически не трогаются (в Worker — только имена поля/вызова) |
| `rejected` чистый (нет денег и резерва) | `release()` при `DECLINED`/`NOT_CHARGED` до перехода в `rejected` | Нет: `InventoryClient.release` и порядок вызовов не трогаются |

## Risks / Trade-offs

- [Risk] Пропущенная ссылка на старое имя → не скомпилируется.
  [Mitigation] Компиляция `mvn -q test` после каждого шага; Java
  гарантирует, что «старое имя» не спрячется в рантайм.
- [Risk] Ренейм `OrderFulfiller` → `OrderCharger` — самый широкий
  (класс + метод + параметр + 2 тестовых класса); риск «потерять»
  семантику boolean-возврата метода.
  [Mitigation] Возврат не меняется (`true` — terminal, `false` —
  повторить); Javadoc класса уточняется под имя `charge` («true — заказ в
  terminal-статусе, false — бюджет исчерпан»).
- [Risk] Засталые ссылки на старые имена в main-спеке после архивации.
  [Mitigation] Дельта обновляет все четыре требования, упоминающие
  `OrderFulfiller`/`fulfill`/`Error`; после архива main-spec ссылается на
  актуальные имена.
- [Trade-off] `charge` на двух уровнях (`OrderCharger.charge` — цикл,
  `PaymentClient.charge` — один HTTP-вызов). Принято: одноимённость
  отражает иерархию «процесс над заказом вызывает вызов к платёжке», а
  не скрывает различие — сигнатуры разные.

## Migration Plan

1. `Error` → `ApiError` (DTO + `GlobalExceptionHandler` + тест).
2. `OrderRepository.Order` → `StoredOrder` (record + 2 места в main + тесты).
3. `databaseUrl` → `parseDatabaseUrl` (`DataSourceConfig` + тест).
4. `OrderFulfiller` → `OrderCharger`, `fulfill` → `charge`, `maxDeadline` →
   `timeBudget` (+ `BackgroundWorker` + 2 тестовых класса) — последним,
   на самом широком скопе.
5. Финал: `mvn -q test`, `make scenarios TASK=1`.
Откат: `git revert` — изменения только в `orders/`, контракт и БД не
тронуты, миграция данных не нужна (база стенда пересоздаётся каждый прогон).

## Open Questions

(нет — все имена выбраны в рецензии, альтернативы отклонены в Decisions)
