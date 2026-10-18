package orders.store;

import com.zaxxer.hikari.HikariDataSource;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import javax.sql.DataSource;

/**
 * Диагностика насыщения Hikari-пула (одна строка WARN в секунду, только
 * когда есть очередь).
 *
 * === ЗАЧЕМ ===
 *
 * Время транзакции в CreateOrderService (t1→t2) ВКЛЮЧАЕТ ожидание
 * свободного соединения из пула: если пул исчерпан, «медленный tx» —
 * это не БД, а очередь внутри JVM. Без этого наблюдения такой случай
 * не отличить от медленного Postgres (стресс-тест 5000 заказов/с:
 * tx до 1263 мс — и БД, и пул подозревались).
 *
 * === ЧТО ПИШЕТ ===
 *
 * waiting > 0 (или пул забит под завязку) → WARN с числами. Тишина —
 * пул не очередь, смотреть надо на сторону БД/хоста.
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class PoolWatchdog {

    private final DataSource dataSource;

    @Scheduled(fixedDelay = 1000)
    public void watch() {
        if (!(dataSource instanceof HikariDataSource hikari)) {
            return; // не Hikari (теоретически) — нечего смотреть
        }
        var pool = hikari.getHikariPoolMXBean();
        if (pool == null) {
            return; // пул ещё не стартовал (до первого соединения)
        }
        int active = pool.getActiveConnections();
        int idle = pool.getIdleConnections();
        int waiting = pool.getThreadsAwaitingConnection();
        int max = hikari.getMaximumPoolSize();
        if (waiting > 0 || active >= max) {
            log.warn("Hikari: пул насыщен (пул: active {}/{} idle {} waiting {})",
                    active, max, idle, waiting);
        }
    }
}
