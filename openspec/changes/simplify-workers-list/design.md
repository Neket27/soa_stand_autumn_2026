# Design — Replace CopyOnWriteArrayList with ArrayList

## Context

`InMemoryChargeDispatcher` (orders/flow/InMemoryChargeDispatcher.java) хранит
созданные в конструкторе воркеры в `CopyOnWriteArrayList`:

```java
private final List<Thread> workers = new CopyOnWriteArrayList<>();
```

Все `add` — в `startWorkers()` (вызывается из конструктора, line 45).
Единственное чтение — в `@PreDestroy shutdown()` (line 130).

## Решение

Заменить `CopyOnWriteArrayList` на `ArrayList`:

```java
private final List<Thread> workers = new ArrayList<>();
```

Удалить import `java.util.concurrent.CopyOnWriteArrayList`.

## Почему безопасно

Spring-контейнер гарантирует:
1. Конструктор бина завершается **до** того, как бин становится доступен другим бинам.
2. `@PreDestroy` вызывается **после** полной инициализации (все `@PostConstruct`,
   injection, `BeanPostProcessor` завершены).

Следовательно, все `workers.add(t)` завершаются до первого `workers.forEach(...)`.
Одновременная запись и чтение исключены жизненным циклом контейнера.

## Обязательные вопросы (по rules)

- **Бюджет 1 с**: не затронут (никаких новых/убранных вызовов).
- **Двойное списание**: не затронут (charge-путь не меняется).
- **Заказ с потерянным ответом → paid**: не затронут.
- **Нагрузка на Payment в блэк-холе**: не затронута.

## Trade-offs

- `ArrayList` без синхронизации: если кто-то в будущем начнёт менять список
  после конструктора (например, ресайз воркеров), потребуется вернуть
  синхронизацию. Для стенда (фиксированное число воркеров из config) это
  неактуально.
