# Tasks: harden-order-payment-flow

## Task 1: Single-flight обработка в BackgroundWorker

**Files:**
- `orders/src/main/java/orders/flow/BackgroundWorker.java`

**Changes:**
1. `workerLoop`: обработка — только если `inFlight.putIfAbsent(orderId, Boolean.TRUE) == null`;
   `inFlight.remove(orderId)` — в `finally`. Дубликатная копия из очереди отбрасывается
   (лог-строка debug/info достаточно).
2. `reaper`: заменить `putIfAbsent(...) == null → queue.offer` на
   `if (!inFlight.containsKey(orderId)) queue.offer(orderId)` — reaper больше не ставит метку.

**Tests:**
- Новый `orders/src/test/java/orders/flow/BackgroundWorkerTest.java` (Jupiter + Mockito,
  паттерн как в `OrderProcessorTest`):
  - заказ, поставленный в очередь дважды, обрабатывается ровно один раз
    (управляемый `OrderProcessor`, который блокируется в `processUntilTerminal`, пока тест
    ставит второй дубль в очередь);
  - reaper не ставит в очередь заказ, уже в inFlight;
  - reaper повторно ставит `pending`-заказ из БД, если он не в inFlight.
  - Конструктор `BackgroundWorker` стартует 8 фоновых потоков — в тесте достаточно одного
    воркера: вынести число воркеров в конструктор (package-private или параметр с дефолтом 8).

**Verification:**
- `cd orders && mvn -q test` — зелёный.
- `make scenarios ONLY=S4 WAIT=5` — PASS (заказов с 2+ списаниями: 0).

## Task 2: Убрать pre-charge reconciliation GET в OrderProcessor

**Files:**
- `orders/src/main/java/orders/flow/OrderProcessor.java`
- `orders/src/test/java/orders/flow/OrderProcessorTest.java`

**Changes:**
1. `processUntilTerminal`: удалить блок pre-charge `hasCharges` (проверка до `charge`);
   первый запрос раунда — `charge`. Реконсилирующий `hasCharges` — только в ветке `NETWORK`.
2. Обновить тесты:
   - `processUntilTerminal_networkWithHasCharges_returnsTrue`: `verify(paymentClient, times(2))`
     → `times(1)` (реконсиляция только после NETWORK).
   - `processUntilTerminal_hasChargesFirst_returnsTrue` (pre-charge находил списание) —
     переписать в поведение «уже выполненное списание обнаруживается идемпотентным POST»:
     mock `charge` → `CHARGED` (заглушка отвечает 200 тем же списанием), `hasCharges` —
     `never()`, итог `paid`.
   - Добавить тест: `charge` → `NETWORK`, `hasCharges` → `false` → `rejected` + `release`
     + брейкер OPEN (уже есть как `processUntilTerminal_networkNoHasCharges_opensBreaker` —
     убедиться, что после изменения `verify(hasCharges, times(1))`).
   - Остальные тесты, штубящие `hasCharges` на `false`, оставить: штуб больше не вызывается
     до charge и не ломает поведение.

**Verification:**
- `cd orders && mvn -q test` — зелёный.
- `make scenarios ONLY=S2,S3,S4,S5 WAIT=5` — все PASS; в журнале S4 «запросов к Payment за
  время отказа» заметно меньше 21 (ожидаемо 6–16).

## Task 3: Убрать мёртвую ветвь "PAID" в OrdersController

**Files:**
- `orders/src/main/java/orders/api/OrdersController.java`
- `orders/src/test/java/orders/api/OrdersControllerTest.java`

**Changes:**
1. `createOrder`: удалить ветвь `if ("PAID".equals(result))` — `processOnce` возвращает только
   `REJECTED`/`PENDING`.
2. `OrdersControllerTest.createOrder_paid_returns201Paid` — удалить (ветвь недостижима);
   поведение «резерв взят → 201 pending + enqueue» уже покрыто
   `createOrder_incomplete_returns201Pending`.

**Verification:**
- `cd orders && mvn -q test` — зелёный.
- `make scenarios ONLY=S1 WAIT=5` — PASS.

## Task 4: Полный прогон и фиксация результата

**Files:** — (только прогон и журнал)

**Changes:**
1. `make scenarios TASK=1` — полный прогон с паузой 30 с по умолчанию.
2. Ожидание: S1–S5 PASS, уровень «полностью»; зафиксировать в журнале прогона число
   «запросов к Payment за время отказа» (S4) — оно уходит в раздел «Проверка» PR.
3. Если какой-то сценарий упал — разобрать по `results/task_1.html` (истории заказов)
   и вернуться в Task 1–3.

**Verification:**
- Вывод раннера: `Прошло 5 из 5`, `Уровень: полностью`.
- `git status` — изменены только файлы в `orders/`.
