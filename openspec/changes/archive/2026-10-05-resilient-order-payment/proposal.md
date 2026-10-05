# Change: resilient-order-payment

## Why

Текущее содержимое `orders/` — наивный Go-пример, который проваливает сценарии S2–S5 задания 1
«Деньги не списываются дважды» (три бага — см. 07-anti-patterns.md «Bad example»):

1. повторяет `POST /payments` **без Idempotency-Key** → двойные списания (S2, S5);
2. ходит в Payment **без тайм-аутов** и синхронно ждёт → ответ на `POST /orders` превышает 1 с (S3, S4);
3. отвечает **5xx** при любой недоступности Payment/Inventory вместо `201 pending`;
4. продолжает слать запросы в Payment **во время его блэк-хола** → превышает бюджет S4.

Цель — переписать Orders на Java (Spring Boot, ADR-001) так, чтобы задание 1 прошло на уровне
**«полностью»**: все 5 сценариев `PASS`, ни одного двойного списания, каждый ответ на `POST /orders`
≤ 1 с, в конце прогона нет `pending`, S4 в пределах бюджета запросов.

## What changes

- Заменить содержимое `orders/` (Go-пример) на Java-сервис: Maven-проект Spring Boot 3 (Java 21)
  + multi-stage `Dockerfile`.
- Три эндпоинта контракта `contracts/orders.yaml`: `GET /healthz`, `POST /orders`, `GET /orders/{id}`.
- **Идемпотентное списание**: каждый `POST /payments` несёт `Idempotency-Key: <order_id>`.
- **Быстрый/медленный путь**: в рамках ~1 с — попытка довести до `paid`/`rejected`; иначе
  `201 pending` + фоновая обработка.
- **Фоновые воркеры**: доводят все `pending` до terminal до конца прогона; опираются на БД
  (реестр pending в БД) — заказы не теряются после рестарта процесса.
- **Реконсиляция** через `GET /payments?order_id=` после неопределённого исхода (lost response, тайм-аут).
- **Circuit breaker** на Payment: срабатывает на тайм-аут/ошибку соединения, ограничивает число
  запросов во время блэк-хола (S4).
- Схема БД создаётся при старте; `amount_cents = Σ qty × price_cents`.

## Scope

- Только каталог `orders/`: код, `pom.xml`, `Dockerfile`, `src/`, `resources/`. Допустим `.gitignore`.
- Уровень «полностью»: S1–S5 `PASS`, в конце прогона нет `pending`, нет списаний без `paid`.

## Non-goals

- Не менять ничего вне `orders/` (mocks/, runner/, cmd/, .github/, .etc/, Makefile,
  docker-compose.yml, contracts/, docs/, README.md, GRADING.md) — CI сверяет diff с baseline
  и проваливает задание при любом отличии.
- Не моделировать отказы Inventory (в задании 1 Inventory не отказывает) — только идемпотентные
  вызовы `reserve`/`release`.
- Не подгонять решение под открытые числа `runner/params/task1.json` (доли, задержки, лимит 30
  запросов, пауза 30 с) — на проверке другие параметры; реализуем свойства, а не числа.
- Не вводить ORM (JPA), очереди сообщений (Kafka/RabbitMQ), внешние сервисы и зависимости
  «на потом» — только то, что обосновано задачей.
