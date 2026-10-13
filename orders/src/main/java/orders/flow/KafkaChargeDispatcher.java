package orders.flow;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Component;
import orders.inventory.InventoryClient;
import orders.payment.PaymentClient;
import orders.store.OrderRepository;

/**
 * Kafka-диспетчер: читает заказы из топика и вызывает Payment API.
 *
 * === КАК РАБОТАЕТ ===
 * 1. OrderService создаёт заказ в PENDING и отправляет событие в Kafka
 * 2. Этот Consumer читает событие и вызывает PaymentClient.charge()
 * 3. При 201 → заказ переходит в PAID
 * 4. При 402 → заказ переходит в REJECTED, снимаем резерв в Inventory
 * 5. При 500 → NAK, событие вернётся в топик (retry)
 *
 * === ЧТО СДЕЛАТЬ САМОМУ (S1-S2) ===
 *
 * S1 (10% отклонений банка):
 * — Здесь уже всё должно работать: 402 → REJECTED + DELETE /reservations/{id}
 * — Но проверь: если снятие резерва упадёт, заказ останется в REJECTED
 *   без снятого резерва → S1 упадёт
 * — Добавь retry для DELETE /reservations/{id} при ошибке
 *
 * S2 (33% ошибок 500):
 * — Сейчас при 500 мы делаем NAK → событие возвращается в топик
 * — Но NAK без задержки может привести к бесконечному циклу
 * — Нужно добавить backoff (пауза между retry), например 1 сек
 * — Проверь: после успешного retry заказ должен стать PAID
 * — Если retry исчерпан — заказ остаётся PENDING (или REJECTED?)
 */
@Component
public class KafkaChargeDispatcher {

    private static final Logger log = LoggerFactory.getLogger(KafkaChargeDispatcher.class);

    private final PaymentClient paymentClient;
    private final InventoryClient inventoryClient;
    private final OrderRepository orderRepository;
    private final KafkaTemplate<String, String> kafkaTemplate;
    private final ObjectMapper objectMapper;

    @Value("${ORDERS_DISPATCH:memory}")
    private String dispatchMode;

    public KafkaChargeDispatcher(PaymentClient paymentClient,
                                  InventoryClient inventoryClient,
                                  OrderRepository orderRepository,
                                  KafkaTemplate<String, String> kafkaTemplate,
                                  ObjectMapper objectMapper) {
        this.paymentClient = paymentClient;
        this.inventoryClient = inventoryClient;
        this.orderRepository = orderRepository;
        this.kafkaTemplate = kafkaTemplate;
        this.objectMapper = objectMapper;
    }

    /**
     * Публикует событие о заказе в Kafka для асинхронной оплаты.
     * Вызывается из CreateOrderService, когда ORDERS_DISPATCH=kafka.
     */
    public void dispatch(String orderId, long amountCents, String currency) {
        // Отправляем событие в топик orders-charge
        // Ключ — orderId для партиционирования (один заказ всегда в одну партицию)
        kafkaTemplate.send("orders-charge", orderId, orderId)
                .whenComplete((result, ex) -> {
                    if (ex != null) {
                        log.error("Не удалось отправить заказ {} в Kafka", orderId, ex);
                        // TODO: что делать если Kafka недоступен?
                        // Вариант 1: перевести заказ в REJECTED
                        // Вариант 2: retry с backoff
                        // Вариант 3: записать в outbox таблицу в PostgreSQL
                    }
                });
    }

    /**
     * Consumer: читает события из топика orders-charge.
     *
     * === ЧТО СДЕЛАТЬ САМОМУ (S1-S2) ===
     *
     * S1: Проверь, что при 402 вызывается inventoryClient.releaseReservation()
     *     Если нет — добавь вызов и retry при ошибке
     *
     * S2: Добавь backoff между retry при 500:
     *     — Используй @KafkaListener + ContainerCustomizer с FixedBackOff
     *     — Или добавь Thread.sleep() перед NAK (простой вариант)
     *     — Или используй Dead Letter Topic (DLT) для проблемных событий
     */
    @KafkaListener(topics = "orders-charge", groupId = "${KAFKA_GROUP_ID:orders-charge}")
    public void onCharge(ConsumerRecord<String, String> record, Acknowledgment ack) {
        String orderId = record.value();

        try {
            // 1. Проверяем, что заказ ещё PENDING
            // (мог быть уже обработан другим consumer или retry)
            OrderStatus current = orderRepository.getStatus(orderId);
            if (current != OrderStatus.PENDING) {
                ack.acknowledge();
                return;
            }

            // 2. Вызываем Payment API
            PaymentClient.PaymentResult result = paymentClient.charge(orderId);

            // 3. Обрабатываем результат
            switch (result.status()) {
                case CHARGED -> {
                    orderRepository.transition(orderId, OrderStatus.PAID);
                    ack.acknowledge();
                    log.info("Заказ {} оплачен", orderId);
                }
                case DECLINED -> {
                    orderRepository.transition(orderId, OrderStatus.REJECTED);
                    // Снимаем резерв товара
                    inventoryClient.releaseReservation(orderId);
                    ack.acknowledge();
                    log.info("Заказ {} отклонён банком, резерв снят", orderId);
                }
                case SERVER_ERROR -> {
                    // 500 от Payment — нужно повторить попытку
                    // TODO(S2): добавь backoff перед NAK
                    // Например: Thread.sleep(1000) перед ack.nacknowledge()
                    log.warn("Payment вернул 500 для заказа {}, retry", orderId);
                    ack.nacknowledge();
                }
            }
        } catch (Exception e) {
            log.error("Ошибка обработки заказа {}", orderId, e);
            // Сетевая ошибка или другая проблема — NAK для retry
            ack.nacknowledge();
        }
    }
}
