package orders.store;

import lombok.extern.slf4j.Slf4j;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import orders.api.dto.DeliveryConfirmed;

/**
 * Репозиторий бонусов (задание 2): журнал обработанных событий
 * delivery.confirmed + балансы покупателей.
 *
 * === ГЛАВНАЯ ГАРАНТИЯ: «РОВНО ОДИН РАЗ» ===
 *
 * Шина доставляет как минимум один раз (контракт Events): копии после
 * таймаута, одновременные копии (parallel_rate), повторы после kill -9
 * в S8. Начислить нужно ровно один раз на событие.
 *
 * Механика — одна транзакция из двух команд:
 *
 *   1. INSERT в bonus_events (event_id PRIMARY KEY) ON CONFLICT DO NOTHING.
 *      Если строка вставлена (rows == 1) — это первая доставка, идём в
 *      шаг 2. Если 0 (конфликт) — копия, начисления нет.
 *   2. UPSERT в bonus_balances: points += bonus_points.
 *
 * Почему это работает при любых сценариях:
 *
 *   — Две одновременные копии одного id: обе лезут в INSERT, в Postgres
 *     вторая ждёт коммита первой на уникальном индексе, затем видит
 *     конфликт → rows = 0 → не начисляет. Гонки «обе начислили» нет.
 *   — kill -9 между шагами: транзакция откатывается ЦЕЛИКО — нет ни
 *     журнала, ни баланса. Шина повторит доставку через секунду после
 *     рестарта, начисление случится сейчас.
 *   — kill -9 после коммита, но до ответа 2xx: шина повторит, INSERT
 *     конфликтнет → 2xx без повторного начисления.
 *
 * === ПОЧЕМУ ОТДЕЛЬНАЯ ТАБЛИЦА ДЛЯ БАЛАНСА ===
 *
 * GET /users/{id}/bonus не должен сканировать растущий журнал
 * (SUM по bonus_events) — это горячий чтением путь. Баланс —
 * материализованная агрегированная сумма, обновляется в той же
 * транзакции, что и журнал.
 */
@Slf4j
@Repository
public class BonusRepository {

    private final JdbcTemplate jdbc;

    private final TransactionTemplate transactionTemplate;

    public BonusRepository(JdbcTemplate jdbc, PlatformTransactionManager transactionManager) {
        this.jdbc = jdbc;
        this.transactionTemplate = new TransactionTemplate(transactionManager);
    }

    /**
     * Начислить бонусы по событию ровно один раз.
     *
     * @return true — событие обработано впервые (начислено);
     *         false — это копия уже обработанного события
     */
    public boolean applyDeliveryEventOnce(DeliveryConfirmed event) {
        return Boolean.TRUE.equals(transactionTemplate.execute(status -> {
            int inserted = jdbc.update("""
                    INSERT INTO bonus_events (event_id, user_id, bonus_points)
                    VALUES (?, ?, ?)
                    ON CONFLICT (event_id) DO NOTHING
                    """, event.id(), event.userId(), event.bonusPoints());
            if (inserted == 0) {
                // Копия: журнал уже содержит событие → баланс НЕ трогаем.
                return false;
            }
            jdbc.update("""
                    INSERT INTO bonus_balances (user_id, points)
                    VALUES (?, ?)
                    ON CONFLICT (user_id)
                    DO UPDATE SET points = bonus_balances.points + EXCLUDED.points
                    """, event.userId(), event.bonusPoints());
            log.debug("Бонусы начислены: user={} points={} event={}",
                    event.userId(), event.bonusPoints(), event.id());
            return true;
        }));
    }

    /**
     * Баланс покупателя. Нет строки в bonus_balances → 0
     * (контракт: «у покупателя без начислений — 0»).
     */
    public long points(String userId) {
        Long points = jdbc.query(
                "SELECT points FROM bonus_balances WHERE user_id = ?",
                (rs, rowNum) -> rs.getLong("points"), userId
        ).stream().findFirst().orElse(0L);
        return points;
    }
}
