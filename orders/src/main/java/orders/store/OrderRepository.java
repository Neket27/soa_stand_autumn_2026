package orders.store;

import lombok.RequiredArgsConstructor;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import orders.api.dto.Item;
import orders.api.dto.Order;
import orders.flow.OrderStatus;

import java.time.Duration;
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
 * === СВЯЗЬ С БИЗНЕС-ПРОЦЕССОМ ===
 *
 * Отклонение банком (402): переход в REJECTED + release_pending = TRUE
 * (KafkaChargeDispatcher или Reaper) → снятие резерва через Inventory,
 * при успехе release_pending = FALSE. Если снятие упало — заказ REJECTED
 * не «зависает» с резервом: снятие повторяет Reaper по release_pending.
 *
 * Ошибка Payment (500/сеть): заказ остаётся PENDING, событие оплаты
 * ретраит Consumer; PENDING старше 30 с подбирает Reaper.
 */
@Repository
@RequiredArgsConstructor
public class OrderRepository {

    private final JdbcTemplate jdbc;

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
     *
     * Условие WHERE status = 'PENDING' гарантирует, что заказ переведёт
     * ровно кто-то один из конкурирующих «платильщиков» (Consumer и Reaper
     * работают параллельно и оба могут звонить в Payment). Возвращает
     * false, если статус уже кто-то сменил, — вызывающий код не должен
     * в этом случае доложить об успехе в лог.
     */
    public boolean transition(String orderId, OrderStatus newStatus) {
        String sql = "UPDATE orders SET status = ? WHERE id = ? AND status = 'PENDING'";
        int updated = jdbc.update(sql, newStatus.name(), orderId);
        return updated > 0;
    }

    /**
     * GET /orders/{id} отдаёт статус строго lowercase: "paid", "rejected",
     * "pending" — раннер сравнивает их с этим набором, uppercase "PAID"
     * он считает нарушением контракта.
     */
    public Order findById(String orderId) {
        String sql = "SELECT id, status, amount_cents FROM orders WHERE id = ?";
        List<Order> results = jdbc.query(sql,
                (rs, rowNum) -> new Order(
                        rs.getString("id"),
                        rs.getString("status").toLowerCase(),
                        rs.getLong("amount_cents")
                ),
                orderId
        );
        return results.isEmpty() ? null : results.get(0);
    }

    /**
     * Сумма заказа в копейках (amount_cents = Σ qty × price_cents).
     * PaymentClient передаёт её в Payment: заглушка "1000" не проходит
     * проверку «списана сумма заказа».
     */
    public long getAmount(String orderId) {
        String sql = "SELECT amount_cents FROM orders WHERE id = ?";
        List<Long> results = jdbc.queryForList(sql, Long.class, orderId);
        if (results.isEmpty()) {
            throw new IllegalStateException("заказ не найден: " + orderId);
        }
        return results.get(0);
    }

    public OrderStatus getStatus(String orderId) {
        String sql = "SELECT status FROM orders WHERE id = ?";
        List<String> results = jdbc.queryForList(sql, String.class, orderId);
        return results.isEmpty() ? null : OrderStatus.valueOf(results.get(0));
    }

    /**
     * PENDING заказы, которые «зависли»: created_at старше olderThan.
     *
     * Возраст нужен, чтобы не хвататься за свежие заказы: Consumer ещё
     * обрабатывает их (событие в Kafka, вызов Payment в полёте) —
     * повторный звонок в Payment для них это лишние запросы и гонка.
     */
    public List<String> findPendingIdsOlderThan(Duration olderThan) {
        String sql = """
            SELECT id FROM orders
            WHERE status = 'PENDING'
              AND created_at < now() - make_interval(secs => ?)
            """;
        return jdbc.queryForList(sql, String.class, olderThan.toMillis() / 1000.0);
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
