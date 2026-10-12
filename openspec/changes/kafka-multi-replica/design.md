## Context

Мотивация — в proposal.md (Why). Состояние: сервис `orders/` написан, S1–S5
проходят как одна реплика (снапшот `24580ff`). Цель: production-режим — N реплик
на K8s под нагрузкой. Ключевые ограничения (03-architecture.md): контракт
`contracts/orders.yaml` и HTTP-форма неизменны (инвариант 7); инварианты 1–6
(бюджет 1 с, не больше одного списания, деньги → paid, rejected чистый, не
перегружать Payment в блэк-холе, pending завершается до конца прогона) должны
соблюдаться **и при N репликах**. Базовые механизмы (idempotency, reconciliation,
CAS-переход) уже есть и транспорт-агностичны — они **не переписываются**, а
переносятся в новую транспортную обвязку.

## Goals / Non-Goals

**Goals:**
- Корректность на N репликах без потери инвариантов: нет дублей обработки (P1),
  глобальный (не per-JVM) бюджет нагрузки на Payment (P2), заказ не теряется при
  рестарте/краше любой реплики.
- Production-качество: быстрый consumer без rebalance-штормов, DLQ, graceful drain,
  backpressure через consumer lag, метрики, HPA-ready, бессоственный сервис.
- Fan-out доменных событий через transactional outbox.
- Дефолтный in-memory транспорт остаётся (CI курса зелёный); Kafka — за флагом.

**Non-Goals:**
- Не менять логику `OrderCharger` (машина списания), `PaymentClient` (idempotency),
  `contracts/*.yaml`.
- Не поднимать Kafka в учебном стенде; Kafka-путь — только K8s/локальный harness.
- Debezium/CDC, schema registry, enforcement ACL, multi-region.

## Архитектура (production-форма)

```
POST /orders ─► createOrder: insert pending (DB, source of truth)
                     │            + best-effort publish orders.charge (не держит ответ)
                     └► 201 pending (≤ 1 с)
                     │
        ┌────────────┴─────────────────────────────────────────────┐
        │ Kafka  orders.charge (key=orderId, partitions ≥ N)        │
        └────────────┬─────────────────────────────────────────────┘
             consumer group (N реплик, CooperativeStickyAssignor)
             poll → enqueue локально (быстро) → commit offset
                     │
        local worker pool (огранич.) → OrderCharger (НЕ меняется)
                     │
                     ├─ terminal: transition(status) + outbox row  [ОДНА tx]
                     └─ not terminal: остаётся pending в DB
                     │
        shared circuit breaker (Postgres, CAS, global single-flight)  ← P2
                     │
        Safety-net reaper (ОДИН лидер, pg advisory lock):
          застрявшие pending > X → publish в orders.charge (re-drive)
                     │
        Outbox relay (claim-based): outbox → publish order.paid/rejected → [fan-out]
```

## Decisions

### 1. Быстрый consumer, развязанная обработка (главное)

**Решение:** Kafka-консьюмер **не** крутит тяжёлый time-budgeted `charge` в
poll-цикле. Он: `poll()` → `workerPool.enqueue(orderId)` → `commit offset`. Тяжёлая
обработка — в локальном воркер-пуле (как сейчас).

**Обоснование:** тяжёлый `charge` (цикл с паузами под брейкер + reconciliation) в
poll-цикле блокирует консьюмер → нарушение `max.poll.interval.ms` → rebalance-штормы
под нагрузкой. Развязка убирает этот класс отказов.

**Source of truth «ещё не готово» = DB (`pending`), не Kafka-офсет.** Офсет =
«триггер доставлен». Упал под после commit до/во время charge → строка `pending`
жива → reaper-лидер re-publish → другая реплика обрабатывает → **заказ не теряется**.

**Альтернативы:**
- Consumer крутит `charge` и коммитит только после terminal — отклонено: блокирует
  poll-цикл, нарушает poll-interval, rebalance-штормы.
- Commit «после enqueue в in-memory» без DB-guarantee — отклонено: потеря при краше.

### 2. Idempotent consumer = effectively-once

**Решение:** at-least-once delivery (rebalance/краш → redelivery) безопасно, т.к.
повторный `charge` с тем же `Idempotency-Key` не дублирует списание, а CAS
`transition WHERE status='pending'` даёт ровно один терминальный переход.

**Альтернативы:** idempotent producer Kafka (enable.idempotence) — настраивается,
но это дедупликация записей **в topic**, не «не спиши Payment дважды»; не заменяет
`Idempotency-Key`. Включается как гигиена, не как гарантия.

### 3. Общий circuit breaker (P2) — Postgres, CAS, global single-flight

**Решение:** состояние брейкера — таблица `circuit_breaker(state, open_since,
half_open_since)` в Postgres (уже в стеке, без новой инфраструктуры). Переход на
проб — CAS:
`UPDATE circuit_breaker SET state='HALF_OPEN', half_open_since=now()
 WHERE state='OPEN' AND now()-open_since >= openForMs RETURNING ...`
