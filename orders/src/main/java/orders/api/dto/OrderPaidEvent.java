package orders.api.dto;

import java.time.OffsetDateTime;
import java.util.UUID;

/**
 * Событие order.paid для шины событий (задание 2, contracts/events.yaml).
 *
 * === СТАБИЛЬНОСТЬ id — ГЛАВНОЕ СВОЙСТВО ===
 *
 * Требование задания: «одно событие можно опубликовать несколько раз,
 * но у всех копий одинаковый id». Доставка в шину — at-least-once
 * (падение между send и markSent в EventsPublisher даёт повтор), и
 * подписчик отличает копию от нового события именно по id.
 *
 * Поэтому id генерируется РОВНО ОДИН РАЗ — здесь, при создании объекта
 * перед записью в outbox, — и «запекается» в payload. Все повторные
 * отправки шлют один и тот же payload из БД → один и тот же id → шина
 * помечает копию как дубль (busSeen в mocks/bus.go) и не выдаёт её
 * подписчикам вторым разом.
 *
 * Сериализация — Jackson со SNAKE_CASE-стратегией (JacksonConfig):
 * {"id":..., "type":"order.paid","order_id":...,"occurred_at":...}.
 *
 * occurred_at — момент перехода заказа в paid (факт в бизнес-процессе),
 * а не момент отправки: при ретраях после восстановления шины (S6)
 * событие уходит позже, но «когда оплатился заказ» не меняется.
 */
public record OrderPaidEvent(String id, String type, String orderId, OffsetDateTime occurredAt) {

    public static final String TYPE = "order.paid";

    /** Фабрика: id и момент создаются здесь, дальше неизменны. */
    public static OrderPaidEvent forOrder(String orderId) {
        return new OrderPaidEvent(UUID.randomUUID().toString(), TYPE, orderId, OffsetDateTime.now());
    }

    /**
     * Готовое тело POST /events — строка, которую кладём в outbox payload.
     *
     * Строится вручную (как и items в OrderRepository): все поля —
     * UUID, известный enum и RFC3339-таймстамп, экранирование не нужно.
     * Главное — она создаётся РОВНО ОДИН РАЗ при записи в outbox:
     * каждая копия при ретрае уходит с этим же телом и этим же id.
     */
    public String payload() {
        return "{\"id\":\"" + id + "\",\"type\":\"" + type
                + "\",\"order_id\":\"" + orderId
                + "\",\"occurred_at\":\"" + occurredAt + "\"}";
    }
}
