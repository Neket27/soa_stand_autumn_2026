## Why

Текущий сервис `orders/` корректен и проходит S1–S5 как **одна** реплика. Команде
нужно **production-решение**, которое работает как **N реплик на Kubernetes под
высокой нагрузкой** и используется в реальных продуктах. На N репликах у текущего
дизайна два production-дефекта:

- **P1 — дубль работы.** In-memory очередь и `reaper` живут на каждой реплике;
  `reaper` (BackgroundWorker.java:88) на **каждой** реплике кладёт ВСЕ `pending` в
  СВОЮ очередь → один заказ обрабатывается несколькими репликами. Idempotency + CAS
  это не ломают, но Payment-вызовы и CPU уходят втупую.
- **P2 — нет глобального бюджета нагрузки на Payment.** `PaymentCircuitBreaker` —
  singleton на JVM. В блэк-холе каждая реплика сама открывает брейкер и сама шлёт
  HALF_OPEN-пробы → глобальный лимит S4 (≤ N запросов за отказ) умножается на N и
  превышается.

Плюс production-маркетплейсу нужны **fan-out событий** (`order.paid/rejected` →
уведомления/аналитика/бухгалтерия) и **отдельно масштабируемый charge-worker**. Kafka
(KRaft) + transactional outbox + **общий** circuit breaker + БД как source of truth
закрывают всё это.

Внимание: учебный стенд — одна реплика и read-only вне `orders/` (инвариант 1),
брокер туда добавить нельзя. Этот change — **production-расширение за пределами
стена**, под целевое K8s-развёртывание; проверяется **локальным N-replica harness**
на Docker Compose (Kafka KRaft). В стенде/CI остаётся дефолтный in-memory транспорт →
сдача курса не затрагивается.

## What Changes

- **Порт транспорта** `ChargeDispatcher` — отделяет `OrderCharger` от механизма
  «куда положить orderId на обработку». Реализации: `in-memory` (текущий
  BackgroundWorker) и `kafka` (consumer group). Дефолт — `in-memory`.
- **Общий circuit breaker** (`orders.breaker-store=in-memory|db`): состояние в
  Postgres, переходы CAS-ом, **глобальный single-flight** HALF_OPEN-проба. Чинит P2.
- **Single-leader safety-net reaper** (Postgres advisory lock) + атомарный claim
  заказа. Чинит P1. Reaper re-publish застрявшие `pending` (в Kafka-режиме — в
  topic).
- **Kafka-транспорт** (`orders.dispatch=in-memory|kafka`): topic `orders.charge`
  (`key=orderId`), **быстрый** consumer group (N реплик) → локальный воркер-пул →
  `OrderCharger`; DLQ; graceful drain при SIGTERM; конфиг через env.
- **Transactional Outbox**: терминальный `transition(status)` + outbox-строка в
  **одной** транзакции; outbox relay (claim-based) публикует `order.paid` /
  `order.rejected` в Kafka (fan-out).
- **Production hardening**: метрики Micrometer/Prometheus (consumer lag, breaker
  state, DLQ, p99 POST, расход S4-бюджета), readiness с учётом Kafka, HPA-ready
  конфиг, security config-ready (SASL/mTLS).
- **Локальный N-replica harness** (dev-only, не в CI курса): `docker-compose.local.yml`
  = Kafka KRaft + Postgres + mocks + N× orders + load-генератор; failure drills.

## Capabilities

### New Capabilities

(нет — всё расширяет capability `orders`)

### Modified Capabilities

- `orders`: добавляются требования multi-replica/Kafka (дельта, ADDED);
  требование «Single-flight background order processing» обобщается до
  «не больше одного in-flight обработки на заказ **во всех репликах**».

## Scope

- `orders/src/main/java/**` и `orders/src/test/java/**` (main + тесты), `orders/pom.xml`.
- Dev/verification-инфраструктура **только на этой feature-ветке, не для CI курса**:
  `docker-compose.local.yml`, `local/` (Kafka KRaft-конфиг, load-harness), и
  артефакты этого change в `openspec/changes/`.
- Ветка: `feature/orders-kafka-replicas` (от снапшота `24580ff`).

**Отклонение от курсового правила** (утверждено пользователем): context/конфиг
запрещают изменения вне `orders/` — это ограничение **CI сдачи курса**. Данный change
осознанно работает за его пределами на отдельной feature-ветке для production-цели;
`master`/сдача курса остаётся in-memory и не трогается. Файлы вне `orders/` в этом
change — dev-only harness, в курсовую ветку не попадают.

## Non-Goals

- Изменения `contracts/*.yaml`, `mocks/`, `runner/`, `cmd/` (read-only стенд).
- Разворачивание Kafka **в самом стенде** (невозможно: read-only `docker-compose.yml`).
- Debezium/CDC, schema registry, enforcement Kafka-ACL (config-ready только).
- Мульти-регион / multi-DC.
- Замена in-memory транспорта дефолтом для курса (останется дефолтом для CI).

## Impact

- **Код**: `orders/` — новые пакеты `orders.kafka` (consumer, DLQ, config),
  `orders.outbox` (relay, модель), правки `flow/` (ChargeDispatcher,
  single-leader reaper), `store/` (claim, outbox tx, breaker-store), `config/`,
  `pom.xml` (kafka-clients, micrometer, testcontainers).
- **Инфраструктура (dev-only)**: `docker-compose.local.yml`, `local/`.
- **API / БД стенда / конфиг**: контракт `contracts/orders.yaml` не меняется. В `orders/`
  добавляются таблицы `circuit_breaker`, `outbox` (и claim-колонки) — создаются
  сервисом при старте, как `orders` (инвариант: схема — сервисом).
- **Тесты**: `cd orders && mvn -q test` (юнит + testcontainers Kafka/Postgres);
  локальный N-replica harness + failure drills; курсовой стенд `make scenarios TASK=1`
  — S1–S5 `PASS` на дефолтном in-memory.