— **обновивший строку** получает право на глобальный HALF_OPEN-проб, остальные
реплики ждут. `onReachable`/`onUnavailable` — тоже CAS-переходы по строке.

**Обоснование:** Postgres уже есть → нулевая новая инфраструктура (в духе
self-contained). Одна строка + CAS = атомарная глобальная координация. Время — из
`now()` БД, не из часов реплик (clock skew).

**Оптимизация `allow()`:** CLOSED/OPEN — из локального кэша с коротким TTL (не в БД
на каждом цикле); только переход на проб — round-trip. Частота `allow()` в charge-
цикле не давит на БД.

**Альтернативы:**
- Redis — быстрее, но новая зависимость; отклонено как не нужное (Postgres есть).
- K8s Lease для лидерства брейкера — отклонено: тянет RBAC на K8s API; Postgres lock
  проще и работает вне K8s.

### 4. Single-leader safety-net reaper + атомарный claim (P1)

**Решение:** (a) первичный dispatch — `createOrder` publish в Kafka (быстро,
распределённо); (b) **single-leader** reaper (лидер через Postgres advisory lock
`pg_try_advisory_lock`) — чисто backstop: застрявшие `pending` (> X c) re-publish в
topic. (c) атомарный claim: обработка берёт заказ
`UPDATE orders SET claimed_by=?, claimed_at=now() WHERE id=? AND status='pending'
RETURNING id` (или `SELECT ... FOR UPDATE SKIP LOCKED`) — строку забирает ровно одна
реплика, дублей нет.

**Обоснование:** лидерство через advisory lock — без K8s API, без нового брокера;
при краше лидера лок освобождается, другой под захватывает. Claim через SKIP LOCKED —
стандартный DB-backed distributed queue, масштабируется на N.

**Альтернативы:**
- K8s Lease (coordination.k8s.io) — «K8s-native», но RBAC + зависимость от K8s API;
  отклонено в пользу Portgres-лока (универсальнее).
- Reaper на всех репликах (как сейчас) + дедуп в Kafka — отклонено: дубль обработки
  (P1) как раз из этого.

### 5. Transactional Outbox + in-process relay (fan-out)

**Решение:** терминальный `transition(status)` и insert строки в `outbox` — в
**одной** транзакции (вводим транзакционную границу вокруг «переход + outbox»).
Relay — in-process, claim-based: `UPDATE outbox SET claiming=? WHERE id=? AND
published_at IS NULL RETURNING ...` → publish `order.paid`/`order.rejected` в Kafka
(`key=orderId`) → `published_at=now()`. At-least-once; downstream идемпотентен.

**Обоснование:** атомарность «состояние + событие» — суть outbox; in-process poller —
типовое self-contained production-решение, без внешней CDC-инфраструктуры.

**Альтернативы:**
- Debezium (CDC на WAL) — «более прод», но тянет Debezium + Schema Registry +
  отдельный deploy, который в сервисе/стенде не разместить; отклонено для self-contained.
- Публикация «напрямую» из charge (без outbox) — отклонено: dual-write (переход в БД
  + publish) рассинхронизируется при краше между ними.

### 6. Транспорт за флагом, дефолт in-memory

**Решение:** `orders.dispatch=in-memory|kafka` (дефолт `in-memory`). Порт
`ChargeDispatcher` с двумя реализациями. K8s ставит `kafka` через env.

**Обоснование:** CI курса гоняет стенд (1 реплика, нет брокера) с дефолтом → зелёный.
Kafka-путь включается только там, где есть брокер.

### 7. Конфиг, партиции, безопасность

- Конфиг — env/ConfigMap: `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_TOPIC_CHARGE`,
  `KAFKA_TOPIC_EVENTS`, `KAFKA_GROUP_ID`, `ORDERS_DISPATCH`, `BREAKER_STORE`,
  партиции, тайминги (продолжение существующих `orders.*`).
- Партиции `orders.charge` и событий ≥ max числа реплик; `key=orderId` (порядок на
  заказ, параллельность между).
- Consumer: `CooperativeStickyAssignor`, `enable.idempotence=true`, разумные
  `session.timeout.ms`/`heartbeat.interval.ms`/`max.poll.records`; DLQ
  `orders.charge.dlq` + алерт.
- Security (config-ready): Kafka SASL/mTLS, DB TLS — параметры на месте, включаются в
  целевом окружении.

## Как соблюдены обязательные инварианты (обязательные вопросы дизайна)

