# Tasks: harden-order-payment-flow

## Task 1: Single-flight обработка в BackgroundWorker

**Files:** `orders/src/main/java/orders/flow/BackgroundWorker.java`,
`orders/src/test/java/orders/flow/BackgroundWorkerTest.java` (новый)

- [x] `workerLoop`: обработка — только если `inFlight.putIfAbsent(orderId, Boolean.TRUE) == null`;
      `inFlight.remove(orderId)` — в `finally`; дубликатная копия из очереди отбрасывается
- [x] `reaper`: заменить `putIfAbsent(...) == null → queue.offer` на
      `if (!inFlight.containsKey(orderId)) queue.offer(orderId)` — reaper больше не ставит метку
- [x] Вынести число воркеров в конструктор (package-private / параметр с дефолтом 8) для тестируемости
- [x] `BackgroundWorkerTest`: заказ, поставленный в очередь дважды, обрабатывается ровно один раз
- [x] `BackgroundWorkerTest`: reaper не ставит в очередь заказ, уже в inFlight
- [x] `BackgroundWorkerTest`: reaper повторно ставит `pending`-заказ из БД, если он не в inFlight
- [x] `cd orders && mvn -q test` — зелёный
- [x] `make scenarios ONLY=S4 WAIT=5` — PASS, заказов с 2+ списаниями: 0

## Task 2: Убрать pre-charge reconciliation GET в OrderProcessor

**Files:** `orders/src/main/java/orders/flow/OrderProcessor.java`,
`orders/src/test/java/orders/flow/OrderProcessorTest.java`

- [x] `processUntilTerminal`: удалить pre-charge `hasCharges`; первый запрос раунда — `charge`;
      реконсилирующий `hasCharges` — только в ветке `NETWORK`
- [x] `processUntilTerminal_networkWithHasCharges_returnsTrue`: `verify(hasCharges, times(2))` → `times(1)`
- [x] `processUntilTerminal_hasChargesFirst_returnsTrue` переписать: уже выполненное списание
      обнаруживается идемпотентным POST (`charge` → `CHARGED`, `hasCharges` — `never()`, итог `paid`)
- [x] Проверить `processUntilTerminal_networkNoHasCharges_opensBreaker`: `verify(hasCharges, times(1))`,
      `rejected` + `release` + брейкер OPEN
- [x] `cd orders && mvn -q test` — зелёный
- [x] `make scenarios ONLY=S2,S3,S4,S5 WAIT=5` — все PASS; в журнале S4 «запросов к Payment за время
      отказа» заметно меньше 21 (ожидаемо 6–16)

## Task 3: Убрать мёртвую ветвь "PAID" в OrdersController

**Files:** `orders/src/main/java/orders/api/OrdersController.java`,
`orders/src/test/java/orders/api/OrdersControllerTest.java`

- [x] `createOrder`: удалить ветвь `if ("PAID".equals(result))` — `processOnce` возвращает только
      `REJECTED`/`PENDING`
- [x] `OrdersControllerTest.createOrder_paid_returns201Paid` — удалить (ветвь недостижима);
      поведение «резерв взят → 201 pending + enqueue» уже покрыто
      `createOrder_incomplete_returns201Pending`
- [x] `cd orders && mvn -q test` — зелёный
- [x] `make scenarios ONLY=S1 WAIT=5` — PASS

## Task 4: Полный прогон и фиксация результата

- [x] `make scenarios TASK=1` — полный прогон с паузой 30 с по умолчанию
- [x] Ожидание: S1–S5 PASS, уровень «полностью»; зафиксировать в журнале число
      «запросов к Payment за время отказа» (S4) — уходит в раздел «Проверка» PR
- [x] При провале сценария — разбор по `results/task_1.html` (истории заказов) и возврат в Task 1–3
- [x] `git status` — изменены только файлы в `orders/`
