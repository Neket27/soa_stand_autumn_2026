package orders.flow;

import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
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
 * 1. CreateOrderService создаёт заказ в PENDING и отправляет событие в Kafka
 * 2. Этот Consumer читает событие и вызывает PaymentClient.charge()
 * 3. При 201 → заказ переходит в PAID
 * 4. При 402 → заказ переходит в REJECTED, снимаем резерв в Inventory
 * 5. При 500/сети → исключение, DefaultErrorHandler повторяет чтение
 *
 * === ВАЖНЫЕ ЛОВУШКИ (по ним падал S1) ===
 *
 * 1. switch ОБЯЗАН покрывать все статусы PaymentStatus. Если ветки нет —
 *    Java молча выходит из метода: без ack, без смены статуса. Заказ
 *    навсегда остаётся PENDING, а 402 «исчезает». Так и было: ветки
 *    NETWORK не было, 402 из RestTemplate приходил как NETWORK.
 *
 * 2. RestTemplate на 4xx/5xx БРОСКАЕТ исключение — 402 надо ловить
 *    отдельно в PaymentClient (там оно уже превращается в DECLINED).
 *
 * 3. Событие не должно уйти в Kafka до коммита транзакции создания
 *    заказа, иначе Consumer не найдёт заказ в БД. Диспетчер при
 *    status == null делает throw → повтор.
 */
@Component
public class KafkaChargeDispatcher {

    private static final Logger log = LoggerFactory.getLogger(KafkaChargeDispatcher.class);

    private final PaymentClient paymentClient;
    private final InventoryClient inventoryClient;
    private final OrderRepository orderRepository;
    private final KafkaTemplate<String, String> kafkaTemplate;

    public KafkaChargeDispatcher(PaymentClient paymentClient,
                                  InventoryClient inventoryClient,
                                  OrderRepository orderRepository,
                                  KafkaTemplate<String, String> kafkaTemplate) {
        this.paymentClient = paymentClient;
        this.inventoryClient = inventoryClient;
        this.orderRepository = orderRepository;
        this.kafkaTemplate = kafkaTemplate;
    }

    /**
     * Публикует событие о заказе в Kafka для асинхронной оплаты.
     * Вызывается из CreateOrderService ПОСЛЕ коммита транзакции.
     */
    public void dispatch(String orderId) {
        // Ключ — orderId: один заказ всегда попадает в одну партицию
        kafkaTemplate.send("orders-charge", orderId, orderId)
                .whenComplete((result, ex) -> {
                    if (ex != null) {
                        log.error("Не удалось отправить заказ {} в Kafka", orderId, ex);
                        // Заказ остаётся PENDING — его подхватит Reaper
                    }
                });
    }

    /**
     * Consumer: читает события из топика orders-charge.
     *
     * === ЧТО СДЕЛАТЬ САМОМУ (S2) ===
     * Сейчас при 500/сети мы бросаем исключение — DefaultErrorHandler
     * повторяет чтение события. Добавь backoff, если retry окажется
     * слишком «горячим» (FixedBackOff в конфигурации container factory).
     */
    @KafkaListener(topics = "orders-charge", groupId = "${KAFKA_GROUP_ID:orders-charge}")
    public void onCharge(ConsumerRecord<String, String> record, Acknowledgment ack) {
        String orderId = record.value();

        try {
            // 1. Заказ мог ещё не появиться в БД (гонка с коммитом)
            //    или быть уже обработан — в обоих случаях retry/пропуск.
            OrderStatus current = orderRepository.getStatus(orderId);
            if (current == null) {
                throw new IllegalStateException("заказ ещё не виден в БД: " + orderId);
            }
            if (current != OrderStatus.PENDING) {
                // Уже оплачен или отклонён — дубль события, подтверждаем
                ack.acknowledge();
                return;
            }

            // 2. Вызываем Payment API
            PaymentClient.PaymentResult result = paymentClient.charge(orderId);

            // 3. Обрабатываем результат — ВСЕ ветки обязательны!
            switch (result.status()) {
                case CHARGED -> {
                    orderRepository.transition(orderId, OrderStatus.PAID);
                    ack.acknowledge();
                    log.info("Заказ {} оплачен", orderId);
                }
                case DECLINED -> {
                    // Банк отклонил: сначала фиксируем REJECTED и метку
                    // «резерв нужно снять» — если DELETE упадёт, заказ
                    // не зависнет, а дорезервит Reaper по release_pending.
                    orderRepository.transition(orderId, OrderStatus.REJECTED);
                    orderRepository.setReleasePending(orderId, true);
                    try {
                        inventoryClient.releaseReservation(orderId);
                        orderRepository.setReleasePending(orderId, false);
                        log.info("Заказ {} отклонён банком, резерв снят", orderId);
                    } catch (Exception e) {
                        log.warn("Резерв не снят для {}, повторит Reaper", orderId, e);
                    }
                    ack.acknowledge();
                }
                case SERVER_ERROR, NETWORK -> {
                    // 500 от Payment или сетевая ошибка → retry события
                    log.warn("Payment недоступен для заказа {} ({}), retry",
                            orderId, result.status());
                    throw new RuntimeException("retry needed: " + result.status());
                }
            }
        } catch (Exception e) {
            // Без ack: DefaultErrorHandler вернёт событие в топик
            log.error("Ошибка обработки заказа {}", orderId, e);
            throw (e instanceof RuntimeException re) ? re : new RuntimeException(e);
        }
    }
}