| Вопрос дизайна | Ответ |
|---|---|
| **Как удерживается бюджет 1 с на `POST /orders`?** | `createOrder` не меняется в ядре: insert `pending` + reserve (в рамках `orders.process.budget-ms=900`) + `201 pending`. Inline-publish в Kafka — **best-effort** с коротким таймаутом и **никогда не держит и не фейлит** ответ: гарантию даёт DB `pending` + single-leader reaper (re-publish). Даже если Kafka медленна/недоступна, ответ в бюджете, заказ не теряется. |
| **Как исключено двойное списание на N репликах?** | Без изменений + idempotent consumer: `Idempotency-Key=order_id` на каждом `POST /payments` (повтор = то же списание 200) + reconciliation + CAS `transition WHERE status='pending'`. Redelivery Kafka (rebalance/краш) → повторный `charge` с тем же ключом → не дважды; ровно один терминальный переход. |
| **Как заказ с потерянным ответом становится `paid`?** | Без изменений: `NETWORK` → `decideAfterNetwork` → `reconcile GET /payments?order_id=` → `CHARGED` → `paid` (`UNKNOWN` → остаётся `pending`, reaper re-drive). Kafka-транспорт не трогает машину `OrderCharger`. |
| **Как ограничена нагрузка на Payment в блэк-холе на N репликах?** | **Общий** circuit breaker (Postgres, CAS): в OPEN только одна реплика глобально выигрывает HALF_OPEN-проб (CAS по одной строке); остальные ждут. Глобальный бюджет S4 теперь не умножается на N. Reaper-лидер не гонит в OPEN. |

Дополнительно: **rejected чистый** — `release()` при `DECLINED`/`NOT_CHARGED` до
перехода (без изменений); **pending завершается до конца прогона** — reaper-лидер +
Kafka re-drive доводят все `pending`.

## Risks / Trade-offs

- [Risk] Inline-publish в `createOrder` затягивает ответ, если Kafka деградировала.
  [Mitigation] Короткий таймаут produce + не фейлить запрос; гарантия — DB + reaper.
  Метрика p99 produce в синхронном пути под алертом.
- [Risk] Consumer lag растёт при отказе Payment → накопление.
  [Mitigation] Бounded локальная очередь + backpressure через lag; HPA по lag; DLQ
  для poison; breaker не гонит в Payment.
- [Risk] Divergence in-memory и Kafka-путей (два транспорта).
  [Mitigation] Общий порт `ChargeDispatcher` + общая `OrderCharger`; оба пути
  покрываются общими юнит-тестами машины списания; Kafka-путь — testcontainers.
- [Risk] Single-leader reaper — точка координации.
  [Mitigation] Advisory lock авто-освобождается при краше лидера; выбор лидера
  миллисекунды; reaper — backstop, не первичный путь.
- [Risk] Outbox relay при краше между publish и mark → дубль события.
  [Mitigation] At-least-once + идемпотентный downstream; `key=orderId` + seq.
- [Risk] Пропел-бюджет S4 при N: CAS-проб должен быть ровно одним.
  [Mitigation] Интеграционный тест на N=3 в harness: глобальный счётчик запросов в
  блэк-холе ≤ лимита.
- [Trade-off] Kafka «тяжелее» in-memory reaper (брокер, офсеты, DLQ). Принято:
  окупается fan-out / масштабируемостью charge-worker / high-load, а не
  «корректностью на N» (её дают фазы 1+2).

## Migration Plan

По фазам из tasks.md; каждая фаза — S1–S5 зелёные (дефолт in-memory), независимо
откатывается к `24580ff`.

1. **Phase 0** — порт `ChargeDispatcher` (in-memory = текущий Behavior). Zero behavior change.
2. **Phase 1** — shared breaker (Postgres) за `BREAKER_STORE=db`. Чинит P2.
3. **Phase 2** — single-leader reaper + атомарный claim. Чинит P1. *(остановка возможна здесь)*
4. **Phase 3** — Kafka-транспорт за `ORDERS_DISPATCH=kafka` (consumer, DLQ, drain, config).
5. **Phase 4** — transactional outbox + relay → `order.paid/rejected`.
6. **Phase 5** — production hardening (метрики, readiness, HPA config, security config).
7. **Phase 6** — локальный N-replica harness + testcontainers + failure drills.

Откат: `git revert` фазы; транспорт за флагом — отключение флага; БД-таблицы
(`circuit_breaker`, `outbox`) — additive, не ломают in-memory путь.

## Resolved Decisions (подтверждено: 3 реплики)

- **Реплик в harness: 3.**
- **Локальный Kafka: single-node KRaft** (без ZooKeeper) в Docker
  (`confluentinc/cp-kafka` в KRaft-режиме), в `docker-compose.local.yml` (dev-only,
  не в CI курса).
- **Партиции:** `orders.charge` = **3** (= числу реплик, каждая реплика ≥ 1 партиции,
  нет простаивающих); event-topics `order.paid` / `order.rejected` = 3. `key=orderId`.
- **Fan-out-проверка:** лёгкий **echo-консьюмер** в harness подписывается на
  event-topics и подтверждает fan-out end-to-end (не только «запись в topic»).
- **Harness-режим:** `orders.dispatch=kafka`, `orders.breaker-store=db`, 3× orders,
  1 Kafka KRaft, 1 Postgres, mocks, load round-robin на 3 реплики.
