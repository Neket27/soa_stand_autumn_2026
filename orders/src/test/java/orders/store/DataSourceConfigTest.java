package orders.store;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class DataSourceConfigTest {

    @Test
    void parseDatabaseUrl_withPasswordAndQuery() {
        DataSourceConfig.ParsedUrl parsed = DataSourceConfig.parseDatabaseUrl(
                "postgres://orders:orders@postgres:5432/orders?sslmode=disable");

        assertEquals("jdbc:postgresql://postgres:5432/orders?sslmode=disable", parsed.jdbcUrl());
        assertEquals("orders", parsed.user());
        assertEquals("orders", parsed.password());
    }

    @Test
    void parseDatabaseUrl_withoutPassword() {
        DataSourceConfig.ParsedUrl parsed = DataSourceConfig.parseDatabaseUrl(
                "postgres://user@host:5432/db");

        assertEquals("jdbc:postgresql://host:5432/db", parsed.jdbcUrl());
        assertEquals("user", parsed.user());
        assertEquals("", parsed.password());
    }

    @Test
    void parseDatabaseUrl_withoutQuery() {
        DataSourceConfig.ParsedUrl parsed = DataSourceConfig.parseDatabaseUrl(
                "postgres://orders:orders@postgres:5432/orders");

        assertEquals("jdbc:postgresql://postgres:5432/orders", parsed.jdbcUrl());
        assertEquals("orders", parsed.user());
        assertEquals("orders", parsed.password());
    }
}
