# Design: fix-interrupt-and-jsonb

## D1. Interrupt flag hygiene in PaymentClient

### Проблема

`java.net.http.HttpClient` бросает `IOException` (не `InterruptedException`) при таймауте.
Исходный код восстанавливал interrupt flag во всех исключениях:

```java
} catch (InterruptedException e) {
    Thread.currentThread().interrupt();  // OK — real interrupt
    return ChargeResult.NETWORK;
} catch (java.io.IOException e) {
    Thread.currentThread().interrupt();  // BUG — IOException таймаут не прерывает поток
    return ChargeResult.NETWORK;
}
```

После `IOException` флаг прерывания установлен. Следующий вызов `Thread.sleep()` или
`HttpClient.send()` на том же воркере немедленно бросает `InterruptedException`.
Воркер умирает, заказ остаётся в `pending` навечно.

### Решение

В `catch (IOException)` **не трогаем** флаг прерывания:

```java
} catch (java.io.IOException e) {
    // Timeout or connection drop. Do NOT set the interrupt flag —
    // it would break subsequent blocking calls (hasCharges, sleep)
    // on this worker thread.
    return ChargeResult.NETWORK;
}
```

`InterruptedException` — только при реальном `Thread.interrupt()` (shutdown, таймаут воркера).
Там flag восстанавливаем.

Аналогично для `hasCharges()`.

## D2. Cross-dialect SQL in OrderRepository

### Проблема

Схема БД (`schema.sql`):
```sql
items JSONB NOT NULL
```

При `INSERT` PostgreSQL требует явного приведения:
```
ERROR: column "items" is of type jsonb but expression is of character varying
```

H2 работает с `VARCHAR` без проблем — тесты проходили, продакшн падал.

### Решение

Автодетекция типа БД в конструкторе:

```java
private static boolean detectPostgres(DataSource dataSource) {
    try (Connection c = dataSource.getConnection()) {
        return "PostgreSQL".equalsIgnoreCase(c.getMetaData().getDatabaseProductName());
    } catch (Exception e) {
        return false;
    }
}
```

В `insertPending()`:
```java
if (usingPostgres) {
    sql = "INSERT INTO orders ... CAST(? AS JSONB) ...";
} else {
    sql = "INSERT INTO orders ... ? ...";
}
```

## D3. Circuit breaker state before reject in OrderProcessor

### Проблема

В `NETWORK` кейсе `circuitBreaker.onTimeoutOrConnError()` вызывался после `return true`:
```java
release(orderId);
if (orderRepository.transition(orderId, "rejected") > 0) return true;
circuitBreaker.onTimeoutOrConnError();  // никогда не выполняется
```

Брейкер оставался в `CLOSED` вместо `OPEN` после reject, что могло привести к повторному
пробросу запросов в недоступный Payment.

### Решение

Перенести вызов `onTimeoutOrConnError()` до перехода:
```java
circuitBreaker.onTimeoutOrConnError();
release(orderId);
if (orderRepository.transition(orderId, "rejected") > 0) return true;
```
