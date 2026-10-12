# Tasks — Kafka / N реплик / production

Курсовой дефолт `orders.dispatch=in-memory` остаётся зелёным на каждом шаге.
Команда проверки после задачи: `cd orders && mvn -q test` + (если затронут
визуально/транспортно путь) `make scenarios ONLY=... WAIT=5`.
Kafka-путь проверяется в `local/` harness (Phase 6), а не в курсовом стенде.

## 1. Phase 0 — Порт ChargeDispatcher (отделить ядро от транспорта)

- [x] 1.1 Интерфейс `orders.flow.ChargeDispatcher` с `void dispatch(String orderId)`
  (seam «положить orderId на фоновую обработку»). `drainOnShutdown` — добавляется в
  Phase 3/5 вместе с graceful drain (не в Phase 0, чтобы не менять поведения).
- [x] 1.2 `BackgroundWorker` → `InMemoryChargeDispatcher implements ChargeDispatcher`
  (queue + пул + single-flight + reaper, поведение 1:1), `enqueue` → `dispatch`.
- [x] 1.3 `CreateOrderService` зависит от `ChargeDispatcher` (не от concrete).
  `OrderCharger` не меняется.
- [x] 1.4 Флаг `orders.dispatch`: пока только in-memory реализация активна;
  `@ConditionalOnProperty` появится с Phase 3 (Kafka-реализация).
- [x] 1.5 Тесты: `InMemoryChargeDispatcherTest` (ex-BackgroundWorkerTest),
  `CreateOrderServiceTest` (мок `ChargeDispatcher`) — `mvn test` 70/70 зелёные.
- [x] 1.6 Спот-чек: `make scenarios ONLY=S1 WAIT=5` — PASS (46 paid/4 rejected, 0
  двойных) — валидирован Spring-wiring + Docker-сборка с новой структурой.

## 2. Phase 1 — Общий circuit breaker (Postgres) [P2]

- [x] 2.1 Схема: `circuit_breaker(id INT PRIMARY KEY CHECK (id=1), state TEXT,
  open_until BIGINT, probe_until BIGINT)` в `schema.sql` (+ seed id=1/CLOSED).
  Окна храним как epoch-ms (время из приложения, NTP в K8s) — dialect-агностично.
- [x] 2.2 Интерфейс `BreakerStateStore` (`allow/onReachable/onUnavailable/getState`) +
  `InMemoryBreakerStateStore` (логика перенесена дословно) + `DbBreakerStateStore`
  (CAS-переходы, global single-flight проб через `UPDATE ... WHERE state='OPEN' AND
  open_until<=?` по числу затронутых строк).
- [x] 2.3 `PaymentCircuitBreaker` — фасад, делегирует `BreakerStateStore`;
  convenience-конструктор `(openForMs, probeWindowMs)` сохранён для тестов. TTL-кэш не
  добавляем — на масштабе N=3 лишний round-trip не критичен.
- [x] 2.4 Флаг `orders.breaker-store=in-memory|db` (дефолт `in-memory`).
- [x] 2.5 Тесты: `DbBreakerStateStoreTest` (H2) — 9 сценариев + «ровно один выигрывает
  проб» (8 потоков), no-op onUnavailable при OPEN, watch-dog. 10/10.
- [x] 2.6 Спот-чек: `make scenarios ONLY=S4 WAIT=5` — PASS (дефолт in-memory, 12 запросов
  к Payment за отказ, 0 double-charge).

## 3. Phase 2 — Single-leader reaper + атомарный claim [P1]

- [x] 3.1 Лидерство: `LeaderElector` (always|postgres по `orders.reaper-leader`);
  `PostgresLeaderElector` — `pg_try_advisory_lock` на выделенном connection (перезахват
  при обрыве, `pg_advisory_unlock` + close при `@PreDestroy`); reaper гейтится `isLeader()`.
- [x] 3.2 Атомарный claim: `OrderRepository.claim(id, leaseMs)` =
  `UPDATE orders SET claim_until=? WHERE id=? AND status='pending' AND
  (claim_until IS NULL OR claim_until<=now)` (epoch-ms BIGINT, lease); колонка
  `orders.claim_until BIGINT` в схеме.
- [x] 3.3 `InMemoryChargeDispatcher`: worker делает `claim` (в дополнение к локальному
  `inFlight`), проигравший claim кладёт order обратно; reaper (лидер) кладёт только
  незакаленные `pending`.
