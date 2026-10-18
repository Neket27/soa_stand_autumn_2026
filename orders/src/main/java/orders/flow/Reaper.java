package orders.flow;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import orders.inventory.InventoryClient;
import orders.payment.PaymentClient;
import orders.store.OrderRepository;
import orders.store.ReleaseDutyRepository;

import java.time.Duration;

/**
 * Периодический "сборщик" заказов, которые зависли в PENDING.
 *
 * === ЗАЧЕМ НУЖЕН ===
 *
 * Если Consumer обработал заказ, но Payment вернул 500 (или retry-и
 * коньюмера исчерпаны), заказ остаётся PENDING. Reaper периодически
 * находит такие заказы и повторяет попытку оплаты. Вторая задача —
 * снятие резервов у REJECTED заказов (release_pending = TRUE).
 *
 * === ОТКРЫТЫЙ ВОПРОС (S2) ===
 *
 * Сейчас заказ, по которому исчерпаны retry-и Consumer'а, рано или
 * поздно оплачивает Reaper — он повторяет бесконечно. Требования
 * допускают и другой вариант: после неудачных повторов переводить
 * заказ в REJECTED. Политику нужно зафиксировать явно.
 */

@Slf4j
@Component
@RequiredArgsConstructor
public class Reaper {

    private final OrderRepository orderRepository;
    private final PaymentClient paymentClient;
    private final InventoryClient inventoryClient;
    private final ReleaseDutyRepository releaseDutyRepository;

    /**
     * Порог «зависшего» заказа.
     *
     * Свежий PENDING ещё разбирает Consumer (событие в Kafka, вызов
     * Payment в полёте), поэтому браться за него в первую секунду жизни
     * незачем: это параллельный звонок в Payment по тому же заказу —
     * лишние запросы и гонка. 30 с — с запасом, больше чем нужно
     * асинхронному пути, чтобы либо оплатить заказ, либо показать, что
     * он застрял.
     */
    private static final Duration PENDING_RETRY_AFTER = Duration.ofSeconds(30);

    /**
     * Компенсации из журнала release_duties.
     *
     * Это двойник releaseReservations() для случая, когда заказа НЕТ:
     * резерв в Inventory создали, но транзакция Orders откатилась, и в
     * таблице orders нет ни строки, ни release_pending. Без журнала
     * такой резерв висел бы вечно, и его пришлось бы снимать вручную.
     *
     * DELETE идемпотентен, поэтому повторяем до успеха сколько угодно.
     */
    private void releaseDutyReservations() {
        for (String orderId : releaseDutyRepository.findAll()) {
            try {
                inventoryClient.releaseReservation(orderId);
                releaseDutyRepository.delete(orderId);
                log.info("Reaper: компенсационный резерв снят для {}", orderId);
            } catch (Exception e) {
                releaseDutyRepository.recordFailure(orderId);
                log.warn("Reaper: не удалось снять компенсационный резерв для {}, повтор позже", orderId, e);
            }
        }
    }

    /**
     * Каждые 10 секунд:
     * 1. Ищет REJECTED заказы с release_pending=TRUE → снимает резерв
     * 2. Добивает компенсации из release_duties (заказ не создался,
     *    а резерв в Inventory уже сделан)
     * 3. Ищет PENDING заказы старше PENDING_RETRY_AFTER → повторяет оплату
     */
    @Scheduled(fixedDelay = 10_000)
    public void sweep() {
        releaseReservations();
        releaseDutyReservations();
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
        for (String orderId : orderRepository.findPendingIdsOlderThan(PENDING_RETRY_AFTER)) {
            try {
                PaymentClient.PaymentResult result = paymentClient.charge(orderId);
                switch (result.status()) {
                    case CHARGED -> {
                        // transition может вернуть false: заказ уже оплатил
                        // Consumer, пока мы шли по списку. В лог не пишем
                        // «оплачен» — это было бы неправдой.
                        if (orderRepository.transition(orderId, OrderStatus.PAID)) {
                            log.info("Reaper: заказ {} оплачен", orderId);
                        } else {
                            log.info("Reaper: заказ {} уже переведён Consumer'ом", orderId);
                        }
                    }
                    case DECLINED -> {
                        if (orderRepository.transition(orderId, OrderStatus.REJECTED)) {
                            log.info("Reaper: заказ {} отклонён банком", orderId);
                        }
                        // release_pending ставим всегда: если Consumer уже
                        // снял резерв — флаг просто потратит один лишний
                        // (идемпотентный) DELETE со стороны Reaper.
                        orderRepository.setReleasePending(orderId, true);
                    }
                    case SERVER_ERROR -> log.warn("Reaper: Payment вернул 500 для {}", orderId);
                    case NETWORK -> log.warn("Reaper: Payment недоступен для {}", orderId);
                }
            } catch (Exception e) {
                log.error("Reaper: ошибка обработки {}", orderId, e);
            }
        }
    }
}
