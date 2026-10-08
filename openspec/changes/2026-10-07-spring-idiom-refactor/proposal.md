# Change: spring-idiom-refactor

## Why

Текущая Java-реализация использует Spring Boot как инфраструктурный слой (web, JDBC, scheduling),
но wiring бизнес-компонентов сделан через центральную `AppConfig` с `@Bean`-методами.
Классы `PaymentClient`, `InventoryClient`, `OrderProcessor`, `PaymentCircuitBreaker` — plain POJO
без Spring-аннотаций. `ObjectMapper` создаётся статически (`new ObjectMapper()`) вместо инъекции
Spring-managed бина. JSON для исходящих запросов строится через `String.format`.

Кроме того, в проекте нет Lombok — конструкторы контроллеров и репозиторийных классов
написаны вручную (5–7 параметров). Валидация входящих данных выполняется вручную через
метод `valid()` в контроллере вместо Bean Validation аннотаций.

Код работает (S1–S5 PASS), но читается как «Java с подмешанным Spring» — неясно, какой
компонент кто создаёт, и теряется стандартная Spring-идиоматика, которую ожидает ревьюер.

## What changes

### Ветка A: Spring idiom (уже реализовано)

- Перенос wiring с `@Bean`-методов на компонентные аннотации (`@Service`, `@Component`).
- Инъекция `ObjectMapper` из Spring context вместо `static new ObjectMapper()`.
- JSON-сериализация исходящих HTTP-тел через Jackson вместо `String.format`.
- Удаление `AppConfig` как источника бин'ов (остаётся только `@EnableScheduling`).
- `@Value`-инъекция конфигурации в конструкторы вместо ручного чтения в конфиг-классе.

### Ветка B: Lombok

- Добавление `lombok` в `pom.xml` (compile scope + annotation processor path).
- `@RequiredArgsConstructor` на `OrdersController` и `OrderRepository` — вместо ручных конструкторов.

### Ветка C: Bean Validation

- Аннотации `@NotBlank`, `@Size(min=1)`, `@Min(1)`, `@DecimalMin` на поля DTO (`OrderRequest`, `Item`).
- `@Valid` на `@RequestBody` в контроллере.
- `@RestControllerAdvice` с обработчиком `MethodArgumentNotValidException` → `400 Bad Request`.
- Удаление ручного метода `valid()` из контроллера.

## Scope

- `orders/pom.xml`
- `orders/src/main/java/orders/config/AppConfig.java` (удалён)
- `orders/src/main/java/orders/payment/PaymentClient.java`
- `orders/src/main/java/orders/inventory/InventoryClient.java`
- `orders/src/main/java/orders/flow/OrderProcessor.java`
- `orders/src/main/java/orders/flow/PaymentCircuitBreaker.java`
- `orders/src/main/java/orders/flow/BackgroundWorker.java`
- `orders/src/main/java/orders/api/OrdersController.java`
- `orders/src/main/java/orders/api/dto/OrderRequest.java`
- `orders/src/main/java/orders/api/dto/Item.java`
- `orders/src/main/java/orders/store/OrderRepository.java`
- `orders/src/main/java/orders/OrdersApplication.java`
- Новый: `orders/src/main/java/orders/api/ValidationExceptionHandler.java`
- Соответствующие тесты

## Non-goals

- Замена `java.net.http.HttpClient` на `RestClient`/`WebClient` — осознанный выбор (per-request timeout).
- Замена ручных daemon-потоков в `BackgroundWorker` на `ThreadPoolTaskExecutor` — осознанный выбор (persistent consumer pattern).
- Переход на Spring Data JPA — схема 1 таблица, `JdbcTemplate` достаточно.
- Изменение API, контрактов, бизнес-логики или поведения при отказах.
- Изменение вне `orders/`.
