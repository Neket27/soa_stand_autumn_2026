package orders.store;

import com.zaxxer.hikari.HikariDataSource;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import javax.sql.DataSource;

/**
 * Диагностика состояния Hikari-пула: примерно раз в секунду пишет WARN,
 * если на момент проверки есть ожидающие соединения или все соединения заняты.
 *
 * Время транзакции может включать ожидание соединения из пула. Этот лог
 * помогает заметить такое ожидание, но сам по себе не определяет его
 * причину: ею могут быть долгие операции с БД, длительные транзакции или
 * недостаточный размер пула.
 *
 * Если WARN нет, значит, во время проверки очередь не наблюдалась и пул
 * не был полностью занят. Кратковременные пики между проверками могут
 * не попасть в лог.
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
