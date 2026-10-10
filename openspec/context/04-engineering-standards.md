---
owner: Кульга Н. И.
updated: 2026-10-04
---
# Engineering Standards

## Стек [🟢 Абсолютно уверен (≈100-80%)]
- **Java 21 (Temurin, LTS)** — для локальных `mvn test`/IDEA нужен JDK 21 (ADR-001, ревизия 2026-10-05). Виртуальные потоки — только на стороне обработки HTTP-запросов; фоновая обработка — ограниченный пул (бюджет S4).
- **Spring Boot 3.x** (3.3+) — `spring-boot-starter-web` (эндпоинты), доступ к БД — `spring-boot-starter-jdbc` (HikariCP) или `starter-data-jpa` (выбор — в 03, открытые вопросы).
- **Maven 3.8.7** — сборка и локальные тесты (`orders/pom.xml`).
- **PostgreSQL 16** (драйвер `org.postgresql:postgresql`) — база `orders`, строка подключения из `DATABASE_URL`.
- **Docker** — multi-stage образ: сборка `maven:3.9-eclipse-temurin-21`, рантайм — JRE 21 (`eclipse-temurin:21-jre`).

Собственный стек стенда — Go 1.26 (mocks, runner, cmd/grade): это read-only и на Orders не влияет.

## Стайл-гайд
Отдельного стайл-гайда курса нет; стандартные конвенции Spring Boot:
- Package по фиче (например, `orders.api`, `orders.payment`, `orders.inventory`, `orders.store`); constructor injection; DTO без экзотических библиотек.
- Все денежные величины — `BigDecimal`, никаких float/double.
- Явные тайм-ауты на каждый исходящий HTTP-вызов; общий клиент конфигурируется один раз.
- Логирование: `order_id`, статус, сумма; никаких токенов и строк подключения.
- Не добавлять зависимости «на потом» — каждая зависимость должна быть обоснована задачей.

## Запуск проверок
Стенд (корень репозитория; нужен Docker Compose v2 + make):
- `make up` — поднять стенд с вашим сервисом (база пересоздаётся, сервис пересобирается).
- `make scenarios TASK=1` — полный прогон сценариев задания 1 (~4 мин, пауза 30 с).
- `make scenarios ONLY=S3 WAIT=5` — один сценарий с короткой паузой — для отладки.
- `make ui` — дашборд заглушек; `make logs` — логи; `make down` — остановить; `make docs` — контракты в Swagger UI (localhost:8091).
- Не запускать `make scenarios` во время прогона из дашборда (make откажет сам).

Сервис (каталог orders/):
- Сборка: `docker build orders/` — обязана собираться без аргументов (так проверяет CI).
- Тесты: `cd orders && mvn -q test` (если в сервисе есть тесты).
- Локальный smoke: `curl -s localhost:8080/orders -d '{"user_id":"u-1","items":[{"sku":"sku-1","qty":1,"price_cents":100}]}'`.

## Код готов, когда:
1. `make scenarios TASK=1` — все 5 сценариев `PASS` (цель: уровень «полностью»).
2. `cd orders && mvn -q test` — зелёный (при наличии тестов).
3. `git status` / `git diff` — изменены только файлы в `orders/` (допустим `.gitignore`).
4. PR: заголовок `ИСУ <номер>`, описание по шаблону (.github/pull_request_template.md).
