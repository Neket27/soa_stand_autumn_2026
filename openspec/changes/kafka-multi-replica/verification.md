# Проверка: Kafka-транспорт и N-реплики (harness, dev-only)

Локальный harness `docker-compose.local.yml` (НЕ для CI курса): Kafka KRaft
(`apache/kafka:3.7.0`, single-node, без ZK) + Postgres 16 + mocks + **3× orders** в
Kafka-режиме (`ORDERS_DISPATCH=kafka`, `BREAKER_STORE=db`, `REAPER_LEADER=postgres`).

Запуск:
```
docker compose -f docker-compose.local.yml up -d --build
bash local/load.sh 40          # normal-mode round-robin
bash local/s4_drill.sh 40      # S4-бюджет (blackhole Payment)
```
Поднимать/ронять: `docker compose -f docker-compose.local.yml stop|start|down`.
Токен mocks для `/admin/*`: `local-kafka-token` (заголовок `X-Admin-Token`).

## Факты среды
- Topic `orders.charge`: **3 партиции** (`KAFKA_NUM_PARTITIONS: 3`), `key=orderId`.
- Consumer group `orders-charge` на 3 репликах → партиции распределены между ними.
- Общая БД (Postgres): `orders` (+ `claim_until`), `circuit_breaker` (общий брейкер).
- Все проверки статуса заказа — через любую реплику (общая БД).

## 1. Normal-mode N=3 — нет потерь, нет дублей
`bash local/load.sh 40`:
- **40/40 → `paid`**, 0 pending, 0 ошибок отправки.
- По логам 3 реплик: 40 событий `charge worker processing <id>`, **40 distinct, 0 дублей**
  → каждый заказ обработан (списан) ровно один раз (claim + idempotency).

## 2. S4-бюджет на N=3 (общий брейкер)
`bash local/s4_drill.sh 40` (blackhole Payment 20 c, 40 заказов во время отказа):

| BREAKER_STORE | `payment.requests.in_blackhole` | ≤ лимита 30 |
|---|---|---|
| `db` (общий) | 20 | да |
| `in-memory` (per-JVM) | 23 | да |

- Глобальный **single-flight HALF_OPEN-проб** доказан юнит-тестом
  `DbBreakerStateStoreTest.concurrentAllow_singleProbeReleased` (8 конкурентных вызовов
  `allow()` → ровно 1 разрешён).
- Разница 20 vs 23 не 3×, т.к. на средней нагрузке доминирует начальный burst (до открытия
  брейкера), а общий брейкер режет именно **пробы**; под более высокой нагрузкой/длиньше
  отказом различие растёт. Оба режима в пределах лимита.

## 3. Failure drills (N=3)

| Drill | Действие | Результат |
|---|---|---|
| (a) kill реплики mid-charge | `latency_ms=2000`, 60 заказов, `docker stop orders2` на i=25 | 49/49 (доставленных живым репликам) → `paid`; `charges.total=49` = 49 distinct → **0 дублей**; пришедшие на dead-реплику заказы re-driven выжившими |
| (b) ронуть Kafka | `docker stop kafka`, 20 заказов (inline-publish фейлит → DB pending), `docker start kafka` | 20/20 отправлены при down Kafka **за 12 c** (~600 мс/шт, 1s-бюджет не сломан), все pending → после up Kafka **20/20 paid за 6 c** (reaper re-publish) |
| (c) restart всех реплик | `latency_ms=1500`, 30 заказов, `docker restart orders1/2/3` mid-flight | **30/30 → paid**, 0 pending; `charges.total=30` = 30 distinct → **0 дублей** |

Во всех дрелах: ни один заказ не потерян, двойных списаний нет.

## Найденный и исправленный баг (drill b)
При недоступном Kafka `KafkaProducer.send()` блокировался на metadata-fetch до
`max.block.ms` (дефолт 60 с) → `POST /orders` мог висеть и сломать бюджет 1 с.
Фикс: `ProducerConfig.MAX_BLOCK_MS_CONFIG = 500` в `KafkaConfig` — inline-publish
fail-fast'ит в рамках бюджета; заказ не теряется (DB `pending` + single-leader reaper).

## 4. Transactional outbox → fan-out (Phase 4)
`ORDERS_OUTBOX_ENABLED=true` в harness. Терминальный `transition` + строка в `outbox` — в
одной tx (откат при сбое outbox → заказ остаётся pending, reaper re-drive). Relay —
single-leader (как reaper), claim-based, публикует `order.paid`/`order.rejected` в Kafka
(`key=orderId`), отмечает `published_at`.

`bash local/fanout_check.sh` после `bash local/load.sh 40`:
```
order.paid      total=40   distinct_orders=40     # ровно одно событие на заказ (exactly-once)
order.rejected  total=0    distinct_orders=0      # (в прогоне без отказов)
distinct orders covered: 40
```
- Invariant at-least-once соблюдён: каждый terminal-заказ получил ≥1 события (distinct==40).
- Single-leader relay → без дублей при нормальной работе (total==40==distinct). At-least-once
  (дубль при краше лидера между publish и mark) задокументирован; downstream идемпотентен.
- `order.rejected` — тот же путь (transition→outbox→relay→topic), покрыт юнит-тестом
  `OutboxTest` (transition+outbox атомарно, откат при UNIQUE(order_id), relay, at-least-once,
  порядок oldest-first).

## 5. Observability / readiness / security (Phase 5)
- **Метрики** `/actuator/prometheus` (тег `application=orders`): `orders.charge.success/failure`,
  `orders.payment.requests` (S4-бюджет-прокси), `orders.post.create` (p99 `POST /orders`),
  `orders.breaker.state` (0/1/2), `orders.dlq.count`, `orders.consumer.lag`,
  `orders.outbox.unpublished`, `orders.worker.queued/inflight`. Подтверждено в harness: все
  метрики присутствуют и реагируют (после 40 заказов `charge.success`+`payment.requests`
  нарастают на ~13-14 на реплику, `outbox.unpublished`→0 после relay).
- **Readiness** `/healthz` = DB reachable (+ consumer assigned при `dispatch=kafka`);
  **liveness** `/livez`. В harness оба 200 на 3 репликах.
- **Security config-ready**: Kafka SASL/mTLS (`orders.kafka.ssl.*`/`sasl.*`, `SASL_SSL`/`SSL`),
  DB TLS (`orders.db.sslmode`/`sslrootcert`) — параметры на месте, инертны до включения
  (покрыты `KafkaSecuritySettingsTest`).

## Ограничения / что осталось
- Полная оркестрация сценариев S1–S5 (режимы Payment: 500/402/lost/медленно) **под Kafka**
  — не прогонялась (harness проверяет normal-mode + blackhole + failure drills + fan-out);
  требует привязки course-runner'а к multi-replica.
- `KafkaChargeDispatcherIT` (testcontainers) на Docker Engine ≥26 корректно SKIPs
  (testcontainers по умолчанию говорит Docker API 1.32, требуется ≥1.44); в совместимом
  Docker-окружении проходит.
- HPA — конфигурационно готово (метрики для autoscaler на месте), сам HPA-манифест не делался
  (не входит в self-contained сервис).
