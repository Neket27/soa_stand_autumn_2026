# Tasks — Перевод комментариев main-кода Orders (russify-main-comments)

Правило перевода (к каждой задаче): переводится только несущий текст комментариев;
идентификаторы, ключи конфигурации, HTTP-статусы, значения статусов, SQL-литералы и
устоявшиеся термины (`Idempotency-Key`, `POST-first`, circuit breaker, blackhole,
reconciliation, reaper) остаются на английском. Код, строки логирования и сообщения
исключений не меняются.

## 1. Пакет `flow` (основной объём)

- [x] 1.1 Перевести комментарии `orders/src/main/java/orders/flow/OrderCharger.java` (~43 строки). Проверка: `cd orders && mvn -q test` — зелёный; `git diff` — только комментарии.
- [x] 1.2 Перевести комментарии `orders/src/main/java/orders/flow/InMemoryChargeDispatcher.java` (~17). Проверка: `cd orders && mvn -q test` — зелёный.
- [x] 1.3 Перевести комментарии `orders/src/main/java/orders/flow/PaymentCircuitBreaker.java` (~15). Проверка: `cd orders && mvn -q test` — зелёный.
- [x] 1.4 Перевести комментарии `orders/src/main/java/orders/flow/CreateOrderService.java` (~9). Проверка: `cd orders && mvn -q test` — зелёный.
- [x] 1.5 Перевести комментарий `orders/src/main/java/orders/flow/OrderStatus.java` (1). Проверка: `cd orders && mvn -q test` — зелёный.

## 2. Остальные пакеты

- [x] 2.1 Перевести комментарии `orders/src/main/java/orders/store/DataSourceConfig.java` (~9) и `orders/src/main/java/orders/store/OrderRepository.java` (~8). Проверка: `cd orders && mvn -q test` — зелёный (включая `OrderRepositoryTest` на H2).
- [x] 2.2 Перевести комментарии `orders/src/main/java/orders/payment/PaymentClient.java` (~5), `orders/src/main/java/orders/api/OrderQueryService.java` (~4), `orders/src/main/java/orders/api/OrderNotFoundException.java` (1), `orders/src/main/java/orders/api/dto/OrderRequest.java` (1). Проверка: `cd orders && mvn -q test` — зелёный.

## 3. Ресурсы

- [x] 3.1 Перевести комментарии `orders/src/main/resources/schema.sql` (2 строки). Проверка: `cd orders && mvn -q test` — зелёный (схема применяется при старте на H2).
- [x] 3.2 Перевести комментарии `orders/Dockerfile` (2 строки). Проверка: `docker build orders/` — собирается без аргументов (как проверяет CI).

## 4. Финальная верификация

- [x] 4.1 Проверить `git diff --stat` и `git diff`: изменены только файлы из Scope (11 Java-файлов, `schema.sql`, `Dockerfile`), в каждом — только строки комментариев; `orders/src/test/**` и код не задеты.
- [x] 4.2 `cd orders && mvn -q test` — весь набор тестов зелёный.
- [x] 4.3 `make scenarios TASK=1` — S1–S5 `PASS` (финальный гейт: поведение не изменилось).
