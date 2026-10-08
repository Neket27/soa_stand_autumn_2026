package orders.store;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import orders.api.dto.Item;
import orders.flow.OrderStatus;
import org.springframework.dao.EmptyResultDataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import javax.sql.DataSource;
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

    public String insertPending(String userId, List<Item> items, long amountCents) {
        String id = UUID.randomUUID().toString();
        String itemsJson;
        try {
            itemsJson = objectMapper.writeValueAsString(items);
        } catch (JsonProcessingException e) {
            throw new RuntimeException("failed to serialize items", e);
        }

        String sql;
        if (usingPostgres) {
            sql = "INSERT INTO orders (id, user_id, status, amount_cents, items, created_at) VALUES (?, ?, 'pending', ?, CAST(? AS JSONB), now())";
        } else {
            sql = "INSERT INTO orders (id, user_id, status, amount_cents, items, created_at) VALUES (?, ?, 'pending', ?, ?, now())";
        }

        jdbcTemplate.update(sql, id, userId, amountCents, itemsJson);
        return id;
    }

    public int transition(String id, OrderStatus to) {
        return jdbcTemplate.update(
                "UPDATE orders SET status = ? WHERE id = ? AND status = 'pending'",
                to.value(), id);
    }

    public Order findById(String id) {
        try {
            return jdbcTemplate.queryForObject(
                    "SELECT id, user_id, status, amount_cents, items FROM orders WHERE id = ?",
                    (rs, rowNum) -> new Order(
                            rs.getString("id"),
                            rs.getString("user_id"),
                            rs.getString("status"),
                            rs.getLong("amount_cents"),
                            rs.getString("items")
                    ), id);
        } catch (EmptyResultDataAccessException e) {
            return null;
        }
    }

    public List<String> findPendingIds() {
        return jdbcTemplate.queryForList(
                "SELECT id FROM orders WHERE status = 'pending' ORDER BY created_at", String.class);
    }

    /** True when the database is reachable — the service is ready to accept orders. */
    public boolean isReady() {
        try {
            return jdbcTemplate.queryForObject("SELECT 1", Integer.class) != null;
        } catch (Exception e) {
            return false;
        }
    }

    public static record Order(String id, String userId, String status, long amountCents, String itemsJson) {}
}
