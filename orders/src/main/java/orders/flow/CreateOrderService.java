package orders.flow;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import orders.api.dto.Item;
import orders.api.dto.OrderCreated;
import orders.api.dto.OrderRequest;
import orders.inventory.InventoryClient;
import orders.store.OrderRepository;

import java.util.List;
import java.util.UUID;

/**
 * Сервис создания заказа.
 *
 * === СЦЕНАРИЙ РАБОТЫ ===
 * 1. Валидируем запрос (user_id, items)
 * 2. Резервируем товары в Inventory API
 * 3. Создаём заказ в PENDING
 * 4. Отправляем на оплату:
 *    — Если ORDERS_DISPATCH=kafka → через Kafka
 *    — Иначе → синхронный вызов Payment
 *
 * === ЧТО СДЕЛАТЬ САМОМУ (S1-S2) ===
 *
 * S1: Здесь уже всё работает. Проверь:
 *     — Резерв товара создаётся до оплаты
 *     — Если оплачена → заказ PAID
 *     — Если отклонена → заказ REJECTED, резерв снят
 *
 * S2: Проверь, что заказ не теряется при ошибке Kafka.
 *     Если Kafka недоступен — заказ должен остаться PENDING
 *     (а не PAID и не REJECTED)
 */
@Service
public class CreateOrderService {

    private final InventoryClient inventoryClient;
    private final PaymentClient paymentClient;
    private final OrderRepository orderRepository;
    private final KafkaChargeDispatcher kafkaDispatcher;

    @Value("${ORDERS_DISPATCH:memory}")
    private String dispatchMode;

    public CreateOrderService(InventoryClient inventoryClient,
                              PaymentClient paymentClient,
                              OrderRepository orderRepository,
                              KafkaChargeDispatcher kafkaDispatcher) {
        this.inventoryClient = inventoryClient;
        this.paymentClient = paymentClient;
        this.orderRepository = orderRepository;
        this.kafkaDispatcher = kafkaDispatcher;
    }

    @Transactional
    public OrderCreated create(OrderRequest request) {
        // 1. Валидация
        validate(request);

        // 2. Генерируем ID
        String orderId = UUID.randomUUID().toString();

        // 3. Считаем сумму
        long amountCents = request.items().stream()
                .mapToLong(i -> i.qty() * i.priceCents())
                .sum();

        // 4. Резервируем товары
        // Если Inventory вернёт ошибку — заказ не создаётся, всё OK
        inventoryClient.reserve(orderId, request.items());

        // 5. Создаём заказ в PENDING
        orderRepository.insertPending(orderId, request.userId(), amountCents, request.items());

        // 6. Оплата
        if ("kafka".equals(dispatchMode)) {
            // Асинхронная оплата через Kafka
            kafkaDispatcher.dispatch(orderId, amountCents, "RUB");
            // Возвращаем PENDING — статус изменится позже
            return new OrderCreated(orderId, "pending");
        } else {
            // Синхронная оплата (in-memory режим)
            PaymentClient.PaymentResult result = paymentClient.charge(orderId);
            return switch (result.status()) {
                case CHARGED -> {
                    orderRepository.transition(orderId, OrderStatus.PAID);
                    yield new OrderCreated(orderId, "paid");
                }
                case DECLINED -> {
                    orderRepository.transition(orderId, OrderStatus.REJECTED);
                    inventoryClient.releaseReservation(orderId);
                    yield new OrderCreated(orderId, "rejected");
                }
                case SERVER_ERROR -> {
                    // При 500 оставляем PENDING — должен сработать reaper
                    yield new OrderCreated(orderId, "pending");
                }
            };
        }
    }

    private void validate(OrderRequest request) {
        if (request.userId() == null || request.userId().isBlank()) {
            throw new IllegalArgumentException("user_id обязателен");
        }
        if (request.items() == null || request.items().isEmpty()) {
            throw new IllegalArgumentException("items не может быть пустым");
        }
        for (Item item : request.items()) {
            if (item.sku() == null || item.sku().isBlank()) {
                throw new IllegalArgumentException("sku обязателен");
            }
            if (item.qty() < 1) {
                throw new IllegalArgumentException("qty должен быть >= 1");
            }
            if (item.priceCents() < 0) {
                throw new IllegalArgumentException("price_cents не может быть отрицательным");
            }
        }
    }
}
