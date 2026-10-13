package orders.store;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import orders.api.dto.Item;
import orders.api.dto.Order;
import orders.flow.OrderStatus;

import java.util.List;

/**
 * Репозиторий для работы с таблицей orders в PostgreSQL.
 *
 * === СХЕМА ТАБЛИЦЫ ===
 * CREATE TABLE orders (
 *     id           TEXT PRIMARY KEY,
 *     user_id      TEXT NOT NULL,
 *     status       TEXT NOT NULL,  -- PENDING, PAID, REJECTED
 *     amount_cents BIGINT NOT NULL,
 *     items        JSONB NOT NULL,
 *     created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
 *     release_pending BOOLEAN DEFAULT FALSE  -- нужно ли снять резерв
 * );
 *
 * === ЧТО СДЕЛАТЬ САМОМУ (S1-S2) ===
 *
 * S1: При отклонении банком:
 *     — Перевести заказ в REJECTED
 *     — Установить release_pending = TRUE
 *     — Снять резерв через Inventory API
 *     — Если снятие удалось — release_pending = FALSE
 *
 * S2: При 500 от Payment:
 *     — Оставить заказ в PENDING
 *     — Consumer должен повторить попытку
 *     — Если retry исчерпан — перевести в REJECTED? Или оставить PENDING?
 */
@Repository
public class OrderRepository {

    private final JdbcTemplate jdbc;

    public OrderRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public void insertPending(String orderId, String userId, long amountCents, List<Item> items) {
        String sql = """
            INSERT INTO orders (id, user_id, status, amount_cents, items)
            VALUES (?, ?, 'PENDING', ?, ?::jsonb)
            """;
        String itemsJson = toJson(items);
        jdbc.update(sql, orderId, userId, amountCents, itemsJson);
    }

    /**
     * Атомарный переход заказа в новый статус.
     * Используется для предотвращения двойной оплаты.
     *
     * === ЧТО СДЕЛАТЬ САМОМУ ===
     *
     * Условие WHERE status = 'PENDING' гарантирует, что заказ
     * не будет переведён дважды (например, двумя consumer-репликами).
     */
    public boolean transition(String orderId, OrderStatus newStatus) {
        String sql = "UPDATE orders SET status = ? WHERE id = ? AND status = 'PENDING'";
        int updated = jdbc.update(sql, newStatus.name(), orderId);
        return updated > 0;
    }

    public Order findById(String orderId) {
        String sql = "SELECT id, status, amount_cents FROM orders WHERE id = ?";
        List<Order> results = jdbc.query(sql,
                (rs, rowNum) -> new Order(
                        rs.getString("id"),
                        rs.getString("status"),
                        rs.getLong("amount_cents")
                ),
                orderId
        );
        return results.isEmpty() ? null : results.get(0);
    }

    public OrderStatus getStatus(String orderId) {
        String sql = "SELECT status FROM orders WHERE id = ?";
        List<String> results = jdbc.queryForList(sql, String.class, orderId);
        return results.isEmpty() ? null : OrderStatus.valueOf(results.get(0));
    }

    /**
     * Получить список PENDING заказов для reaper.
     * Reaper периодически проверяет PENDING заказы и повторяет попытку.
     */
    public List<String> findPendingIds() {
        String sql = "SELECT id FROM orders WHERE status = 'PENDING'";
        return jdbc.queryForList(sql, String.class);
    }

    /**
     * Получить заказы с release_pending = TRUE.
     * Это REJECTED заказы, у которых не удалось снять резерв.
     */
    public List<String> findReleasePendingIds() {
        String sql = "SELECT id FROM orders WHERE release_pending = TRUE";
        return jdbc.queryForList(sql, String.class);
    }

    public void setReleasePending(String orderId, boolean pending) {
        String sql = "UPDATE orders SET release_pending = ? WHERE id = ?";
        jdbc.update(sql, pending, orderId);
    }

    public void isReady() {
        jdbc.queryForObject("SELECT 1", Integer.class);
    }

    private String toJson(List<Item> items) {
        // Простая сериализация JSON вручную
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < items.size(); i++) {
            Item item = items.get(i);
            if (i > 0) sb.append(",");
            sb.append(String.format(
                    "{\"sku\":\"%s\",\"qty\":%d,\"price_cents\":%d}",
                    item.sku(), item.qty(), item.priceCents()
            ));
        }
        sb.append("]");
        return sb.toString();
    }
}
