package orders.store;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariDataSource;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;

import javax.sql.DataSource;
import java.sql.SQLException;
import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

class OrderRepositoryTest {

    private JdbcTemplate jdbcTemplate;
    private OrderRepository repository;

    @BeforeEach
    void setUp() throws SQLException {
        HikariDataSource ds = new HikariDataSource();
        ds.setJdbcUrl("jdbc:h2:mem:testdb;DB_CLOSE_DELAY=-1");
        ds.setDriverClassName("org.h2.Driver");

        jdbcTemplate = new JdbcTemplate(ds);

        jdbcTemplate.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id           VARCHAR PRIMARY KEY,
                user_id      VARCHAR NOT NULL,
                status       VARCHAR NOT NULL,
                amount_cents BIGINT NOT NULL,
                items        VARCHAR,
                created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """);

        jdbcTemplate.execute("DELETE FROM orders");

        repository = new OrderRepository(jdbcTemplate, new ObjectMapper());
    }

    @Test
    void insertAndFindById() {
        String id = repository.insertPending("user-1", List.of(
                new orders.api.dto.Item("sku-1", 2, 49900)), 99800);

        assertNotNull(id);
        OrderRepository.Order order = repository.findById(id);
        assertNotNull(order);
        assertEquals("user-1", order.userId());
        assertEquals(99800, order.amountCents());
        assertEquals("pending", order.status());
    }

    @Test
    void transition_pendingToPaid() {
        String id = repository.insertPending("user-1", List.of(
                new orders.api.dto.Item("sku-1", 1, 1000)), 1000);

        int updated = repository.transition(id, "paid");
        assertEquals(1, updated);

        OrderRepository.Order order = repository.findById(id);
        assertEquals("paid", order.status());
    }

    @Test
    void transition_onlyOnce() {
        String id = repository.insertPending("user-1", List.of(
                new orders.api.dto.Item("sku-1", 1, 1000)), 1000);

        int first = repository.transition(id, "paid");
        assertEquals(1, first);

        int second = repository.transition(id, "rejected");
        assertEquals(0, second);
    }

    @Test
    void findPendingIds() {
        String id1 = repository.insertPending("user-1", List.of(
                new orders.api.dto.Item("sku-1", 1, 1000)), 1000);
        String id2 = repository.insertPending("user-2", List.of(
                new orders.api.dto.Item("sku-2", 2, 500)), 1000);

        List<String> pending = repository.findPendingIds();
        assertEquals(2, pending.size());
        assertTrue(pending.contains(id1));
        assertTrue(pending.contains(id2));
    }

    @Test
    void findPendingIds_excludesNonPending() {
        String id = repository.insertPending("user-1", List.of(
                new orders.api.dto.Item("sku-1", 1, 1000)), 1000);

        repository.transition(id, "paid");

        List<String> pending = repository.findPendingIds();
        assertTrue(pending.isEmpty());
    }

    @Test
    void findByIdNotFound() {
        OrderRepository.Order order = repository.findById("nonexistent-id");
        assertNull(order);
    }
}
