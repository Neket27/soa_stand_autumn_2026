package orders.api.dto;

/**
 * Входящее событие delivery.confirmed от шины (contracts/events.yaml).
 *
 * Служба доставки присылает это событие в POST /webhooks/delivery.
 * ВАЖНО: событие не ссылается на заказы — начисление идёт строго по
 * user_id из события, без поиска заказа (требование контракта).
 */
public record DeliveryConfirmed(String id, String type, String userId,
                                long bonusPoints, String occurredAt) {

    public static final String TYPE = "delivery.confirmed";
}
