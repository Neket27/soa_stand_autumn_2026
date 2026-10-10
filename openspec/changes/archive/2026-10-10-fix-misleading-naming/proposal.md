## Why

Рецензия кода `orders/` выявила пять неймингов уровня 1, которые активно вводят
читателя в заблуждение: параметр-длительность с именем `maxDeadline`, два разных
класса `Order` в разных слоях, DTO `Error`, затеняющий `java.lang.Error`,
класс `OrderFulfiller` (в e-commerce «fulfillment» — это склад и доставка, а
класс занимается только оплатой) и метод `databaseUrl()` с именем, совпадающим
с именем параметра. Цель: по коду без объяснений должно быть понятно, что
происходит. Поведение сервиса при этом не меняется.

## What Changes

- `OrderFulfiller` → `OrderCharger`; метод `fulfill(orderId, Duration maxDeadline)`
  → `charge(orderId, Duration timeBudget)` — параметр стал честным: это
  время-бюджет попыток, а не абсолютный дедлайн.
- `OrderRepository.Order` (record) → `OrderRepository.StoredOrder` — исчезает
  столкновение с API-DTO `orders.api.dto.Order`.
- `orders.api.dto.Error` → `orders.api.dto.ApiError` — убирается затенение
  `java.lang.Error`. JSON-тело ответа (`{"error": ..., "message": ...}`) не меняется.
- `DataSourceConfig.databaseUrl(String)` → `DataSourceConfig.parseDatabaseUrl(String)`
  — метод читается как действие, а не как поле.
- Тесты переименовываются вместе с классами (`OrderFulfillerTest` →
  `OrderChargerTest` и т.д.).
- Обновляются ссылки в требованиях спеки `orders`, упоминающие старые имена
  (поведение требований не меняется).

## Capabilities

### New Capabilities

(нет)

### Modified Capabilities

- `orders`: четыре требования обновляются под переименованные идентификаторы —
  «State machine logging», «Single path for order status changes»,
  «Validation exception handling», «Main API responses are contract DTOs».
  Изменения только в текстах (имена классов/методов), поведение не меняется.

## Scope

- Только `orders/src/main/java/**` и `orders/src/test/java/**`: идентификаторы и
  ссылки на них.
- Speка: `openspec/changes/fix-misleading-naming/specs/orders/spec.md` (дельта).

## Non-Goals

- Ренеймы уровня 2 рецензии (`reaper()`, `allow()`, `transition()`, `budgetMs`,
  `deadlineMs`, `decideAfterNetwork`, `OrderCreated`) — отдельное изменение.
- Любое изменение логики, таймингов, конфигурационных свойств
  (`orders.worker.deadline-ms` остаётся), SQL и HTTP-контракта
  (`contracts/*.yaml`, JSON-поля, статусы `paid/pending/rejected`).
- Изменения вне `orders/` (и этого change-каталога).

## Impact

- **Код**: main — `flow/OrderFulfiller.java` (+`BackgroundWorker.java`),
  `store/OrderRepository.java` (+`OrderQueryService.java`), `api/dto/Error.java`
  (+`GlobalExceptionHandler.java`), `store/DataSourceConfig.java`; test —
  `OrderFulfillerTest`, `BackgroundWorkerTest`, `OrderRepositoryTest`,
  `OrderQueryServiceTest`, `GlobalExceptionHandlerTest`, `DataSourceConfigTest`.
- **API / БД / конфиг**: не затрагиваются.
- **Тесты**: `cd orders && mvn -q test`; итог — `make scenarios TASK=1` (все
  S1–S5 `PASS`).
