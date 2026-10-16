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
            @Value("${DB_PASSWORD:}") String password,
            @Value("${DB_POOL_SIZE:16}") int poolSize) {

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
        // Размер пула под конфигурацию «12 партиций, concurrency 12»:
        // 12 потоков коньюмера + ~4 (API-запросы, outbox-поллер, Reaper).
        // Соединение держится только на время запроса — HTTP-вызов Payment
        // пул не занимает, поэтому 16 покрывает пик без ожидания.
        //
        // Потолок сверху — max_connections=100 у Postgres (дефолт) на ВСЕ
        // реплики orders: 5 × 16 = 80, запас ~20 на healthcheck'и и раннер.
        // Больше 20 на инстанс при 5 репликах ставить нельзя — упрутся в
        // «too many connections». Меняется через DB_POOL_SIZE без правки кода.
        dataSource.setMaximumPoolSize(poolSize);
        return dataSource;
    }

    @Bean
    public JdbcTemplate jdbcTemplate(DataSource dataSource) {
        return new JdbcTemplate(dataSource);
    }
}
