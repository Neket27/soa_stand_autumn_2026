# Tasks — Replace CopyOnWriteArrayList with ArrayList (simplify-workers-list)

После задачи: `cd orders && mvn -q test`.

## 1. Замена типа коллекции

- [x] 1.1 `InMemoryChargeDispatcher.java`:
  - Удалить `import java.util.concurrent.CopyOnWriteArrayList;`.
  - Добавить `import java.util.ArrayList;` (если ещё нет).
  - `new CopyOnWriteArrayList<>()` → `new ArrayList<>()`.
- [x] 1.2 Проверить, нет ли ссылок на `CopyOnWriteArrayList` в тестах
  (`InMemoryChargeDispatcherTest` и др.).
- [ ] 1.3 `cd orders && mvn -q test` — все зелёные. (Blocked: JDK 21 не доступен в окружении)
- [x] 1.4 `git diff --name-only` — изменён только `orders/`.
