# Change: slim-datasource-config

## Why

`orders/store/DataSourceConfig` настраивает DataSource и JdbcTemplate явно,
хотя часть этой работы Spring Boot делает сам. Конкретно:

1. **Редундантный `JdbcTemplate` @Bean.** `JdbcTemplateAutoConfiguration`
   создаёт `JdbcTemplate` автоматически, когда в контексте ровно один
   `DataSource` и собственного бина `JdbcTemplate` нет — а мы его и
   объявляем, отключая автоконфигурацию и дублируя строку кода.
2. **Ручная настройка пула.** `HikariConfig` собирается вручную с
   `maximumPoolSize(5)`/`minimumIdle(1)` — параметры без обоснования задачей;
   дефолты Hikari (max 10) для сервиса с 8 фоновыми воркерами и
   виртуальными потоками не хуже. Единственный осмысленный параметр —
   короткий `connectionTimeout` (healthz обязан быстро ответить 503 на
   мёртвую БД, а не ждать 30 с по дефолту).
3. **Парсинг `DATABASE_URL` неотестуем.** Конвертация
   `postgres://user:pass@host:port/db?sslmode=...` → JDBC-URL + credentials
   встроена в тело @Bean-метода — проверить её юнит-тестом нельзя.

**Почему «только properties, без класса» невозможно** (проверено):
стенд передаёт `DATABASE_URL` в Heroku-формате
`postgres://orders:orders@postgres:5432/orders?sslmode=disable`
(`docker-compose.yml`, read-only). Spring Boot понимает только
`spring.datasource.url` (JDBC-URL) + отдельные `spring.datasource.username`/
`password`; плейсхолдеры properties не умеют разбирать URI, а формат
переменной менять нельзя. Поэтому небольшой @Configuration-класс остаётся —
но только для формата конвертации.

**Отдельно по деньгам (проверено, не меняется).** Запрос «использовать
BigDecimal для денег» отклонён: курсовой стандарт 04-engineering-standards.md
требует «все денежные величины — `long` (центы), никакого float/double»;
контракты `orders.yaml`/`payment.yaml` фиксируют `amount_cents`/`price_cents`
как `integer, format: int64` (раннер сверяет ответы byte-уровнем); арифметика —
только целочисленные `qty × price_cents` и суммы. BigDecimal противоречил бы
и стандарту, и read-only контракту.

## What changes

### 1. Один @Bean вместо двух (design D1)

`DataSourceConfig` — единственный @Bean: `dataSource(@Value("${DATABASE_URL}"))`
→ `HikariDataSource` (JDBC-URL из `postgres://`, credentials из userInfo,
`connectionTimeout=5000`). Явный `JdbcTemplate` @Bean **удаляется** —
автоконфигурация Boot создаёт его от нашего DataSource.
`@ConditionalOnProperty(name = "DATABASE_URL")` сохраняется.

### 2. Конвертация URL — тестуемый статический метод (design D2)

Парсинг выносится: `DataSourceConfig.databaseUrl(String) → ParsedUrl`
(record `jdbcUrl, user, password`). Юнит-тесты: с паролем, без пароля,
с query-параметрами, без query, без порта.

## Scope

- `orders/src/main/java/orders/store/DataSourceConfig.java`
- Новый: `orders/src/test/java/orders/store/DataSourceConfigTest.java`

## Non-goals

- `BigDecimal` для денег — отклонён (стандарт + контракт, см. Why).
- Формат `DATABASE_URL` и `docker-compose.yml` — read-only.
- `spring.datasource.hikari.*` properties — неприменимы к кастомному
  DataSource-бину (связываются только с автоконфигурируемым); параметры
  остаются в коде.
- Изменения `application.properties` сверх необходимости (не требуется).
- Изменения поведения charge-пути, брейкера, идемпотентности.
- Изменения вне `orders/`.