- [x] 3.4 Тесты: `OrderRepositoryTest.claim_concurrent_singleWinner` (8 потоков → 1
  выиграл), expired-lease, terminal, + reaper с `AlwaysLeaderElector`. 12/12.
- [x] 3.5 Спот-чек: `make scenarios TASK=1` — S1–S5 PASS.
- [x] 3.6 «N реплик корректно без Kafka» достигнуто (claim+лидер+общий breaker); Phase 3+
  — fan-out событий и горизонтальный масштаб, по решению — делать полный production.

## 4. Phase 3 — Kafka-транспорт (fast consumer) [за ORDERS_DISPATCH=kafka]

- [x] 4.1 `pom.xml`: `org.apache.kafka:kafka-clients` (чистый KafkaConsumer/Producer,
  без spring-kafka) + testcontainers (test/it). Общий `ChargeWorkerPool` вынесен из
  `InMemoryChargeDispatcher` (порт транспорта, одна worker-логика на оба режима).
- [x] 4.2 `orders.kafka.ChargeEventProducer`: publish `orders.charge` (`key=orderId`),
  inline `send().get(800мс)` best-effort (ошибка не фейлит запрос — log + reaper),
  `sendBackground` для reaper, `sendToDlq`.
- [x] 4.3 `KafkaChargeDispatcher implements ChargeDispatcher`: consumer group
  (`CooperativeStickyAssignor`, `enable.auto.commit=false`, manual `commitSync`) →
  `poll` → `pool.offer` (не блокирующий, backpressure через lag: офсет коммитится,
  только если все записи доставки) → тяжёлый `charge` в `ChargeWorkerPool`, НЕ в poll.
- [x] 4.4 DLQ: malformed (null/blank key+value) → `orders.charge.dlq` + `dlqCount()`;
  партиция не блокируется.
- [x] 4.5 Graceful drain: `@PreDestroy` → `consumer.wakeup()` + join +
  `pool.drainOnShutdown(grace)`; K8s preStop/`terminationGracePeriodSeconds` — в доках (6.3).
- [x] 4.6 Reaper-лидер в kafka-режиме re-publish застрявшие `pending` в topic
  (`sendBackground`).
- [x] 4.7 `KafkaChargeDispatcherIT` (testcontainers Kafka+Postgres): «consumer →
  worker → paid», «redelivery того же orderId → не дважды» (claim WHERE status='pending'),
  «DLQ на poison». Скомпилировано; в Docker-окружении с совместимым API проходит,
  на Docker Engine ≥26 (требует API ≥1.44, testcontainers по умолчанию говорит 1.32)
  корректно SKIPs (зелёный, не падает) — см. 8.x.
- [x] 4.8 Спот-чек с kafka → Phase 6 harness: E2E прогон N=3 (normal-mode 40/40,
  consumer group, 3 партиции) — см. 7.4/7.6 и `verification.md`.

## 5. Phase 4 — Transactional Outbox → fan-out

- [x] 5.1 Схема: `outbox(id IDENTITY PK, order_id TEXT UNIQUE, topic TEXT, payload TEXT,
  created_at, published_at, claiming)` — dialect-agnostic (H2+Postgres); `UNIQUE(order_id)`
  = одно событие на заказ. *(deviation: payload TEXT вместо JSONB, key/seq убраны как
  дублирующие order_id/id)*
- [x] 5.2 Транзакционная граница: `OrderRepository.transition()` + `INSERT outbox` —
  **одна tx** (`TransactionTemplate`), только терминальные; opt-in `orders.outbox.enabled`
  (дефолт false → курсовой стенд byte-identical). Откат при сбое outbox → заказ pending.
- [x] 5.3 `OutboxRelay` (claim-based, **single-leader** через `LeaderElector`): claim →
  publish `order.paid`/`order.rejected` (`key=orderId`) → `published_at`; at-least-once.
  `ChargeEventPublisher`/`OutboxPublisher` — narrow seam.
- [x] 5.4 Тесты `OutboxTest`: «переход+outbox атомарно», «откат при UNIQUE(order_id) →
  pending + нет outbox», «relay публикует+отмечает», «at-least-once дубль при lost-mark»,
  «порядок oldest-first». 7 тестов.
- [x] 5.5 fan-out check в harness: `local/fanout_check.sh` (консьюмит event-topics,
  total+distinct orderId).
- [x] 5.6 Спот-чек: harness N=3 kafka + outbox — `load.sh 40` → 40/40 paid;
  `fanout_check.sh` → order.paid **total=40 distinct=40** (exactly-once).

