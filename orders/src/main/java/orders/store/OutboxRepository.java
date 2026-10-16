package orders.store;

import lombok.RequiredArgsConstructor;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.sql.Timestamp;
import java.util.List;

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
     * Неотправленные события в порядке записи (по id).
     * limit — чтобы не вытягивать миллионы строк одной пачкой.
     */
    public List<OutboxEvent> findUnsent(int limit) {
        String sql = """
            SELECT id, order_id, event_type, payload, created_at
            FROM outbox_events
            WHERE sent_at IS NULL
            ORDER BY id
            LIMIT ?
            """;
        return jdbc.query(sql, (rs, rowNum) -> new OutboxEvent(
                rs.getLong("id"),
                rs.getString("order_id"),
                rs.getString("event_type"),
                rs.getString("payload"),
                rs.getTimestamp("created_at")
        ), limit);
    }

    /** Пометить событие отправленным. */
    public void markSent(long id) {
        jdbc.update("UPDATE outbox_events SET sent_at = now() WHERE id = ?", id);
    }

    /**
     * Событие outbox.
     * payload — тело сообщения в Kafka (сейчас это id заказа).
     */
    public record OutboxEvent(long id, String orderId, String eventType,
                              String payload, Timestamp createdAt) {}
}
