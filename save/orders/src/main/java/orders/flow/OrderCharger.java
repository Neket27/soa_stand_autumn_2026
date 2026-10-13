package orders.flow;

import orders.inventory.InventoryClient;
import orders.payment.PaymentClient;
import orders.store.OrderRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.time.Duration;

/**
 * Единственная машина состояний списания: доводит pending-заказ до терминального
 * статуса (paid/rejected) через payment-клиент. Единственный класс, который
 * обращается к {@link PaymentClient}.
 */
@Service
public class OrderCharger {

    private static final Logger log = LoggerFactory.getLogger(OrderCharger.class);

    private final InventoryClient inventoryClient;
    private final PaymentClient paymentClient;
    private final OrderRepository orderRepository;
    private final PaymentCircuitBreaker circuitBreaker;
    private final Duration chargeTimeout;
    private final long breakerWaitMs;

    public OrderCharger(InventoryClient inventoryClient,
                          PaymentClient paymentClient,
                          OrderRepository orderRepository,
                          PaymentCircuitBreaker circuitBreaker,
                          @Value("${orders.process.charge-timeout-ms:1000}") long chargeTimeoutMs,
                          @Value("${orders.process.breaker-wait-ms:2000}") long breakerWaitMs) {
        this.inventoryClient = inventoryClient;
        this.paymentClient = paymentClient;
        this.orderRepository = orderRepository;
        this.circuitBreaker = circuitBreaker;
        this.chargeTimeout = Duration.ofMillis(chargeTimeoutMs);
        this.breakerWaitMs = breakerWaitMs;
    }

    /**
     * Доводит машину состояний списания заказа в пределах заданного временного бюджета.
     *
     * @return true, если заказ достиг терминального статуса (paid/rejected),
     *         false, если временной бюджет исчерпан (reaper повторит попытку)
     */
    public boolean charge(String orderId, Duration timeBudget) {
        long deadline = System.currentTimeMillis() + timeBudget.toMillis();

        while (System.currentTimeMillis() < deadline) {
            OrderRepository.StoredOrder order = orderRepository.findById(orderId);
            if (order == null) {
                log.info("order={} not found, stopping", orderId);
                return false;
            }
            OrderStatus status = order.status();
            if (status == OrderStatus.PAID || status == OrderStatus.REJECTED) {
                log.info("order={} already terminal status={}", orderId, order.status());
                return true;
            }

            // Гейт по circuit breaker. allow() выпускает ровно ОДИН проб,
            // когда брейкер в HALF_OPEN: один воркер стучится в payment,
            // остальные ждут.
            if (!circuitBreaker.allow()) {
                try {
                    Thread.sleep(breakerWaitMs);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    log.info("order={} worker interrupted", orderId);
                    return false;
                }
                continue;
            }

            // POST-first: GET до списания нет. Списание, выполненное в предыдущей
            // попытке, обнаруживает сам идемпотентный POST — тот же
            // Idempotency-Key и тело, Payment отвечает 200 на то же
            // списание. Reconciliation-GET выполняется только после неопределённого
            // исхода (NETWORK), где "нет ответа" != "нет списания".
            // Списание с тайм-аутом 1 с. В S3 задержка мока (2 с) больше,
            // поэтому списание тайм-аутится на стороне клиента — но мок
            // фиксирует списание по факту получения, и reconciliation находит его в ветке NETWORK.
            PaymentClient.ChargeResult chargeResult = paymentClient.charge(orderId, order.amountCents(), chargeTimeout);
            log.info("order={} chargeResult={}", orderId, chargeResult);

            switch (chargeResult) {
                case CHARGED:
                    circuitBreaker.onReachable();
                    log.info("order={} transitioning to paid", orderId);
                    if (orderRepository.transition(orderId, OrderStatus.PAID) > 0) return true;
                    break;
                case DECLINED:
                    // 402 — это HTTP-ответ: Payment доступен.
                    circuitBreaker.onReachable();
                    log.info("order={} charge declined, releasing", orderId);
                    if (!rejectWithReleaseDuty(orderId)) break;
                    return true;
                case SERVER_ERROR:
                    log.info("order={} charge server error, retrying", orderId);
                    PaymentClient.ChargeResult retryResult = paymentClient.charge(orderId, order.amountCents(), chargeTimeout);
                    log.info("order={} charge retryResult={}", orderId, retryResult);
                    switch (retryResult) {
                        case CHARGED:
                            circuitBreaker.onReachable();
                            if (transitionToPaid(orderId)) return true;
                            break;
                        case DECLINED:
                            circuitBreaker.onReachable();
                            if (!rejectWithReleaseDuty(orderId)) break;
                            return true;
                        case SERVER_ERROR:
                            // снова 500: ответ — всё ещё "доступность"
                            circuitBreaker.onReachable();
                            break;
                        case NETWORK:
                            if (!decideAfterNetwork(orderId)) break;
                            return true;
                    }
                    break;
                case NETWORK:
                    // Тайм-аут или потерянный ответ. "Нет ответа" != "нет списания":
                    // открываем брейкер в момент тайм-аута (до reconciliation-GET),
                    // затем реконсиляция решает судьбу заказа.
                    if (!decideAfterNetwork(orderId)) break;
                    return true;
            }
        }
        log.warn("order={} deadline expired, current status={}", orderId,
                orderRepository.findById(orderId) != null ? orderRepository.findById(orderId).status() : "deleted");
        return false;
    }

    /**
     * Решает судьбу заказа после неопределённого исхода списания.
     *
     * @return true, если заказ достиг терминального статуса, false, если он
     *         остаётся pending (reaper подхватит его снова, когда брейкер пропустит)
     */
    private boolean decideAfterNetwork(String orderId) {
        circuitBreaker.onUnavailable();
        PaymentClient.ReconciliationResult result = paymentClient.reconcile(orderId, chargeTimeout);
        log.info("order={} reconciliation={}", orderId, result);
        switch (result) {
            case CHARGED:
                circuitBreaker.onReachable();
                return transitionToPaid(orderId);
            case NOT_CHARGED:
                return rejectWithReleaseDuty(orderId);
            case UNKNOWN:
                    // Не удалось верифицировать: деньги МОГЛИ быть списаны, поэтому
                    // заказ нельзя reject (rejected должен быть чистым). Остаёмся pending.
                return false;
        }
        return false;
    }

    private boolean transitionToPaid(String orderId) {
        return orderRepository.transition(orderId, OrderStatus.PAID) > 0;
    }

    /**
     * Отклоняет заказ со снятием резерва. Release duty, записанный вместе с
     * терминальным переходом, отражает результат inline-релиза: успех -> FALSE,
     * неудача -> TRUE (release-sweep в reaper ретраит DELETE до 204).
     *
     * @return true, если этот вызов выполнил терминальный переход, false, если CAS
     *         выиграл другой воркер (вызывающий прекращает обработку этого заказа)
     */
    private boolean rejectWithReleaseDuty(String orderId) {
        boolean released = release(orderId);
        if (!released)
            log.warn("order={} inline release failed, release duty set for the reaper", orderId);

        return orderRepository.transition(orderId, OrderStatus.REJECTED, !released) > 0;
    }

    private boolean release(String orderId) {
        return inventoryClient.release(orderId, Duration.ofSeconds(1));
    }
}
