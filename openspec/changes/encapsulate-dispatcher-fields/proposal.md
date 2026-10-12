# Proposal — Encapsulate dispatcher fields (encapsulate-dispatcher-fields)

## Why

`InMemoryChargeDispatcher` имеет два package-private поля:

```java
final BlockingQueue<String> queue = new LinkedBlockingQueue<>();
final Map<String, Boolean> inFlight = new ConcurrentHashMap<>();
```

Они доступны любому классу в пакете `orders.flow` — нарушает инкапсуляцию.
Тесты (`InMemoryChargeDispatcherTest`, тот же пакет) используют прямой доступ:
`worker.queue.isEmpty()`, `worker.inFlight.put(...)`, `worker.queue.toArray()`.

## What Changes

- `queue` и `inFlight` — `private`.
- Добавить package-private геттеры `getQueue()` и `getInFlight()`,
  возвращающие фактические ссылки (тесты в том же пакете).
- Тесты: `worker.queue` → `worker.getQueue()`, `worker.inFlight` → `worker.getInFlight()`.

Поведение **byte-identical**: только видимость и способ доступа.

## Capabilities

### New Capabilities

(нет)

### Modified Capabilities

(нет — чистый рефакторинг, поведение не меняется)

## Scope

- `orders/src/main/java/orders/flow/InMemoryChargeDispatcher.java`
- `orders/src/test/java/orders/flow/InMemoryChargeDispatcherTest.java`

## Non-Goals

- Return defensive copies / unmodifiable views — избыточно для стенда;
  тестам нужен прямой доступ к живому состоянию.
- Изменение `workers` (уже `private`).

## Impact

- **Код**: 2 файла, ~8 строк.
- **Контракты, схемы, сценарии**: без изменений.
- **Тесты**: обновляются на геттеры, логика не меняется.
