# Change: contract-dto-responses

## Why

Контроллер основного API вручную собирает `ResponseEntity` в каждом ветвлении:
`POST /orders` — `ResponseEntity.status(201).body(...)` в обеих ветках,
`GET /orders/{id}` — null-чек и ручная сборка `404` в теле метода. Контракт
эндпоинтов описан record-DTO (`OrderCreated`, `Order`, `Error`), но способ
доставки ответа не соответствует идиоматике Spring: фиксированный статус лучше
закрепить аннотацией, а «заказа нет» — доменным исключением через
`GlobalExceptionHandler` (создан в `2026-10-08-refactor-order-flow`).

Исправление тезиса «ResponseEntity — антипаттерн»: это верно только для
эндпоинтов с фиксированным успехом и обрабатываемых ошибок — основного API.
Для `GET /healthz` (смена 200/503, тело не нужно) `ResponseEntity<Void>` —
правильный инструмент и остаётся без изменений.

Побочный баг compliance: `contracts/orders.yaml` для `404` на `GET /orders/{id}`
объявляет JSON-тело схемы `Error`, а код возвращает пустое тело. Раннер сценариев
404 не порождает (запрашивает только созданные id), поэтому тело можно безопасно
добавить — сервис станет соответствовать контракту полностью.

## What changes

### 1. `POST /orders` — DTO + `@ResponseStatus` (design D1)

`createOrder` возвращает `OrderCreated` напрямую; `201` закреплён аннотацией
`@ResponseStatus(HttpStatus.CREATED)`. Ветвей на статус больше нет: контракт
отвечает 201 и для `pending`, и для `rejected`. `400` остаётся за
`GlobalExceptionHandler` (валидация бросает исключение до тела метода).

### 2. `GET /orders/{id}` — query use case + доменное исключение (design D2)

Контроллер не делает ни null-чек, ни маппинг, ни сборку 404:

- `OrderQueryService` (новый, `orders.api`) — read-use case:
  `getOrder(id)` сам обращается к `OrderRepository`, мапит `Order`-DTO и
  бросает `OrderNotFoundException` (новый, `orders.api`), когда заказа нет.
- Контроллер — одна строка: `return orderQueryService.getOrder(id)`.
- `GlobalExceptionHandler` мапит исключение на `404` с телом
  `{"error":"not_found","message":"order not found"}` — то, что контракт
  объявляет, но код никогда не возвращал.

### 3. `GET /healthz` — без изменений (design D3)

`ResponseEntity<Void>` с 200/503 — легитимный случай использования
`ResponseEntity` (сменный статус, нет тела): DTO-ответ тут невозможен по
смыслу.

## Scope

- `orders/src/main/java/orders/api/OrdersController.java`
- `orders/src/main/java/orders/api/OrderQueryService.java` (новый)
- `orders/src/main/java/orders/api/OrderNotFoundException.java` (новый)
- `orders/src/main/java/orders/api/GlobalExceptionHandler.java`
- `orders/src/test/java/orders/api/OrderQueryServiceTest.java` (новый)
- `orders/src/test/java/orders/api/OrdersControllerTest.java`
- `orders/src/test/java/orders/api/GlobalExceptionHandlerTest.java` (новый)

## Non-goals

- `GET /healthz` — `ResponseEntity<Void>` остаётся (design D3).
- Формы record-DTO (`OrderCreated`, `Order`, `Error`) — уже контрактные,
  не меняются; JSON-тела byte-идентичны.
- Клиенты Payment/Inventory (`PaymentClient`, `InventoryClient`) — это не API
  сервиса, там `ResponseEntity` не используется.
- Изменения поведения при отказах, брейкера, идемпотентности.
- Изменения вне `orders/`.
