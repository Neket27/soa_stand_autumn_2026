package orders.flow;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import orders.inventory.InventoryClient;
import orders.payment.PaymentClient;
import orders.store.OrderRepository;

/**
 * Периодический "сборщик" заказов, которые зависли в PENDING.
 *
 * === ЗАЧЕМ НУЖЕН ===
 *
 * Если Consumer обработал заказ,но Payment вернул 500, заказ остаётся PENDING.
 * Reaper находит такие заказы и повторяет попытку оплаты.
 *
 * === ЧТО СДЕЛАТЬ САМОМУ (S1-S2) ===
 *
 * S1: Если заказ REJECTED, но резерв не снят (release_pending=TRUE):
 *     — Reaper должен повторить DELETE /reservations/{id}
 *     — После успеха установить release_pending = FALSE
 *
 * S2: Если заказ PENDING слишком долго (например, > 30 сек):
 *     — Reaper должен повторить вызов Payment API
 *     — Или перевести заказ в REJECTED (по требованиям)
 */
@Component
public class Reaper {

    private static final Logger log = LoggerFactory.getLogger(Reaper.class);

    private final OrderRepository orderRepository;
    private final PaymentClient paymentClient;
    private final InventoryClient inventoryClient;

    public Reaper(OrderRepository orderRepository,
                  PaymentClient paymentClient,
                  InventoryClient inventoryClient) {
        this.orderRepository = orderRepository;
        this.paymentClient = paymentClient;
        this.inventoryClient = inventoryClient;
    }

    /**
     * Каждые 10 секунд:
     * 1. Ищет REJECTED заказы с release_pending=TRUE → снимает резерв
     * 2. Ищет PENDING заказы старше N минут → повторяет оплату
     */
    @Scheduled(fixedDelay = 10_000)
    public void sweep() {
        releaseReservations();
        retryPendingOrders();
    }

    private void releaseReservations() {
        for (String orderId : orderRepository.findReleasePendingIds()) {
            try {
                inventoryClient.releaseReservation(orderId);
                orderRepository.setReleasePending(orderId, false);
                log.info("Reaper: резерв снят для заказа {}", orderId);
            } catch (Exception e) {
                log.warn("Reaper: не удалось снять резерв для {}", orderId, e);
            }
        }
    }

    private void retryPendingOrders() {
        // TODO(S2): добавить проверку времени создания заказа
        // Если заказ PENDING дольше 30 секунд — повторить оплату
        for (String orderId : orderRepository.findPendingIds()) {
            try {
                PaymentClient.PaymentResult result = paymentClient.charge(orderId);
                switch (result.status()) {
                    case CHARGED -> {
                        orderRepository.transition(orderId, OrderStatus.PAID);
                        log.info("Reaper: заказ {} оплачен", orderId);
                    }
                    case DECLINED -> {
                        orderRepository.transition(orderId, OrderStatus.REJECTED);
                        orderRepository.setReleasePending(orderId, true);
                        log.info("Reaper: заказ {} отклонён банком", orderId);
                    }
                    case SERVER_ERROR -> log.warn("Reaper: Payment вернул 500 для {}", orderId);
                }
            } catch (Exception e) {
                log.error("Reaper: ошибка обработки {}", orderId, e);
            }
        }
    }
}
