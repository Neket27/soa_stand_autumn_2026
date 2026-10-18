package orders.store;

import lombok.RequiredArgsConstructor;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

/**
 * Распределённый барьер готовности реплик (см. warmup_barrier в schema.sql).
 *
 * === ЗАЧЕМ ===
 *
 * Раннер стартует сценарий от /healthz=200 ЛЮБОЙ реплики, а прогрев
 * (WarmupRunner) идёт на каждой реплике независимо и занимает секунды.
 * Без барьера первая «готовая» реплика запускала сценарий, пока остальные
 * ещё дренировали прогрев-заказы — их оплаты и резервы попадали в факты
 * сценария (регрессия TASK=1: 4624 чужих списания в S1).
 *
 * Классический барьер на общей памяти: реплика помечает себя готовой
 * ПОСЛЕ прогрева и дренажа, а200 отдаёт только когда пометились все.
 */
@Repository
@RequiredArgsConstructor
public class WarmupBarrierRepository {

    private final JdbcTemplate jdbc;

    /**
     * Пометить реплику готовой. upsert: повторный рестарт той же реплики
     * (например, после S6/S7) обновляет отметку, а не плодит строки.
     */
    public void markReady(String replicaId) {
        jdbc.update("""
                INSERT INTO warmup_barrier (replica_id) VALUES (?)
                ON CONFLICT (replica_id) DO UPDATE SET done_at = now()
                """, replicaId);
    }

    /** Сколько реплик уже пометились (в текущей БД). */
    public int readyCount() {
        Integer count = jdbc.queryForObject(
                "SELECT count(*) FROM warmup_barrier", Integer.class);
        return count == null ? 0 : count;
    }
}
