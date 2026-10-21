package orders.flow;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.kafka.listener.DefaultErrorHandler;
import org.springframework.kafka.support.Acknowledgment;
import org.springframework.stereotype.Component;
import orders.inventory.InventoryClient;
import orders.api.dto.OrderPaidEvent;
import orders.payment.PaymentClient;
import orders.store.OrderRepository;

/**
 * Kafka-Consumer заказов: читает события из топика orders-charge
 * и вызывает Payment API.
 *
 * События в топик кладёт OutboxDispatcher (Transactional Outbox),
 * а не HTTP-поток — см. CreateOrderService.
 *
 * === КАК РАБОТАЕТ ===
 * 1. OutboxDispatcher отправил событие "заказ X нужно оплатить"
 * 2. Этот Consumer читает событие и вызывает PaymentClient.charge()
 * 3. При 201 → заказ переходит в PAID
 * 4. При 402 → заказ переходит в REJECTED, снимаем резерв в Inventory
 * 5. При 500/сети → исключение, DefaultErrorHandler повторно обрабатывает
 *    ту же запись Kafka
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
 * 3. Проверка статуса заказа перед оплатой делает Consumer
 *    идемпотентным: дубли события (outbox at-least-once, повторные
 *    чтения) не приводят к повторному списанию.
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class KafkaChargeDispatcher {

    private final PaymentClient paymentClient;
    private final InventoryClient inventoryClient;
    private final OrderRepository orderRepository;

    /**
     * Consumer: читает события из топика orders-charge.
     *
     * Ретраи настроены в KafkaConfig.kafkaListenerContainerFactory:
     * FixedBackOff(1 с, 35 повторов) — при 500/сети исключение из веток
     * SERVER_ERROR/NETWORK приводит к повторной обработке той же записи,
     * а не к её публикации обратно в топик. 1 с — пауза между повторами;
     * полное время зависит также от длительности вызовов Payment и состояния
     * Circuit Breaker. После исчерпания повторов запись передаётся recovery
     * handler; заказ может остаться PENDING, и его подберёт Reaper.
     */
    // Партиций столько же, сколько consumer-потоков (KafkaConfig.NewTopic):
    // оплата с высокой задержкой Payment (S3: 2000 мс) должна идти
    // параллельно, иначе очередь оплат не укладывается в wait_s раннера.
    @KafkaListener(topics = "orders-charge", groupId = "${KAFKA_GROUP_ID:orders-charge}", concurrency = "${KAFKA_CONCURRENCY:12}")
    public void onCharge(ConsumerRecord<String, String> record, Acknowledgment ack) {
        String orderId = record.value();

        try {
            // 1. С transactional outbox гонки «событие раньше коммита
            //    заказа» быть не может: событие лежит в той же транзакции,
            //    что и заказ, и публикуется только после коммита. Поэтому
            //    null означает чужое/устаревшее событие (например, из
            //    прошлого запуска стенда: Kafka-топик живёт дольше БД).
            //    Ретрай бессмыслен — заказ никогда не появится. Подтверждаем
            //    запись без повторной обработки и идём дальше.
            //
            // Статус и сумма берутся ОДНИМ запросом: раньше это были
            // getStatus() здесь + getAmount() внутри charge() — два
            // SELECT на событие, 5000 лишних запросов/с на стресс-тесте
            // 5000 заказов/с (Postgres упирался в CPU).
            OrderRepository.OrderState state = orderRepository.getStatusAndAmount(orderId);
            if (state == null) {
                log.warn("Событие для неизвестного заказа {} (offset {}), пропускаю",
                        orderId, record.offset());
                ack.acknowledge();
                return;
            }

            if (state.status() != OrderStatus.PENDING) {
                // Уже оплачен или отклонён — дубль события, подтверждаем
                ack.acknowledge();
                return;
            }

            // 2. Вызываем Payment API
            PaymentClient.PaymentResult result = paymentClient.charge(orderId, state.amountCents());

            // 3. Обрабатываем результат — ВСЕ ветки обязательны!
            switch (result.status()) {
                case CHARGED -> {
                    // false → Reaper успел перевести заказ первым; в лог
                    // пишем факт, а не «оплачен». Событие order.paid пишет
                    // только победитель перехода — см. markPaid.
                    if (orderRepository.markPaid(orderId, OrderPaidEvent.forOrder(orderId))) {
                        log.info("Заказ {} оплачен", orderId);
                    } else {
                        log.info("Заказ {} уже переведён Reaper'ом", orderId);
                    }
                    ack.acknowledge();
                }
                case DECLINED -> {
                    // Банк отклонил: сначала фиксируем REJECTED и метку
                    // «резерв нужно снять» — если DELETE упадёт, заказ
                    // не зависнет, а дорезервит Reaper по release_pending.
                    boolean first = orderRepository.transition(orderId, OrderStatus.REJECTED);
                    orderRepository.setReleasePending(orderId, true);
                    try {
                        inventoryClient.releaseReservation(orderId);
                        orderRepository.setReleasePending(orderId, false);
                        log.info(first
                                ? "Заказ {} отклонён банком, резерв снят"
                                : "Заказ {} отклонён Reaper'ом ранее, резерв снят",
                                orderId);
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
            // Без ack: DefaultErrorHandler повторно обработает эту запись,
            // а после исчерпания повторов передаст её recovery handler.
            log.error("Ошибка обработки заказа {}", orderId, e);
            throw (e instanceof RuntimeException re) ? re : new RuntimeException(e);
        }
    }
}
