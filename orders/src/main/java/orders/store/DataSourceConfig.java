package orders.store;

import com.zaxxer.hikari.HikariDataSource;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.JdbcTemplate;

import javax.sql.DataSource;

/**
 * Конфигурация DataSource для PostgreSQL.
 *
 * DATABASE_URL в формате: postgres://user:password@host:5432/dbname?sslmode=disable
 * Преобразуем в JDBC-формат: jdbc:postgresql://host:5432/dbname?sslmode=disable
 * Учётные данные передаём отдельно через setUsername/setPassword.
 */
@Configuration
public class DataSourceConfig {

    @Bean
    public DataSource dataSource(
            @Value("${DATABASE_URL}") String databaseUrl,
            @Value("${DB_USER:}") String username,
            @Value("${DB_PASSWORD:}") String password) {

        // Извлекаем учётные данные из URL, если они там есть
        String urlWithoutCredentials = databaseUrl;

        // Формат: postgres://user:password@host:5432/dbname
        if (databaseUrl.contains("@")) {
            String[] parts = databaseUrl.split("@");
            String credentials = parts[0].replace("postgres://", "").replace("postgresql://", "");
            String[] credParts = credentials.split(":", 2);

            if (username.isBlank() && credParts.length > 0) {
                username = credParts[0];
            }
            if (password.isBlank() && credParts.length > 1) {
                password = credParts[1];
            }

            // Берём часть после @
            urlWithoutCredentials = "jdbc:postgresql://" + parts[1];
        } else {
            // Нет учётных данных в URL — просто добавляем префикс
            if (databaseUrl.startsWith("postgres://")) {
                urlWithoutCredentials = "jdbc:postgresql://" + databaseUrl.substring("postgres://".length());
            } else if (databaseUrl.startsWith("postgresql://")) {
                urlWithoutCredentials = "jdbc:" + databaseUrl;
            }
        }

        HikariDataSource dataSource = new HikariDataSource();
        dataSource.setJdbcUrl(urlWithoutCredentials);
        dataSource.setUsername(username);
        dataSource.setPassword(password);
        dataSource.setDriverClassName("org.postgresql.Driver");
        dataSource.setMaximumPoolSize(10);
        return dataSource;
    }

    @Bean
    public JdbcTemplate jdbcTemplate(DataSource dataSource) {
        return new JdbcTemplate(dataSource);
    }
}
