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
 * The single charge state machine: drives a pending order to a terminal
 * status (paid/rejected) using the payment client. The only class that talks
 * to {@link PaymentClient}.
 */
@Service
public class OrderFulfiller {

    private static final Logger log = LoggerFactory.getLogger(OrderFulfiller.class);

    private final InventoryClient inventoryClient;
    private final PaymentClient paymentClient;
    private final OrderRepository orderRepository;
    private final PaymentCircuitBreaker circuitBreaker;
    private final Duration chargeTimeout;
    private final long breakerWaitMs;

    public OrderFulfiller(InventoryClient inventoryClient,
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

    public boolean fulfill(String orderId, Duration maxDeadline) {
        long deadline = System.currentTimeMillis() + maxDeadline.toMillis();

        while (System.currentTimeMillis() < deadline) {
            OrderRepository.Order order = orderRepository.findById(orderId);
            if (order == null) {
                log.info("order={} not found, stopping", orderId);
                return false;
            }
            OrderStatus status = OrderStatus.fromValue(order.status());
            if (status == OrderStatus.PAID || status == OrderStatus.REJECTED) {
                log.info("order={} already terminal status={}", orderId, order.status());
                return true;
            }

            // Gate on the circuit breaker. allow() releases exactly ONE probe
            // when the breaker is HALF_OPEN, so a single worker pokes payment
            // and the rest wait.
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

            // POST-first: no pre-charge GET. A charge executed in a previous
            // attempt is discovered by the idempotent POST itself — same
            // Idempotency-Key and body, Payment answers 200 for the same
            // charge. The reconciliation GET runs only after an indeterminate
            // outcome (NETWORK), where "no response" != "no charge".
            // Charge with 1s timeout. In S3 the mock latency (2s) is longer,
            // so the charge times out on the client side — but the mock records
            // the charge on receipt, and reconciliation finds it in the NETWORK branch.
            PaymentClient.ChargeResult chargeResult = paymentClient.charge(orderId, order.amountCents(), chargeTimeout);
            log.info("order={} chargeResult={}", orderId, chargeResult);

            switch (chargeResult) {
                case CHARGED:
                    circuitBreaker.onReachable();
                    log.info("order={} transitioning to paid", orderId);
                    if (orderRepository.transition(orderId, OrderStatus.PAID) > 0) return true;
                    break;
                case DECLINED:
                    // 402 is an HTTP answer: Payment is reachable.
                    circuitBreaker.onReachable();
                    log.info("order={} charge declined, releasing", orderId);
                    if (!release(orderId)) {
                        log.warn("order={} release failed after decline", orderId);
                    }
                    if (transitionToRejected(orderId)) return true;
                    break;
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
                            if (!release(orderId)) {
                                log.warn("order={} release failed after decline (retry)", orderId);
                            }
                            if (transitionToRejected(orderId)) return true;
                            break;
                        case SERVER_ERROR:
                            // still 500: an answer is still "reachable"
                            circuitBreaker.onReachable();
                            break;
                        case NETWORK:
                            if (!decideAfterNetwork(orderId)) break;
                            return true;
                    }
                    break;
                case NETWORK:
                    // Timeout or lost response. "No response" != "no charge":
                    // open the breaker at the moment of the timeout (before the
                    // reconciliation GET), then reconcile to decide the fate.
                    if (!decideAfterNetwork(orderId)) break;
                    return true;
            }
        }
        log.warn("order={} deadline expired, current status={}", orderId,
                orderRepository.findById(orderId) != null ? orderRepository.findById(orderId).status() : "deleted");
        return false;
    }

    /**
     * Decides the fate of an order after an indeterminate charge outcome.
     *
     * @return true if the order reached a terminal status, false if it stays
     *         pending (the reaper will pick it up again once the breaker allows)
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
                if (!release(orderId)) {
                    log.warn("order={} release failed after network timeout", orderId);
                }
                return transitionToRejected(orderId);
            case UNKNOWN:
                // Unable to verify: the money MAY have been charged, so the order
                // must not be rejected (rejected has to be clean). Stay pending.
                return false;
        }
        return false;
    }

    private boolean transitionToPaid(String orderId) {
        return orderRepository.transition(orderId, OrderStatus.PAID) > 0;
    }

    private boolean transitionToRejected(String orderId) {
        return orderRepository.transition(orderId, OrderStatus.REJECTED) > 0;
    }

    private boolean release(String orderId) {
        return inventoryClient.release(orderId, Duration.ofSeconds(1));
    }
}
