package orders.store;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import orders.api.dto.Item;
import orders.api.dto.Order;
import orders.api.dto.OrderPaidEvent;
import orders.flow.OrderStatus;

import java.time.Duration;
import java.util.List;

/**
 * Репозиторий для работы с таблицей orders в PostgreSQL.
 *
 * === СХЕМА ТАБЛИЦЫ ===
 * CREATE TABLE orders (
 *     id           TEXT PRIMARY KEY,
 *     user_id      TEXT NOT NULL,
 *     status       TEXT NOT NULL,  -- PENDING, PAID, REJECTED
 *     amount_cents BIGINT NOT NULL,
 *     items        JSONB NOT NULL,
 *     created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
 *     release_pending BOOLEAN DEFAULT FALSE  -- нужно ли снять резерв
 * );
 *
 * === СВЯЗЬ С БИЗНЕС-ПРОЦЕССОМ ===
 *
 * Отклонение банком (402): переход в REJECTED + release_pending = TRUE
 * (KafkaChargeDispatcher или Reaper) → снятие резерва через Inventory,
 * при успехе release_pending = FALSE. Если снятие упало — заказ REJECTED
 * не «зависает» с резервом: снятие повторяет Reaper по release_pending.
 *
 * Ошибка Payment (500/сеть): заказ остаётся PENDING, событие оплаты
 * ретраит Consumer; PENDING старше 30 с подбирает Reaper.
 */
@Repository
public class OrderRepository {

    private final JdbcTemplate jdbc;

    private final OutboxRepository outboxRepository;

    private final TransactionTemplate transactionTemplate;

    public OrderRepository(JdbcTemplate jdbc,
                           OutboxRepository outboxRepository,
                           PlatformTransactionManager transactionManager) {
        this.jdbc = jdbc;
        this.outboxRepository = outboxRepository;
        this.transactionTemplate = new TransactionTemplate(transactionManager);
    }

    public void insertPending(String orderId, String userId, long amountCents, List<Item> items) {
        String sql = """
            INSERT INTO orders (id, user_id, status, amount_cents, items)
            VALUES (?, ?, 'PENDING', ?, ?::jsonb)
            """;
        String itemsJson = toJson(items);
        jdbc.update(sql, orderId, userId, amountCents, itemsJson);
    }

    /**
     * Атомарный переход заказа в новый статус.
     *
     * Условие WHERE status = 'PENDING' гарантирует, что заказ переведёт
     * ровно кто-то один из конкурирующих «платильщиков» (Consumer и Reaper
     * работают параллельно и оба могут звонить в Payment). Возвращает
     * false, если статус уже кто-то сменил, — вызывающий код не должен
     * в этом случае доложить об успехе в лог.
     */
    public boolean transition(String orderId, OrderStatus newStatus) {
        String sql = "UPDATE orders SET status = ? WHERE id = ? AND status = 'PENDING'";
        int updated = jdbc.update(sql, newStatus.name(), orderId);
        return updated > 0;
    }

    /**
     * Атомарно: переход заказа в PAID + запись события order.paid в outbox
     * (задание 2, contracts/events.yaml).
     *
     * === ГЛАВНАЯ ГАРАНТИЯ ===
     *
     * Требования задания: «событие не публикуется для заказа, которого нет
     * или который не оплачен» и «каждый заказ, который стал paid, в итоге
     * публикуется». Обе стороны одного факта — статус PAID и событие в
     * очереди — обязаны появиться ОДНОЙ транзакцией. Иначе:
     *   — статус закоммичен, событие не записано (упал процесс между
     *     UPDATE и INSERT) → paid-заказ навсегда без события, S6/S7 падают;
     *   — событие записано, статус откатился → событие об оплате
     *     неоплаченного заказа, S6/S7 падают на «события только для
     *     оплаченных».
     *
     * === КТО ВЫЗЫВАЕТ ===
     *
     * Обе точки перехода в PAID: KafkaChargeDispatcher (201 от Payment)
     * и Reaper (повтор оплаты зависшего PENDING). Кто из них победил в
     * гонке за WHERE status = 'PENDING' — тот и пишет событие; проигравший
     * получает false и ничего не добавляет (иначе — два события с разными
     * id на один заказ, что S6 считает нарушением «у всех копий один id»).
     *
     * Прогрев-заказы (warmup-user) НЕ исключаются: WarmupRunner шлёт их
     * и при холодном старте, и при каждом рестарте контейнера (kill -9
     * посреди S7/S8), их списания попадают в факты сценария, а проверка
     * «каждый paid заказ есть в шине» требует события для ЛЮБОГО заказа
     * с итогом paid из фактов. warmup-заказ либо paid (событие нужно),
     * либо rejected (события нет — не публикуется вовсе), так что
     * проверка «события только для оплаченных» не задевается. События
     * холодного старта затирает /admin/reset перед сценарием.
     *
     * @return true, если именно этот вызов перевёл заказ в PAID
     */
    public boolean markPaid(String orderId, OrderPaidEvent event) {
        return transactionTemplate.execute(status -> {
            // UPDATE ... RETURNING: одна команда даёт и переход, и
            // успешную проверку «заказ был PENDING» — без второго
            // round-trip и без гонки между ними.
            List<String> users = jdbc.query(
                    "UPDATE orders SET status = 'PAID' "
                            + "WHERE id = ? AND status = 'PENDING' "
                            + "RETURNING user_id",
                    (rs, rowNum) -> rs.getString("user_id"), orderId);
            if (users.isEmpty()) {
                // Заказ уже не PENDING: либо уже оплачен Reaper'ом/Consumer'ом
                // (событие записал тот, кто победил), либо заказа нет вовсе
                // (запись события для несуществующего заказа — нарушение).
                return false;
            }
            outboxRepository.insert(orderId, OrderPaidEvent.TYPE, event.payload());
            return true;
        });
    }

