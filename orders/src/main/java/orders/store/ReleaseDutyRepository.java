package orders.store;

import lombok.RequiredArgsConstructor;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.List;

/**
 * Журнал компенсаций: резерв в Inventory создан, но заказ создать не
 * удалось (транзакция Orders откатилась).
 *
 * === ЗАЧЕМ ОТДЕЛЬНАЯ ТАБЛИЦА ===
 *
 * У такой пары «резерв без заказа» нет хозяина: в таблице orders нет
 * строки, поэтому и release_pending, и Reaper о ней не знают. Журнал
 * даёт резерву «адрес»: CreateOrderService пишет сюда order_id, а Reaper
 * на каждом тике повторяет DELETE /reservations/{order_id} до успеха.
 *
 * === ПОЧЕМУ БЕЗОПАСНО ПОВТОРЯТЬ ===
 *
 * DELETE идемпотентен: повторный вызов и вызов по неизвестному id
 * заглушка отвечает 204. Доставка at-least-once здесь не вредит.
 *
 * === ЖИВОЙ ОСТАТОК ===
 *
 * Если БД лежит (типичная причина отката заказа), и запись в журнал
 * тоже не пройдёт — остаётся только громкий лог и ручное снятие.
 * Полностью этот остаток закрывает TTL на резервах на стороне Inventory.
 */
@Repository
@RequiredArgsConstructor
public class ReleaseDutyRepository {

    private final JdbcTemplate jdbc;

    /**
     * Записать обязанность снять резерв. ON CONFLICT DO NOTHING: повторная
     * запись того же заказа (дубль или рестарт) не должна падать.
     */
    public void insert(String orderId) {
        String sql = "INSERT INTO release_duties (order_id) VALUES (?) ON CONFLICT DO NOTHING";
        jdbc.update(sql, orderId);
    }

    /** Все невыполненные компенсации в порядке появления. */
    public List<String> findAll() {
        String sql = "SELECT order_id FROM release_duties ORDER BY created_at";
        return jdbc.queryForList(sql, String.class);
    }

    /** Учесть неудачную попытку (для наблюдаемости в журнале фактов/логах). */
    public void recordFailure(String orderId) {
        String sql = "UPDATE release_duties SET attempts = attempts + 1 WHERE order_id = ?";
        jdbc.update(sql, orderId);
    }

    /** Компенсация выполнена — запись журнала больше не нужна. */
    public void delete(String orderId) {
        String sql = "DELETE FROM release_duties WHERE order_id = ?";
        jdbc.update(sql, orderId);
    }
}
