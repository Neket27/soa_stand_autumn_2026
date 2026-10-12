# Tasks — Encapsulate dispatcher fields (encapsulate-dispatcher-fields)

После задачи: `cd orders && mvn -q test`.

## 1. Поля + геттеры

- [x] 1.1 `InMemoryChargeDispatcher.java`:
  - `final BlockingQueue<String> queue` → `private final BlockingQueue<String> queue`.
  - `final Map<String, Boolean> inFlight` → `private final Map<String, Boolean> inFlight`.
  - Добавить package-private геттеры:
    ```java
    BlockingQueue<String> getQueue() { return queue; }
    Map<String, Boolean> getInFlight() { return inFlight; }
    ```
- [x] 1.2 Тесты `InMemoryChargeDispatcherTest`:
  - `worker.inFlight` → `worker.getInFlight()` (5 мест: lines 45, 53, 70, и др.).
  - `worker.queue` → `worker.getQueue()` (3 места: lines 58, 69, 111).
- [ ] 1.3 `cd orders && mvn -q test` — все зелёные. (Blocked: JDK 21 не доступен)
- [x] 1.4 `git diff --name-only` — изменены только файлы в `orders/`.
