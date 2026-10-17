package orders.store;

import lombok.RequiredArgsConstructor;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.sql.Timestamp;
import java.time.Duration;
import java.util.List;
import java.util.stream.Collectors;

/**
 * Репозиторий таблицы outbox_events (паттерн Transactional Outbox).
 *
 * === ЗАЧЕМ ===
 *
 * Прямая отправка события в Kafka (dual-write) ненадёжна: транзакция БД
 * закоммичена, а send() в Kafka упал → событие потеряно навсегда, заказ
 * навсегда PENDING. Откатить БД после коммита нельзя.
 *
 * Outbox решает это атомарно: событие пишется В ТОЙ ЖЕ транзакции, что
 * и заказ. Гарантия: либо обе записи в БД, либо ни той, ни другой.
 * OutboxDispatcher потом шлёт неотправленные события в Kafka и помечает
 * их sent_at. Доставка at-least-once: падение между send и markSent
 * даст дубль события, но Consumer идемпотентен (проверка статуса
 * заказа + Idempotency-Key в Payment).
 */
@Repository
@RequiredArgsConstructor
public class OutboxRepository {

    private final JdbcTemplate jdbc;

    /**
     * Положить событие в outbox.
     *
     * ВАЖНО: вызывается внутри @Transactional заказа. JdbcTemplate
     * берёт Connection из того же DataSource → участвует в той же
     * транзакции. Если транзакция откатится — событие исчезнет вместе
     * с заказом (это и нужно).
     */
    public void insert(String orderId, String eventType, String payload) {
        String sql = """
            INSERT INTO outbox_events (order_id, event_type, payload)
            VALUES (?, ?, ?)
            """;
        jdbc.update(sql, orderId, eventType, payload);
    }

    /**
     * Заявить (claim) пачку неотправленных событий для отправки в Kafka.
     *
     * === ЗАЧЕМ CLAIMING (найдено стресс-тестом на 5000 заказов/с) ===
     *
     * Раньше каждая из 5 реплик читала `WHERE sent_at IS NULL LIMIT n` —
     * все ОДНИ И ТЕ ЖЕ строки головы очереди. Следствия на 60 000 заказов:
     *   — Kafka получала ~3,3 события на заказ (дубли), коньюмеры платили
     *     Payment по 199 246 запросов вместо 60 000 (139 000 из них —
     *     идемпотентные «уже оплачено»);
     *   — 5 реплик делали UPDATE sent_at по одним строкам → блокировки
     *     row-lock в Postgres (замерено: CPU 253%);
     *   — перегруженная заглушка mocks отдавала reserve() медленнее
     *     (до 540 мс) и POST /orders упирался в бюджет.
     *
     * === КАК РАБОТАЕТ ===
     *
     * FOR UPDATE SKIP LOCKED — каноничный шардированный воркер очереди:
     * параллельные заявители НЕ ждут друг друга, а пропускают занятые
     * строки и берут следующие. Каждая строка заявляет ровно одной
     * репликой.
     *
     * claimed_at — «тайм-аут видимости»: если реплика упала между claim
     * и markSent, строка не должна висеть вечно — другая реплика (или
     * та же после рестарта) заберёт её через visibilityTimeout. Отсюда
     * же at-least-once: дедупликация — на стороне Consumer'а (проверка
     * статуса) и Payment (Idempotency-Key), как и было.
     */
    public List<OutboxEvent> claimBatch(int limit, Duration visibilityTimeout) {
        String sql = """
            WITH claimed AS (
                SELECT id FROM outbox_events
                WHERE sent_at IS NULL
                  AND (claimed_at IS NULL
                       OR claimed_at < now() - make_interval(secs => ?))
                ORDER BY id
                FOR UPDATE SKIP LOCKED
                LIMIT ?
            )
            UPDATE outbox_events o
            SET claimed_at = now()
            FROM claimed c
            WHERE o.id = c.id
            RETURNING o.id, o.order_id, o.event_type, o.payload, o.created_at
            """;
        return jdbc.query(sql, (rs, rowNum) -> new OutboxEvent(
                rs.getLong("id"),
                rs.getString("order_id"),
                rs.getString("event_type"),
                rs.getString("payload"),
                rs.getTimestamp("created_at")
        ), visibilityTimeout.toMillis() / 1000.0, limit);
    }

    /**
     * Пометить пачку событий отправленными ОДНИМ запросом.
     *
     * Пачка, а не по строке: на 5000 событий/с построчные UPDATE давали
     * 5000 запросов/с только на пометку. Пачка ≤100 строк — один round
     * trip. Дубль после падения между send и markSent ограничен размером
     * пачки и безопасен (idempotent Consumer + Idempotency-Key).
     */
    public void markSent(List<Long> ids) {
        if (ids.isEmpty()) {
            return;
        }
        // Строка id через запятую → распаковка в bigint[] на стороне БД:
        // это даёт одну команду UPDATE на всю пачку без ручного
        // PreparedStatement (массивы JDBC через JdbcTemplate — лишняя
        // возня, а id — числа без какого-либо «текстового» риска).
        String csv = ids.stream().map(String::valueOf).collect(Collectors.joining(","));
        jdbc.update("UPDATE outbox_events SET sent_at = now() "
                + "WHERE id = ANY(string_to_array(?, ',')::bigint[])", csv);
    }

    /**
     * Событие outbox.
     * payload — тело сообщения в Kafka (сейчас это id заказа).
     */
    public record OutboxEvent(long id, String orderId, String eventType,
                              String payload, Timestamp createdAt) {}
}
