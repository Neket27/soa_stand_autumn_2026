# Design: slim-datasource-config

Уменьшение `DataSourceConfig` до единственной обязанности: конвертация
`DATABASE_URL` (Heroku-формат стенда) в JDBC-DataSource. Всё остальное —
автоконфигурация Spring Boot.

## Текущая форма

```java
@Configuration
@ConditionalOnProperty(name = "DATABASE_URL")
public class DataSourceConfig {
    @Bean
    public DataSource dataSource(@Value("${DATABASE_URL}") String databaseUrl)
            throws URISyntaxException { /* парсинг URI + новый HikariConfig
                с maximumPoolSize(5)/minimumIdle(1)/connectionTimeout(5000) */ }

    @Bean
    public JdbcTemplate jdbcTemplate(DataSource dataSource) {
        return new JdbcTemplate(dataSource);   // дублирует автоконфигурацию
    }
}
```

## Почему «только properties» — невозможно (проверено)

Стенд (read-only) даёт: `DATABASE_URL=postgres://orders:orders@postgres:5432/orders?sslmode=disable`
(`docker-compose.yml:54`). Требования Boot-автоконфигурации DataSource:
`spring.datasource.url` — **JDBC**-URL (`jdbc:postgresql://host:port/db`) и
отдельные `spring.datasource.username` / `password`. Из одной переменной
`postgres://user:pass@host:port/db` в properties:

- не поменять схему (`postgres://` → `jdbc:postgresql://`) — плейсхолдеры
  не умеют string-surgery;
- не извлечь `user`/`pass` из `userInfo` URI;
- query-часть (`sslmode=disable`) нужно сохранить в JDBC-URL.

Класс с конвертацией неизбежен; вопрос только в его размере.

## D1. Один @Bean: `DataSource`

```java
@Configuration
@ConditionalOnProperty(name = "DATABASE_URL")
public class DataSourceConfig {

    @Bean
    public DataSource dataSource(@Value("${DATABASE_URL}") String databaseUrl) {
        ParsedUrl url = databaseUrl(databaseUrl);
        HikariDataSource ds = new HikariDataSource();
        ds.setJdbcUrl(url.jdbcUrl());
        ds.setUsername(url.user());
        ds.setPassword(url.password());
        ds.setConnectionTimeout(5000);
        return ds;
    }

    static ParsedUrl databaseUrl(String databaseUrl) { /* парсинг */ }

    record ParsedUrl(String jdbcUrl, String user, String password) {}
}
```

- **`JdbcTemplate` @Bean удалён.** `JdbcTemplateAutoConfiguration`
  (`@ConditionalOnSingleCandidate(DataSource)` +
  `@ConditionalOnMissingBean(JdbcTemplate)`) создаёт бин сама: у нас ровно
  один DataSource (наш), `spring-jdbc` на classpath (starter-jdbc). Проверка —
  реальным стартом: healthz 200 + S1 (см. tasks).
- **`maximumPoolSize`/`minimumIdle` убраны** — дефолты Hikari (max 10,
  minIdle = max): у сервиса 8 фоновых воркеров + виртуальные HTTP-потоки,
  пул 10 покрывает; ручные 5/1 не были обусловлены задачей (04-standards:
  каждая деталь должна быть обоснована).
- **`connectionTimeout=5000` остаётся** (единственный осмысленный параметр):
  `healthz` → `isReady()` → `SELECT 1`; при мёртвой БД Hikari иначе держал бы
  запрос до дефолтных 30 с. Fail-fast readiness: раннер ждёт healthz до
  2 минут, 5 с на попытку — с запасом.
- `@ConditionalOnProperty(name = "DATABASE_URL")` сохраняется: без
  `DATABASE_URL` (локальный запуск без стенда) бин не создаётся.
- `spring.sql.init.mode=always` + `schema.sql` — без изменений:
  `SQLInitializationAutoConfiguration` работает с любым DataSource-бином.

## D2. Конвертация — тестуемый статический метод

`databaseUrl(String) → ParsedUrl(jdbcUrl, user, password)`:

| Вход | `jdbcUrl` | user | password |
|---|---|---|---|
| `postgres://orders:orders@postgres:5432/orders?sslmode=disable` | `jdbc:postgresql://postgres:5432/orders?sslmode=disable` | `orders` | `orders` |
| `postgres://user@host:5432/db` | `jdbc:postgresql://host:5432/db` | `user` | `""` |
| `postgres://orders:orders@postgres:5432/orders` | `jdbc:postgresql://postgres:5432/orders` | `orders` | `orders` |

`URI.create` вместо `new URI(...)` (без checked `URISyntaxException`): на
старте сервис и так умирает с мусорным `DATABASE_URL` — `IllegalArgumentException`
в логгах старта честнее, чем декларируемое исключение в сигнатуре @Bean.

## Обязательные вопросы дизайна

**Как соблюдается бюджет 1 с.** Неизменно: sync-путь `POST /orders`
(`insert` + `reserve`) использует тот же JdbcTemplate (теперь из
автоконфигурации) и те же тайм-ауты; изменение конфигурации пула (5→10)
не влияет на задержки в норме — коннекты к локальному Postgres < 1 мс.

**Как исключено двойное списание.** Неизменно: charge-машина
(`OrderFulfiller`), идемпотентность и брейкер не трогаются; пул коннекций
не участвует в гарантиях «один charge на заказ».

**Как заказ с потерянным ответом станет paid.** Неизменно: reaper +
`OrderFulfiller` работают на тех же бинах.

**Как ограничивается нагрузка на Payment в блэк-холе.** Неизменно:
`BackgroundWorker`/брейкер не трогаются; пул БД на запросы к Payment не
влияет.

## Риски

- Упразднение явного `JdbcTemplate` @Bean — опора на автоконфигурацию без
  тестов контекста в проекте. Снятие риска: интеграционная проверка
  реальным стартом (`make up` → healthz 200 → S1): healthz 200 доказывает,
  что `isReady()` выполнил SQL через автоконфигурированный JdbcTemplate.
- Дефолтный пул 10 вместо 5: при одновременной нагрузке держится до 10
  коннектов к Postgres — для стенда безлимитно (Postgres в том же compose).