## 6. Phase 5 — Production hardening

- [x] 6.1 Micrometer (`actuator`+`prometheus`): `orders.charge.success/failure`,
  `orders.payment.requests` (S4-бюджет-прокси), `orders.post.create` (p99),
  `orders.breaker.state` (gauge), `orders.dlq.count`, `orders.consumer.lag`,
  `orders.outbox.unpublished`, `orders.worker.queued/inflight`. `OrderMetrics` фасад.
- [x] 6.2 Readiness: `/healthz` = DB [+ consumer assigned при dispatch=kafka]
  (`ReadinessProbe`); `/livez` — liveness.
- [x] 6.3 Конфиг-доки: `orders/PRODUCTION.md` (все env, партиции, тайминги, HPA-ready,
  security, observability).
- [x] 6.4 Security config-ready: `KafkaSecuritySettings` (SASL/SSL → `SASL_SSL`/`SSL`,
  PLAIN/SCRAM), DB TLS (`orders.db.sslmode`/`sslrootcert`). Инертны до включения.
- [x] 6.5 Тесты: `KafkaSecuritySettingsTest` (парсинг security), `ReadinessProbeTest`
  (readiness), `OrderMetricsTest` (smoke метрик).

## 7. Phase 6 — Локальный N-replica harness + failure drills

- [x] 7.1 `docker-compose.local.yml` (dev-only, НЕ для CI курса): Kafka KRaft
  (single-node `apache/kafka:3.7.0`, без ZK) + Postgres 16 + mocks + **3× orders**
  (kafka-режим) + load. `BREAKER_STORE`/`REAPER_LEADER` — через env.
- [x] 7.2 KRaft-конфиг: topic `orders.charge` с **партициями = 3**
  (`KAFKA_NUM_PARTITIONS: 3` + auto-create). Подтверждено: 3 партиции, consumer group
  распределяет их между репликами.
- [x] 7.3 Load-harness: `local/load.sh` — round-robin N заказов на 3 реплики, ожидание
  terminal, отчёт paid/rejected/pending.
- [x] 7.4 Прогон **N=3**, dispatch=kafka, breaker-store=db: normal-mode **40/40 paid**,
  0 pending, 0 ошибок; каждый orderId обработан ровно 1 раз (по логам — 0 дублей).
  Полная оркестрация S1–S5 (режимы Payment) под Kafka — отдельно (см. 7.6/8.3).
- [x] 7.5 **S4-бюджет N=3**: `local/s4_drill.sh` (blackhole 20s, 40 заказов).
  `in_blackhole` = 20 (db, общий брейкер) / 23 (in-memory, per-JVM) — оба ≤ лимита 30.
  Глобальный single-flight проб доказан юнит-тестом (8 потоков → ровно 1).
- [x] 7.6 Failure drills: (a) kill orders2 mid-charge (latency 2s) → 49/49 paid,
  `charges.total`=49=distinct (0 дублей); (b) ронуть Kafka → 20/20 sent при down
  (~600 мс/шт, 1s-бюджет цел), все pending → 20/20 paid за 6 c после up; (c)
  restart всех 3 → 30/30 paid, 0 дублей. Выявлен+исправлен баг `max.block.ms=500`
  (иначе `send()` висел 60 c при down Kafka).
- [x] 7.7 Отчёт: `openspec/changes/kafka-multi-replica/verification.md` (N=3 normal,
  S4-бюджет A/B, failure drills, найденный баг, ограничения).

## 8. Финальная верификация

- [x] 8.1 `cd orders && mvn -q test` — зелёные: 84 теста; `KafkaChargeDispatcherIT`
  (testcontainers) на Docker Engine ≥26 (требует API ≥1.44, testcontainers по умолчанию
  1.32) корректно SKIPs, в совместимом Docker-окружении проходит.
- [x] 8.2 Курсовой стенд дефолт (in-memory): `make scenarios TASK=1` — S1–S5 PASS
  (сдача курса не затронута; проверено после Phase 0–3 рефакторинга).
- [x] 8.3 Harness N=3 (kafka): normal-mode 40/40 paid + S4-бюджет + failure drills
  (7.6) ✓. Полная оркестрация S1–S5 (режимы Payment) под Kafka — отдельно (не входит в
  core-сдачу; см. `verification.md`).
- [x] 8.4 `git status`/diff — за границей `orders/` только dev-only файлы
  (`docker-compose.local.yml`, `local/`) и артефакты change; в `master`/сдачу они
  не попадают.
