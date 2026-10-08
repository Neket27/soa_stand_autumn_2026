# Tasks: slim-datasource-config

Сборка/тесты: `cd orders && JAVA_HOME=~/.jdks/temurin-21.0.12.1 mvn -q test`.
Каждая задача оставляет компилируемое состояние с зелёными тестами и
прогоном сценария раннера. Изменения только в `orders/`.

- [x] Task 1: Тестуемая конвертация DATABASE_URL
- [x] Task 2: Минимальный DataSourceConfig (один @Bean, без JdbcTemplate-бина)
- [x] Task 3: Интеграционная проверка (mvn test + make up + make scenarios TASK=1)

---

## Task 1: Тестуемая конвертация DATABASE_URL

**Files:**
- `orders/src/main/java/orders/store/DataSourceConfig.java`
- Новый: `orders/src/test/java/orders/store/DataSourceConfigTest.java`

**Changes:**
1. В `DataSourceConfig` — статический метод
   `databaseUrl(String) → record ParsedUrl(String jdbcUrl, String user,
   String password)` с логикой из текущего @Bean (design D2):
   `URI.create`, схема `jdbc:postgresql://host:port/path[?query]`,
   user/password из `userInfo` (`user:pass` / `user`).
2. @Bean `dataSource` вызывает `databaseUrl(...)` и настраивает
   `HikariDataSource` (`setJdbcUrl/setUsername/setPassword/
   setConnectionTimeout(5000)`).
3. **Пока** `JdbcTemplate` @Bean не удаляем (Task 2) — состояние
   компилируемое, поведение то же.

**Tests:** `DataSourceConfigTest` (без Spring-контекста):
- `parsesUrlWithPasswordAndQuery` — таблица design D2, строка 1.
- `parsesUrlWithoutPassword` — строка 2.
- `parsesUrlWithoutQuery` — строка 3.

**Verification:** `mvn -q test` — green.
`make scenarios ONLY=S1 WAIT=5` — PASS (поведение неизменно).

---

## Task 2: Минимальный DataSourceConfig

**Files:**
- `orders/src/main/java/orders/store/DataSourceConfig.java`

**Changes:**
1. Удалить @Bean `JdbcTemplate` — `JdbcTemplateAutoConfiguration` создаёт
   бин от единственного DataSource (design D1).
2. Убрать ручные `maximumPoolSize`/`minimumIdle` — дефолты Hikari;
   `connectionTimeout(5000)` сохранить (fail-fast healthz).
3. Убрать лишние импорты (`HikariConfig`, `Qualifier`).
   Итог — класс ~35 строк: @Bean + `databaseUrl` + `ParsedUrl`.

**Tests:** существующие (`DataSourceConfigTest` + все юниты) зелёные;
проверка наличия автоконфигурируемого `JdbcTemplate` — в Task 3
реальным стартом (healthz 200 = `isReady()` выполнил SQL).

**Verification:** `mvn -q test` — green.
`make up` → `healthz` 200 (контекст поднимается, JdbcTemplate из
автоконфигурации, schema.sql применился).
`make scenarios ONLY=S1 WAIT=5` — PASS.

---

## Task 3: Интеграционная проверка

**Files:** — (проверка, не изменение)

**Changes:**
1. `cd orders && JAVA_HOME=~/.jdks/temurin-21.0.12.1 mvn -q test` — все
   тесты зелёные.
2. `make scenarios TASK=1 WAIT=10` — S1–S5 `PASS`, уровень «полностью»;
   сверка с результатами до change: те же статусы, двойных списаний нет.
3. Визуальный check: в `orders/` ровно один `@Bean DataSource`, явного
   `@Bean JdbcTemplate` нет; `application.properties` не вырос.

**Rollback:** `git revert` по задаче; поведение до change — в коммите,
прошедшем `make scenarios TASK=1`.
