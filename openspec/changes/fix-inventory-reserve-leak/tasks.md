# Tasks — Durable release резерва (fix-inventory-reserve-leak)

После каждой задачи: `cd orders && mvn -q test`; после Phase 2–4 spot-чек
`make scenarios ONLY=S1 WAIT=5`; финал — `make scenarios TASK=1` (пауза 30 с).

## 0. Phase 0 — завершить rename, вернуть компиляцию (прекурсор)

- [x] 0.1 Класс `BackgroundWorker` → `InMemoryChargeDispatcher`
  (`orders/src/main/java/orders/flow/InMemoryChargeDispatcher.java`): имя класса,
  логгер, имя потока воркера (`bg-worker-` → `charge-worker-`), сообщения логов.
- [x] 0.2 Тест: `class BackgroundWorkerTest` → `InMemoryChargeDispatcherTest`
  (`orders/src/test/java/orders/flow/InMemoryChargeDispatcherTest.java`).
- [x] 0.3 Ссылки: `CreateOrderService` — поле/конструктор
  `BackgroundWorker backgroundWorker` → `InMemoryChargeDispatcher`.
- [x] 0.4 Проверка: `cd orders && mvn -q test` зелёный;
  `make scenarios ONLY=S1 WAIT=5` — PASS (поilaku 1:1, переименование не меняет логики).

## 1. Phase 1 — схема + repository

- [x] 1.1 `schema.sql`: в `CREATE TABLE orders` колонка
  `release_pending BOOLEAN NOT NULL DEFAULT FALSE` + для живых БД
  `ALTER TABLE orders ADD COLUMN IF NOT EXISTS release_pending BOOLEAN NOT NULL DEFAULT FALSE`.
- [x] 1.2 `OrderRepository.transition(String id, OrderStatus to, boolean releasePending)` —
  `UPDATE orders SET status = ?, release_pending = ? WHERE id = ? AND status = 'pending'`;
  старая `transition(id, to)` делегирует с `false`.
- [x] 1.3 `findReleasePendingIds()` —
  `SELECT id FROM orders WHERE status = 'rejected' AND release_pending`.
- [x] 1.4 `clearReleaseDuty(String id)` —
  `UPDATE orders SET release_pending = FALSE WHERE id = ? AND status = 'rejected' AND release_pending`.
- [x] 1.5 Тесты `OrderRepositoryTest` (H2): transition с duty=true/false (CAS по-прежнему
  один раз), findReleasePendingIds возвращает только rejected+duty, clearReleaseDuty
  не трогает paid/duty=false.

## 2. Phase 2 — OrderCharger: duty по результату release

- [x] 2.1 Ветка DECLINED (основной switch): `boolean released = release(orderId);`
  → `transition(orderId, REJECTED, !released) > 0`.
- [x] 2.2 Ветка SERVER_ERROR → retry → DECLINED: то же.
- [x] 2.3 `decideAfterNetwork` NOT_CHARGED: то же (duty = !released).
- [x] 2.4 Тесты `OrderChargerTest` (мок InventoryClient): decline + release ok →
  transition с duty=false; decline + release fail → duty=true; NOT_CHARGED +
  release fail → duty=true; paid-путь — duty не передаётся (false).
- [x] 2.5 Spot-чек: `make scenarios ONLY=S1 WAIT=5` — PASS (rejected: резерв снят).

## 3. Phase 3 — CreateOrderService: неопределённый reserve → rejected + duty

- [x] 3.1 `reserve()==false` → `transition(orderId, REJECTED, true)` (без inline-DELETE
  на HTTP-пути).
- [x] 3.2 Тест `CreateOrderServiceTest`: reserve false → REJECTED + duty=true,
  dispatch не вызывается; reserve true → PENDING, duty не затрагивается.
- [x] 3.3 Spot-чек: `make scenarios ONLY=S1 WAIT=5` — PASS.

## 4. Phase 4 — release-sweep в reaper

- [x] 4.1 `InMemoryChargeDispatcher`: конструктор — добавить `InventoryClient`.
- [x] 4.2 `reaper()`: сначала `sweepReleases()` (всегда), затем гейт
  `if (breaker OPEN) return;` **только** для pending-sweep.
- [x] 4.3 `sweepReleases()`: по `findReleasePendingIds()` — `release(id, 1s)`;
  успех → `clearReleaseDuty(id)`; неудача → duty остаётся (повтор на следующем свипе).
  Лог: `order=… release sweep: duty cleared/kept`.
- [x] 4.4 Тесты `InMemoryChargeDispatcherTest`: (a) rejected+duty → sweep шлёт DELETE,
  204 → duty снят; (b) DELETE fail → duty не снят; (c) при breaker OPEN release-sweep
  идёт, pending-sweep — нет; (d) paid-заказ sweep не трогает.
- [x] 4.5 Spot-чек: `make scenarios ONLY=S1 WAIT=5` — PASS.

## 5. Phase 5 — финальная верификация

- [x] 5.1 `cd orders && mvn -q test` — все зелёные.
- [x] 5.2 `make scenarios TASK=1` (пауза 30 с, без ONLY) — S1–S5 PASS,
  уровень «полностью»; в `results/task_1.txt` для S1 — «резервов … сняты 3»
  (rejected чистые), 0 двойных списаний, 0 5xx.
- [x] 5.3 `git status`/`git diff --name-only` — изменены только `orders/`
  (+ артефакты openspec/changes, plan-only).
