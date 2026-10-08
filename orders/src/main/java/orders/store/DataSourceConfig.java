package orders.store;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.JdbcTemplate;

import javax.sql.DataSource;
import java.net.URI;

@Configuration
@ConditionalOnProperty(name = "DATABASE_URL")
public class DataSourceConfig {

    @Bean
    public DataSource dataSource(@Value("${DATABASE_URL}") String databaseUrl) throws java.net.URISyntaxException {
        // DATABASE_URL format: postgres://user:pass@host:port/db?sslmode=disable
        // Parse it as a URI: postgres://user:pass@host:port/db
        URI uri = new URI(databaseUrl);
        String host = uri.getHost();
        int port = uri.getPort();
        String path = uri.getPath();
        String query = uri.getQuery();

        // Extract user:pass from userInfo
        String userInfo = uri.getUserInfo();
        String user = "";
        String pass = "";
        if (userInfo != null && userInfo.contains(":")) {
            int colonIdx = userInfo.indexOf(':');
            user = userInfo.substring(0, colonIdx);
            pass = userInfo.substring(colonIdx + 1);
        } else if (userInfo != null) {
            user = userInfo;
        }

        // Build jdbc:postgresql://host:port/db
        String jdbcUrl = String.format("jdbc:postgresql://%s:%d%s", host, port, path);
        if (query != null && !query.isEmpty()) {
            jdbcUrl += "?" + query;
        }

        HikariConfig config = new HikariConfig();
        config.setJdbcUrl(jdbcUrl);
        config.setUsername(user);
        config.setPassword(pass);
        config.setMaximumPoolSize(5);
        config.setMinimumIdle(1);
        config.setConnectionTimeout(5000);

        return new HikariDataSource(config);
    }

    @Bean
    public JdbcTemplate jdbcTemplate(@Qualifier("dataSource") DataSource dataSource) {
        return new JdbcTemplate(dataSource);
    }
}
