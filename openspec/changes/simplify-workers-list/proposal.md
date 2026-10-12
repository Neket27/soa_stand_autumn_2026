# Proposal — Replace CopyOnWriteArrayList with ArrayList (simplify-workers-list)

## Why

`InMemoryChargeDispatcher` хранит созданную в конструкторе коллекцию воркеров в
`CopyOnWriteArrayList` (`orders/flow/InMemoryChargeDispatcher.java:32`).
`CopyOnWriteArrayList` нужен, когда коллекция читается и пишется **одновременно**
с разных потоков. Здесь это не так:

- Все `workers.add(t)` выполняются в конструкторе (`startWorkers`, line 45) —
  однопоточный, до того как Spring отдаёт бин контейнеру.
- Единственное чтение — `workers.forEach(Thread::interrupt)` + `workers.size()`
  в `@PreDestroy shutdown()` (line 130), которое вызывается после полной
  инициализации бина.

Гонки нет. `CopyOnWriteArrayList` — защитная страховка без функциональной
необходимости; дизайн-док change `harden-observability` сам признаёт:
«Запись в список только при старте (1 раз), чтение при shutdown (1 раз). Безвредно».

## What Changes

- `CopyOnWriteArrayList` → `ArrayList` в `InMemoryChargeDispatcher`.
- Удалить import `java.util.concurrent.CopyOnWriteArrayList`.

Поведение **byte-identical**: никаких изменений в логике, контрактах, сценариях.

## Capabilities

### New Capabilities

(нет)

### Modified Capabilities

(нет — поведение не меняется, это внутренний рефакторинг)

## Scope

- `orders/src/main/java/orders/flow/InMemoryChargeDispatcher.java` (1 строка: тип поля, 1 строка: import).
- `orders/src/test/java/orders/flow/InMemoryChargeDispatcherTest.java` — только если тест
  ссылок на тип `CopyOnWriteArrayList` (не ожидается).

## Non-Goals

- Замена `ArrayList` на `List.of` / `Collections.unmodifiableList` — возможно, но
  не в рамках этого change (минимальный скоуп).
- Изменение логистики lifecycle (перенос `startWorkers` в `@PostConstruct` и т.д.).

## Impact

- **Код**: 1 файл, 2 строки (import + тип поля).
- **Контракты, схемы, сценарии**: без изменений.
- **Тесты**: существующие — без изменений; `mvn test` остаётся зелёным.
