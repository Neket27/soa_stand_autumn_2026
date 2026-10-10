package orders.store;

import com.zaxxer.hikari.HikariDataSource;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import javax.sql.DataSource;
import java.net.URI;

/**
 * Converts the stand-provided DATABASE_URL (Heroku form
 * postgres://user:pass@host:port/db?query) into a JDBC data source.
 * JdbcTemplate is auto-configured by Spring Boot from this single DataSource.
 */
@Configuration
@ConditionalOnProperty(name = "DATABASE_URL")
public class DataSourceConfig {

    @Bean
    public DataSource dataSource(@Value("${DATABASE_URL}") String databaseUrl) {
        ParsedUrl parsed = parseDatabaseUrl(databaseUrl);

        HikariDataSource dataSource = new HikariDataSource();
        dataSource.setJdbcUrl(parsed.jdbcUrl());
        dataSource.setUsername(parsed.user());
        dataSource.setPassword(parsed.password());
        dataSource.setConnectionTimeout(5000);
        return dataSource;
    }

    /**
     * postgres://user:pass@host:port/db?query ->
     * jdbc:postgresql://host:port/db?query + user + password.
     */
    static ParsedUrl parseDatabaseUrl(String databaseUrl) {
        URI uri = URI.create(databaseUrl);
        String jdbcUrl = String.format("jdbc:postgresql://%s:%d%s",
                uri.getHost(), uri.getPort(), uri.getPath());
        if (uri.getQuery() != null && !uri.getQuery().isEmpty()) {
            jdbcUrl += "?" + uri.getQuery();
        }

        String user = "";
        String password = "";
        String userInfo = uri.getUserInfo();
        if (userInfo != null) {
            int colonIdx = userInfo.indexOf(':');
            if (colonIdx >= 0) {
                user = userInfo.substring(0, colonIdx);
                password = userInfo.substring(colonIdx + 1);
            } else {
                user = userInfo;
            }
        }
        return new ParsedUrl(jdbcUrl, user, password);
    }

    record ParsedUrl(String jdbcUrl, String user, String password) {}
}
