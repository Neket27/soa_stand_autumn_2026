# Design — Durable release резерва

## Context

Две утечки резерва при `rejected` (инвариант 5, `openspec/context/03-architecture.md`):

- **L1** — `OrderCharger.release()` (`orders/flow/OrderCharger.java:178`): один `DELETE`
  (1 с), при неудаче `log.warn`, заказ уже `rejected` — терминальный, повторять некому.
- **L2** — `CreateOrderService` (`orders/flow/CreateOrderService.java:40-44`):
  `reserve()==false` — неопределённый исход (тайм-аут ≠ «резерва не было»), заказ
  `rejected`, `DELETE` не вызывается.

Плюс прекурсор: дерево не компилируется — файл `InMemoryChargeDispatcher.java` содержит
`public class BackgroundWorker` (rename не завершён).

## Решение

**Durable compensating action: персистентная обязанность (release duty) + at-least-once sweep.**

1. **Колонка** `orders.release_pending BOOLEAN NOT NULL DEFAULT FALSE` в `schema.sql`
   (+ `ADD COLUMN IF NOT EXISTS` для живых БД). Duty — это «товар по этому заказу обязан
   быть снят со склада», факт отдельный от статуса заказа.
2. **Атомарный transition с duty.** `OrderRepository.transition(id, to, releasePending)` —
   один `UPDATE orders SET status=?, release_pending=? WHERE id=? AND status='pending'`
   (тот же CAS, что и сейчас). Окна «rejected закоммичен, duty не закоммичен» нет.
   2-аргументная `transition(id, to)` остаётся (duty=FALSE) для `paid`.
3. **OrderCharger** (ветки DECLINED в обоих местах и NOT_CHARGED в `decideAfterNetwork`):
   inline-`release` (быстрый путь, 1 с) → `transition(id, REJECTED, duty=!released)`.
   Crash между успешным release и transition безопасен: заказ ещё `pending`, reaper
   перезапустит charge; 402 с тем же Idempotency-Key запоминается mock (и реальным
   Payment по контракту), повторный decline → повторный release (204 идемпотентен).
4. **CreateOrderService**: `reserve()==false` → `transition(id, REJECTED, duty=true)`.
   На HTTP-пути **нет** второго вызова в Inventory (см. бюджет ниже).
5. **Release-sweep в reaper** (`InMemoryChargeDispatcher`, `@Scheduled 500 мс`):
   ```
   sweepReleases():  для каждого id из findReleasePendingIds():
       if inventoryClient.release(id, 1s): clearReleaseDuty(id)
   ```
   `findReleasePendingIds` = `SELECT id FROM orders WHERE status='rejected' AND release_pending`.
   `clearReleaseDuty` = `UPDATE orders SET release_pending=FALSE WHERE id=? AND status='rejected' AND release_pending`.
6. **Restructure reaper**: текущий early-return «breaker OPEN → return» переносится
   **внутрь** pending-sweep. Release-sweep идёт всегда — Payment-брейкер не знает
   о здоровье Inventory.

## Почему так (рассматривавшиеся варианты)

| Вариант | Почему не он |
|---|---|
| Ретраи release в цикле воркера (в пределах 25 с) | Держит воркера и in-flight-марку на зомби-заказе; при долгом отказе Inventory всё равно не уложится в дедлайн → всё равно нужен персистентный fallback; два механизма вместо одного |
| In-memory очередь «должников» | Теряется при рестарте (прогон — это один запуск, но `make up` пересоздаёт базу, а рестарт процесса в harness допустим); БД уже source of truth — добавлять второй носитель состояния нерационально |
| `GET /reservations?order_id=` перед `DELETE` | Лишний запрос: `DELETE` по контракту 204 и для отсутствующего резерва — он сам и есть подтверждение «резерва больше нет» |
| Inline-`DELETE` на HTTP-пути при `reserve()==false` | Бюджет: reserve может занять до 890 мс из 900 мс, второй вызов не влезает (см. ниже) |

**At-least-once + идемпотентный `DELETE`** — классика: статус «резерв снят» не храним
отдельно, потому что `204` по контракту означает «резерва по заказу больше нет» —
проверка состояния встроена в саму операцию (self-verifying command).

## Обязательные вопросы (по rules)

- **Бюджет 1 с**: HTTP-путь не получает ни одного нового вызова (reserve — как был;
  transition — один локальный UPDATE). При неопределённом reserve ответ остаётся
  `201 rejected` в бюджете; уборка — в фоне (≤ 500 мс до sweep + 1 с тайм-аут
  DELETE, что на порядки меньше паузы раннера 30 с).
- **Двойное списание**: charge-путь Payment не изменён; новые коды не касаются
  `POST /payments`. Idempotency-Key/CAS/reconcile — как были.
- **Заказ с потерянным ответом → paid**: не затронут (S5-логика в
  `decideAfterNetwork` не меняется; NOT_CHARGED-ветка получает duty, исход
  `paid` не меняется).
- **Нагрузка на Payment в блэк-холе**: release-sweep ходит **только в Inventory**,
  в бюджет S4 (считает запросы к Payment) не входит. Pending-sweep сохраняет
  гейт «breaker OPEN → skip».

## Trade-offs и слабые места (честно)

- **Долгий отказ Inventory** → по одному `DELETE` на застрявший заказ каждые 500 мс
  (2 req/s/заказ), backoff/лимитера нет. Для стенда (N ≤ ~70 заказов) это ничтожно;
  в production нужен был бы backoff + breaker для Inventory — в Non-Goals.
- **Лишний идемпотентный `DELETE`** при «тайм-аут, но выполнился» — один запрос,
  204, без side-эффекта (контракт).
- **Сweep не знает, создавался ли резерв** при L2 — и не должен: `DELETE` без
  резерва тоже 204, обязанность гаснет корректно в обоих случаях.

## Схема

По одной диаграмме на файл (IDEA-плагины рендерят надёжнее):
- `diagrams/orders-components.puml` — компонентная схема сервиса с паттернами
  (требует Graphviz, локально `/usr/bin/dot`);
- `diagrams/orders-flow.puml` — последовательность основных потоков (S5 lost
  response, S1 decline) с подписанными паттернами (Graphviz не требует).

Подписанные паттерны: Idempotent Command (Idempotency-Key), Reconciliation,
Circuit Breaker, CAS state machine (single terminal transition), at-least-once
reaper, Durable Compensating Action (release duty — новый). Готовые PNG рядом:
`OrdersComponents.png`, `OrdersFlow.png`.

## Тесты

- Юнит: `OrderRepositoryTest` (transition с duty, find/clear), `OrderChargerTest`
  (decline/NOT_CHARGED × release ok/fail), `CreateOrderServiceTest` (reserve false
  → rejected + duty), `InMemoryChargeDispatcherTest` (sweep чистит duty; sweep
  идёт при OPEN-брейкере; неудачный release держит duty).
- Сценарии: `make scenarios ONLY=S1 WAIT=5` (spot) и финальный `make scenarios
  TASK=1` — S1–S5 PASS «полностью», «rejected: резерв снят» — уже в проверках
  раннера (для S1: 3 rejected).
