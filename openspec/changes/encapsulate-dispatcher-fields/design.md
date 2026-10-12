# Design — Encapsulate dispatcher fields

## Context

`InMemoryChargeDispatcher` (orders/flow/InMemoryChargeDispatcher.java):

```java
final BlockingQueue<String> queue = new LinkedBlockingQueue<>();   // line 30
final Map<String, Boolean> inFlight = new ConcurrentHashMap<>();   // line 31
private final List<Thread> workers = new ArrayList<>();            // line 32
```

`queue` и `inFlight` — package-private (нет модификатора доступа).
Тесты в том же пакете (`orders.flow`) используют прямой доступ.

## Решение

1. **Поля** — `private`:
   ```java
   private final BlockingQueue<String> queue = new LinkedBlockingQueue<>();
   private final Map<String, Boolean> inFlight = new ConcurrentHashMap<>();
   ```

2. **Геттеры** — package-private (без модификатора), возвращают фактические ссылки:
   ```java
   BlockingQueue<String> getQueue() { return queue; }
   Map<String, Boolean> getInFlight() { return inFlight; }
   ```
   Package-private достаточно: тесты в `orders.flow`, наружный доступ не нужен.

3. **Тесты** — заменить прямой доступ на геттеры:
   - `worker.queue.isEmpty()` → `worker.getQueue().isEmpty()`
   - `worker.queue.toArray()` → `worker.getQueue().toArray()`
   - `worker.inFlight.isEmpty()` → `worker.getInFlight().isEmpty()`
   - `worker.inFlight.put(...)` → `worker.getInFlight().put(...)`
   - `worker.inFlight.containsKey(...)` → `worker.getInFlight().containsKey(...)`

## Почему не defensive copy

Геттеры возвращают живую ссылку, а не копию. Для стенда это допустимо:
- Конкуренция между тестом и воркерами уже есть (тесты мокают `charge` с
  `CountDownLatch`), defensive copy не решает проблему.
- Тестам нужен реальный текущий состояния (например, `inFlight.put` для
  симуляции «заказ уже обрабатывается»).

## Обязательные вопросы (по rules)

- **Бюджет 1 с**: не затронут.
- **Двойное списание**: не затронут.
- **Заказ с потерянным ответом → paid**: не затронут.
- **Нагрузка на Payment в блэк-холе**: не затронута.
