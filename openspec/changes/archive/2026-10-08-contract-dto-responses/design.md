# Design: contract-dto-responses

Реконструкция слоя доставки ответов основного API. Поведение и JSON-тела
не меняются (кроме тела 404 — оно появляется, хотя контракт объявлял его
всегда; раннер 404 не порождает, см. D2).

## Текущая форма

```java
@PostMapping("/orders")
public ResponseEntity<OrderCreated> createOrder(@Valid @RequestBody OrderRequest r) {
    CreateResult result = createOrderService.createOrder(r);
    return ResponseEntity.status(201).body(new OrderCreated(...));   // 201 в коде метода
}

@GetMapping("/orders/{id}")
public ResponseEntity<Order> getOrder(@PathVariable String id) {
    OrderRepository.Order order = orderRepository.findById(id);
    if (order == null) {
        return ResponseEntity.notFound().build();                    // 404 без тела —
    }                                                                // контракт ждёт Error
    return ResponseEntity.ok(new Order(...));
}
```

Цель — контроллер описывает контракт **типом возвращаемого значения**, а не
HTTP-обвязкой: статус успеха закреплён аннотацией, «заказа нет» — исключением
в глобальном обработчике.

## D1. `POST /orders`: `OrderCreated` + `@ResponseStatus(HttpStatus.CREATED)`

Контракт (`contracts/orders.yaml`) отвечает `201` и для `pending`, и для
`rejected` — статус фиксирован для всех успешных возвратов, значит он не
данный метода, а свойство контракта эндпоинта. Аннотация
`@ResponseStatus(HttpStatus.CREATED)` переводит его в декларативный слой:
метод возвращает `OrderCreated`, Spring ставит 201.

`400` на некорректное тело не затрагивается: `MethodArgumentNotValidException`
бросается до тела метода (интерцептор валидации), `@ResponseStatus` не
применяется к выброшенному исключению — его ловит `GlobalExceptionHandler`.

Почему не `200` + DTO: контракт явно требует 201, раннер проверяет статус —
`@ResponseStatus(CREATED)` сохраняет контракт без ручного `status(201)`.

## D2. `GET /orders/{id}`: `OrderQueryService` + `OrderNotFoundException`

Решение «заказа нет» и маппинг строк БД в API-DTO — за read-use case, а не за
контроллером:

- `OrderQueryService` (`@Service`, `orders.api`, `@RequiredArgsConstructor` —
  один final-полю `OrderRepository`, без `@Value`):

  ```java
  public Order getOrder(String id) {
      OrderRepository.Order order = orderRepository.findById(id);
      if (order == null) {
          throw new OrderNotFoundException(id);
      }
      return new Order(order.id(), order.status(), order.amountCents());
  }
  ```

- `OrderNotFoundException extends RuntimeException` — новый класс в
  `orders.api` (это API-ограничение: «нет ресурса для ответа», не ошибка
  flow-слоя и не ошибка хранилища).
- Контроллер: `public Order getOrder(@PathVariable String id) {
  return orderQueryService.getOrder(id); }` — без null-чека, маппинга и
  404.
- `GlobalExceptionHandler` получает
  `@ExceptionHandler(OrderNotFoundException.class)` → `404` + тело
  `{"error":"not_found","message":"order not found"}`.

**Почему не бросает репозиторий.** `OrderRepository.findById` используют
фоновые пути (`OrderFulfiller`), где null — легитимное состояние («заказ
удалён, остановить обработку»), а не ошибка; бросок из репозитория либо
сломал бы фон, либо заставил бы его ловить API-исключение. Кроме того,
«нет строки в таблице» — факт хранилища, а «нет ресурса, отвечаем 404» —
правило API-слоя: `store` не должен знать про исключения `api`.

**Тело 404 — починка compliance.** Контракт для 404 объявляет
`content: application/json` со схемой `Error` (поля `error`/`message` без
`required`); текущий код возвращает пустое тело, т.е. ответ не соответствует
объявленной медиаструктуре. Сценарии S1–S5 404 не порожают (раннер
запрашивает только созданные id), поэтому появление тела — безопасное
улучшение: основной сценарий «unknown order is 404» из основной спеки
проверяет только статус и остаётся истинным.

Почему не ручное `ResponseEntity.notFound().body(...)`: это тот же паттерн,
который убираем (ветвление на HTTP-статус в методе). Исключение + обработчик
— единый путь для всех ошибок API (уже так у 400/500).

## D3. `GET /healthz` — фиксированная точка

`ResponseEntity<Void>` с 200/503 остаётся. Критерий: «есть ли смысл описать
этот ответ DTO-типом + аннотацией?» Нет — статус зависит от времени
(БД жива/не жива), тела нет; `ResponseEntity` для сменных статусов —
назначение, а не антипаттерн. Тезис «ResponseEntity всегда плохо» в change
не закреплён сознательно: он неверен для healthz.

## Обязательные вопросы дизайна

**Как соблюдается бюджет 1 с.** Неизменно: в sync-путь (`insert` + `reserve`)
не добавляется ни одного вызова; меняется только тип возвращаемого значения и
способ установки 201 (аннотация обрабатывается Spring после возврата метода).

**Как исключено двойное списание.** Неизменно: `OrderFulfiller`,
`PaymentClient`, брейкер и идемпотентность не трогаются; слой доставки
ответов не участвует в charge-машине.

**Как заказ с потерянным ответом станет paid.** Неизменно: путь
pending → reaper → `OrderFulfiller` не меняется; `GET /orders/{id}` для такого
заказа по-прежнему возвращает 200 с текущим статусом.

**Как ограничивается нагрузка на Payment в блэк-холе.** Неизменно:
`BackgroundWorker`, пул 8, breaker-gate и reaper не трогаются; на ответный
путь HTTP-запросов к Payment не влияет.

## Риски

- Standalone MockMvc не регистрирует `@ControllerAdvice` автоматически —
  тесты добавят `.setControllerAdvice(new GlobalExceptionHandler())`, иначе
  бросок `OrderNotFoundException` в тесте даст 500.
- `@ResponseStatus(CREATED)` + `@Valid`: порядок гарантирован Spring
  (валидация → метод → аннотация статуса); покрывается существующими
  тестами 400/201 в `OrdersControllerTest`.
