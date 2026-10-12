# Proposal — Перевод комментариев main-кода Orders на русский (russify-main-comments)

## Why

Основной код сервиса написан с английскими комментариями, при этом весь проектный
контекст, документация курса и коммуникация — на русском. Перевод комментариев
`orders/src/main` повышает читаемость для команды и согласованность с контекстом.
Поведение **не** меняется: только текст комментариев, ноль изменений кода.

## What Changes

- Перевод всех комментариев (Javadoc + inline) в 11 Java-файлах
  `orders/src/main/java/**` с английского на русский (~102 строки).
- Перевод комментариев в `orders/src/main/resources/schema.sql` (2 строки)
  и `orders/Dockerfile` (2 строки).
- На английском остаются: идентификаторы (классы, методы, поля), ключи конфигурации,
  HTTP-статусы, значения статусов (`pending`/`paid`/`rejected`), SQL-литералы и
  устоявшиеся термины (`Idempotency-Key`, `POST-first`, circuit breaker, blackhole,
  reconciliation, reaper). Переводится только несущий текст.
- **BREAKING** — нет.

## Capabilities

### New Capabilities

(нет)

### Modified Capabilities

(нет — spec-level поведение не меняется, в `.openspec.yaml` заявлен `skip_specs: true`)

## Scope

- `orders/src/main/java/**` — 11 файлов с комментариями:
  `flow/OrderCharger.java`, `flow/OrderStatus.java`, `flow/InMemoryChargeDispatcher.java`,
  `flow/PaymentCircuitBreaker.java`, `flow/CreateOrderService.java`,
  `payment/PaymentClient.java`, `api/OrderQueryService.java`, `api/OrderNotFoundException.java`,
  `api/dto/OrderRequest.java`, `store/DataSourceConfig.java`, `store/OrderRepository.java`.
- `orders/src/main/resources/schema.sql`, `orders/Dockerfile`.
- Артефакты change в `openspec/changes/russify-main-comments/` (plan-only, в PR курса не попадает).

Новых зависимостей **нет**.

## Non-Goals

- Тестовый код `orders/src/test/**` — не трогать (решено заранее).
- `pom.xml`, `application.properties`, `contracts/` — комментариев нет / контракт read-only.
- Перевод идентификаторов, сообщений логирования и исключений — нет, они часть
  наблюдаемого поведения.
- Любые изменения кода, переименования, рефакторинг стиля — нет; только текст
  существующих комментариев.
- Всё вне `orders/` — запрещено CI.

## Impact

- **Поведение**: без изменений; комментарии не влияют на байт-код и рантайм.
- **Сборка**: `cd orders && mvn -q test` обязана остаться зелёной (проверка, что
  правки только в комментариях и код не задет).
- **Сценарии раннера S1–S5**: не затрагиваются.
- **CI**: diff ограничен `orders/` (и plan-only артефактами change).