    /**
     * GET /orders/{id} отдаёт статус строго lowercase: "paid", "rejected",
     * "pending" — раннер сравнивает их с этим набором, uppercase "PAID"
     * он считает нарушением контракта.
     */
    public Order findById(String orderId) {
        String sql = "SELECT id, status, amount_cents FROM orders WHERE id = ?";
        List<Order> results = jdbc.query(sql,
                (rs, rowNum) -> new Order(
                        rs.getString("id"),
                        rs.getString("status").toLowerCase(),
                        rs.getLong("amount_cents")
                ),
                orderId
        );
        return results.isEmpty() ? null : results.get(0);
    }

    /**
     * Сумма заказа в копейках (amount_cents = Σ qty × price_cents).
     * PaymentClient передаёт её в Payment: заглушка "1000" не проходит
     * проверку «списана сумма заказа».
     */
    public long getAmount(String orderId) {
        String sql = "SELECT amount_cents FROM orders WHERE id = ?";
        List<Long> results = jdbc.queryForList(sql, Long.class, orderId);
        if (results.isEmpty()) {
            throw new IllegalStateException("заказ не найден: " + orderId);
        }
        return results.get(0);
    }

    public OrderStatus getStatus(String orderId) {
        String sql = "SELECT status FROM orders WHERE id = ?";
        List<String> results = jdbc.queryForList(sql, String.class, orderId);
        return results.isEmpty() ? null : OrderStatus.valueOf(results.get(0));
    }

    /** Статус и сумма заказа одним запросом (см. getStatusAndAmount). */
    public record OrderState(OrderStatus status, long amountCents) {}

    /**
     * Статус + сумма ОДНИМ запросом (null — заказа нет).
     *
     * Оплата в Kafka-пути раньше делала ДВА запроса: getStatus() для
     * проверки идемпотентности и getAmount() внутри charge(). На
     * стресс-тесте 5000 заказов/с это 5000 лишних SELECT/с на Postgres
     * (который и так упирался в CPU). Один запрос вместо двух —
     * каноничная экономия round-trip'ов в горячем пути.
     */
    public OrderState getStatusAndAmount(String orderId) {
        String sql = "SELECT status, amount_cents FROM orders WHERE id = ?";
        List<OrderState> results = jdbc.query(sql, (rs, rowNum) -> new OrderState(
                OrderStatus.valueOf(rs.getString("status")),
                rs.getLong("amount_cents")
        ), orderId);
        return results.isEmpty() ? null : results.get(0);
    }

    /**
     * Есть ли среди id хоть один PENDING (одним EXISTS-запросом).
     * Используется прогрев-дренажем: опрашивать тысячи id отдельными
     * SELECT'ами каждые 200 мс — сама по себе нагрузка на БД.
     */
    public boolean anyPending(List<String> ids) {
        if (ids.isEmpty()) {
            return false;
        }
        String csv = String.join(",", ids);
        String sql = """
            SELECT EXISTS(
                SELECT 1 FROM orders
                WHERE status = 'PENDING'
                  AND id = ANY(string_to_array(?, ',')::text[])
            )
            """;
        Boolean result = jdbc.queryForObject(sql, Boolean.class, csv);
        return Boolean.TRUE.equals(result);
    }

    /**
     * PENDING заказы, которые «зависли»: created_at старше olderThan.
     *
     * Возраст нужен, чтобы не хвататься за свежие заказы: Consumer ещё
     * обрабатывает их (событие в Kafka, вызов Payment в полёте) —
     * повторный звонок в Payment для них это лишние запросы и гонка.
     */
    public List<String> findPendingIdsOlderThan(Duration olderThan) {
        String sql = """
            SELECT id FROM orders
            WHERE status = 'PENDING'
              AND created_at < now() - make_interval(secs => ?)
            """;
        return jdbc.queryForList(sql, String.class, olderThan.toMillis() / 1000.0);
    }

    /**
     * Получить заказы с release_pending = TRUE.
     * Это REJECTED заказы, у которых не удалось снять резерв.
     */
    public List<String> findReleasePendingIds() {
        String sql = "SELECT id FROM orders WHERE release_pending = TRUE";
        return jdbc.queryForList(sql, String.class);
    }

    public void setReleasePending(String orderId, boolean pending) {
        String sql = "UPDATE orders SET release_pending = ? WHERE id = ?";
        jdbc.update(sql, pending, orderId);
    }

    public void isReady() {
        jdbc.queryForObject("SELECT 1", Integer.class);
    }

    private String toJson(List<Item> items) {
        // Простая сериализация JSON вручную
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < items.size(); i++) {
            Item item = items.get(i);
            if (i > 0) sb.append(",");
            sb.append(String.format(
                    "{\"sku\":\"%s\",\"qty\":%d,\"price_cents\":%d}",
                    item.sku(), item.qty(), item.priceCents()
            ));
        }
        sb.append("]");
        return sb.toString();
    }
}
