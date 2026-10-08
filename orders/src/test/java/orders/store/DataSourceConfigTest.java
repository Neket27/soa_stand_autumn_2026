package orders.store;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class DataSourceConfigTest {

    @Test
    void databaseUrl_withPasswordAndQuery() {
        DataSourceConfig.ParsedUrl parsed = DataSourceConfig.databaseUrl(
                "postgres://orders:orders@postgres:5432/orders?sslmode=disable");

        assertEquals("jdbc:postgresql://postgres:5432/orders?sslmode=disable", parsed.jdbcUrl());
        assertEquals("orders", parsed.user());
        assertEquals("orders", parsed.password());
    }

    @Test
    void databaseUrl_withoutPassword() {
        DataSourceConfig.ParsedUrl parsed = DataSourceConfig.databaseUrl(
                "postgres://user@host:5432/db");

        assertEquals("jdbc:postgresql://host:5432/db", parsed.jdbcUrl());
        assertEquals("user", parsed.user());
        assertEquals("", parsed.password());
    }

    @Test
    void databaseUrl_withoutQuery() {
        DataSourceConfig.ParsedUrl parsed = DataSourceConfig.databaseUrl(
                "postgres://orders:orders@postgres:5432/orders");

        assertEquals("jdbc:postgresql://postgres:5432/orders", parsed.jdbcUrl());
        assertEquals("orders", parsed.user());
        assertEquals("orders", parsed.password());
    }
}
