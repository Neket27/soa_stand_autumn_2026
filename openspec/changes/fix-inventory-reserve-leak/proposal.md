# Proposal — Durable release резерва (fix-inventory-reserve-leak)

## Why

Инвариант 5 (`rejected` — чистый: денег нет **и** действующего резерва нет) сейчас
гарантируется **однократной** inline-операцией `DELETE /reservations/{order_id}`.
Есть две утечки, при которых `rejected`-заказ навсегда остаётся со снятым
неполностью резервом (товар «зависает» на складе):

- **L1 — `release()` без ретрая.** `OrderCharger.release()`
  (`orders/flow/OrderCharger.java:178`) делает один `DELETE` с тайм-аутом 1 с;
  при неудаче — только `log.warn`, а заказ уже переходит в `rejected`
  (терминальный). Перезапускающей петли на снятие резерва нет: ни воркер, ни
  reaper (`InMemoryChargeDispatcher.reaper()` свипает только `pending`) его больше
  не трогает. Утечка.
- **L2 — потерянный ответ на `reserve`.** `CreateOrderService.createOrder()`
  (`orders/flow/CreateOrderService.java:40-44`): `inventoryClient.reserve()`
  возвращает `false` на тайм-аут/сеть — но это **неопределённый** исход: резерв
  мог создаться, а ответ потеряться (аналог S5, но для Inventory). Заказ
  немедленно `rejected`, `DELETE` при этом никто не вызывает → тот же leak.

В стенде это не видно: в S1–S5 отказывает только Payment, Inventory безотказный,
mock-`DELETE` всегда отвечает. На проверке «что будет при отказе» (раздел шаблона
PR) и в реальном мире это прямое нарушение инварианта 5.

Второй повод для change: текущее дерево в середине переименования
(файл `InMemoryChargeDispatcher.java` содержит `public class BackgroundWorker` —
проект **не собирается**). Завершение rename — обязательная задача-прекурсор
(Phase 0), иначе ничего не верифицировать.

## What Changes

- **Durable «release duty»** — признак обязанности снять резерв, персистентный в
  БД: колонка `orders.release_pending BOOLEAN NOT NULL DEFAULT FALSE`.
- Терминальный переход в `rejected` — **один атомарный UPDATE** со значением duty:
  inline-`release` удался → `FALSE`; не удался/исход `reserve` неизвестен → `TRUE`.
  Окна «заказ rejected, а обязанности ещё нет» не существует.
- **Неопределённый `reserve`** (тайм-аут/сеть на `POST /reservations`) → `rejected`
  с duty `TRUE`. На HTTP-пути **никакого** дополнительного `DELETE` — бюджет 1 с
  не позволяет вписать второй вызов (reserve сам занимает до 890 мс); уборку
  делает reaper.
- **Release-sweep в reaper** (`InMemoryChargeDispatcher`): кроме pending-sweep,
  раз в 500 мс по `rejected + release_pending` — `DELETE /reservations/{id}`,
  при `204` — снять duty. `DELETE` идемпотентен по контракту
  (`contracts/inventory.yaml`: «Повторный вызов и вызов для заказа без резерва
  тоже отвечают 204») → at-least-once ретрай безопасен, «тайм-аут, но DELETE
  выполнился» закрывается следующим свипом.
- Release-sweep **не гейтится** Payment-брейкером (это другой сервис; блэк-хол
  Payment — не повод не снимать резерв в Inventory). Pending-sweep гейт
  сохраняет.
- **Phase 0**: завершить rename `BackgroundWorker` → `InMemoryChargeDispatcher`
  (класс + тест + ссылки), вернуть проект в компилируемое состояние.

Поведение в стенде **byte-identical**: inline-`release` в S1 всегда успешен →
duty не проставляется → sweep не шлёт ни одного лишнего запроса. Новые пути
покрываются юнит-тестами с мокируемым Inventory.

## Capabilities

### New Capabilities

(нет — всё расширяет capability `orders`)

### Modified Capabilities

- `orders`:
  - **MODIFIED** «Order status consistency» — «rejected implies no held
    reservation» становится **durable**: снятие резерва подтверждено, а не просто
    предпринято;
  - **ADDED** «Durable reservation release» — неопределённые исходы
    `POST/DELETE /reservations` + персистентная обязанность + ретрай до `204`.

## Scope

- `orders/src/main/java/**`, `orders/src/test/java/**`,
  `orders/src/main/resources/schema.sql`.
- Артефакты change в `openspec/changes/fix-inventory-reserve-leak/`
  (plan-only, в PR курса не попадает).

Новых зависимостей **нет**.

## Non-Goals

- Надёжность самого Inventory (в стенде не отказывает; свой breaker/backoff для
  Inventory — за рамками).
- Изменения charge-пути Payment (charge/reconcile/breaker не трогаются).
- Multi-replica (это change `kafka-multi-replica`; duty-flag совместим с ним —
  sweep под leader-gate добавляется там).
- Метрики/наблюдаемость (в текущем дереве их нет).

## Impact

- **Схема**: колонка `orders.release_pending` (создаётся сервисом при старте,
  как всё остальное в `schema.sql`).
- **Код**: `OrderRepository` (transition с duty, `findReleasePendingIds`,
  `clearReleaseDuty`), `OrderCharger` (duty по результату release),
  `CreateOrderService` (rejected с duty при неопределённом reserve),
  `InMemoryChargeDispatcher` (release-sweep + зависимость `InventoryClient`).
- **Контракт** `contracts/orders.yaml` — без изменений.
- **Тесты**: юнит (H2 + моки) + `make scenarios TASK=1` (S1–S5 PASS,
  «rejected: резерв снят» — уже проверка раннера).
