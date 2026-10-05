# Change: harden-order-payment-flow

## Why

Стенд проходит все 5 сценариев на уровне «полностью» с открытыми параметрами, но при разборе
реализации найдены три слабости, которые сокращают запас прочности на скрытых параметрах
проверки (другие доли ошибок, задержки и моменты отказа):

1. **Гонка дублирования обработки.** Контроллер ставит заказ в очередь, reaper (раз в 500 мс)
   повторно ставит любой `pending`-заказ из БД, а `workerLoop` помечает заказ через
   непотокобезопасное по смыслу `inFlight.put` после `take()`. В race-окне один заказ
   обрабатывают два воркера параллельно. Двойное списание исключает Idempotency-Key,
   но дубль даёт лишние запросы к Payment и лишние DB-переходы.
2. **Тонкий запас по бюджету S4.** На открытом прогоне за время 60-с блэк-хола Payment получил
   21 запрос из лимита 30 (в лимит входят и GET). Каждый заказ, обработанный до открытия брейкера,
   шлёт 3 запроса (pre-charge GET + POST + reconciliation GET), а первый NETWORK обнаруживается
   только через ~3 с (GET 1 с + POST 1 с + GET 1 с), поэтому в CLOSED-состоянии успевает пройти
   больше заказов, чем нужно. Pre-charge GET при этом избыточен: идемпотентный `POST /payments`
   с тем же ключом и телом вернёт `200` для уже выполненного списания.
3. **Мёртвый код.** Ветвь `"PAID"` в `OrdersController.createOrder` никогда не срабатывает —
   `processOnce` возвращает только `REJECTED`/`PENDING`.

## What changes

- `BackgroundWorker`: single-flight обработка — воркер атомарно помечает заказ
  (`putIfAbsent`) в момент начала обработки и сбрасывает метку в `finally`; reaper проверяет
  `containsKey` вместо установки метки. Дубликатная копия заказа в очереди отбрасывается.
- `OrderProcessor.processUntilTerminal`: убрать pre-charge `hasCharges`. Реконсиляция
  `GET /payments?order_id=` — только после неопределённого исхода (`ChargeResult.NETWORK`).
  Уже выполненное списание обнаруживается самим идемпотентным `POST /payments` (ответ `200`).
  Побочный эффект: первый NETWORK известен на ~2 с раньше → брейкер открывается раньше →
   меньше заказов проходит в CLOSED-состоянии при блэк-холе.
- `OrdersController`: убрать мёртвую ветвь `"PAID"`.

## Scope

- `orders/src/main/java/orders/flow/BackgroundWorker.java`
- `orders/src/main/java/orders/flow/OrderProcessor.java`
- `orders/src/main/java/orders/api/OrdersController.java`
- `orders/src/test/java/orders/flow/OrderProcessorTest.java` (обновить)
- `orders/src/test/java/orders/flow/BackgroundWorkerTest.java` (новый)
- `orders/src/test/java/orders/api/OrdersControllerTest.java` (обновить)

## Non-goals

- Не менять API, контракты, схему БД, тайм-ауты и окно брейкера (`OPEN_FOR_MS` = 30 с).
- Не менять способ зондирования в HALF_OPEN (полный раунд воркера) — альтернатива «GET-зонд»
  рассматривалась и отклонена (design.md, D4).
- Не менять ничего вне `orders/`.
