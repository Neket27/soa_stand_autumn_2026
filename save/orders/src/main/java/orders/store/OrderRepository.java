package orders.store;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import orders.api.dto.Item;
import orders.flow.OrderStatus;
import org.springframework.dao.EmptyResultDataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import javax.sql.DataSource;
import java.math.BigDecimal;
import java.sql.Connection;
import java.util.List;
import java.util.UUID;

@Repository
public class OrderRepository {

    private final ObjectMapper objectMapper;
    private final JdbcTemplate jdbcTemplate;
    private final boolean usingPostgres;

    public OrderRepository(JdbcTemplate jdbcTemplate, ObjectMapper objectMapper) {
        this.jdbcTemplate = jdbcTemplate;
        this.objectMapper = objectMapper;
        this.usingPostgres = detectPostgres(jdbcTemplate.getDataSource());
    }

    private static boolean detectPostgres(DataSource dataSource) {
        try (Connection c = dataSource.getConnection()) {
            return "PostgreSQL".equalsIgnoreCase(c.getMetaData().getDatabaseProductName());
        } catch (Exception e) {
            return false;
        }
    }

    public String insertPending(String userId, List<Item> items, BigDecimal amountCents) {
        String id = UUID.randomUUID().toString();
        String itemsJson;
        try {
            itemsJson = objectMapper.writeValueAsString(items);
        } catch (JsonProcessingException e) {
            throw new RuntimeException("failed to serialize items", e);
        }

        String sql;
        if (usingPostgres) {
            sql = "INSERT INTO orders (id, user_id, status, amount_cents, items, created_at) VALUES (?, ?, 'PENDING', ?, CAST(? AS JSONB), now())";
        } else {
            sql = "INSERT INTO orders (id, user_id, status, amount_cents, items, created_at) VALUES (?, ?, 'PENDING', ?, ?, now())";
        }

        jdbcTemplate.update(sql, id, userId, amountCents, itemsJson);
        return id;
    }

    public int transition(String id, OrderStatus to) {
        return transition(id, to, false);
    }

    /**
     * Переводит pending-заказ в терминальный статус, атомарно записывая в том же
     * UPDATE durable-обязанность «снять резерв» (окна, где заказ уже rejected,
     * а обязанность ещё не записана, не существует).
     */
    public int transition(String id, OrderStatus to, boolean releasePending) {
        return jdbcTemplate.update(
                "UPDATE orders SET status = ?, release_pending = ? WHERE id = ? AND status = 'PENDING'",
                to.name(), releasePending, id);
    }

    /** Rejected-заказы, у которых ещё стоит release duty (release-sweep в reaper). */
    public List<String> findReleasePendingIds() {
        return jdbcTemplate.queryForList(
                "SELECT id FROM orders WHERE status = 'REJECTED' AND release_pending", String.class);
    }

    /** Снять release duty, когда снятие резерва подтверждено (DELETE 204). */
    public int clearReleaseDuty(String id) {
        return jdbcTemplate.update(
                "UPDATE orders SET release_pending = FALSE WHERE id = ? AND status = 'REJECTED' AND release_pending",
                id);
    }

    public StoredOrder findById(String id) {
        try {
            return jdbcTemplate.queryForObject(
                    "SELECT id, user_id, status, amount_cents, items FROM orders WHERE id = ?",
                    (rs, rowNum) -> new StoredOrder(
                            rs.getString("id"),
                            rs.getString("user_id"),
                            OrderStatus.valueOf(rs.getString("status")),
                            rs.getBigDecimal("amount_cents"),
                            rs.getString("items")
                    ), id);
        } catch (EmptyResultDataAccessException e) {
            return null;
        }
    }

    public List<String> findPendingIds() {
        return jdbcTemplate.queryForList(
                "SELECT id FROM orders WHERE status = 'PENDING' ORDER BY created_at", String.class);
    }

    /** True, когда база доступна — сервис готов принимать заказы. */
    public boolean isReady() {
        try {
            return jdbcTemplate.queryForObject("SELECT 1", Integer.class) != null;
        } catch (Exception e) {
            return false;
        }
    }

    public static record StoredOrder(String id, String userId, OrderStatus status, BigDecimal amountCents, String itemsJson) {}
}
