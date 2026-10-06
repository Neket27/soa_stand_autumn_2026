package orders.flow;

import orders.inventory.InventoryClient;
import orders.payment.PaymentClient;
import orders.store.OrderRepository;

import java.time.Duration;

public class OrderProcessor {

    private final InventoryClient inventoryClient;
    private final PaymentClient paymentClient;
    private final OrderRepository orderRepository;
    private final PaymentCircuitBreaker circuitBreaker;

    public OrderProcessor(InventoryClient inventoryClient,
                          PaymentClient paymentClient,
                          OrderRepository orderRepository,
                          PaymentCircuitBreaker circuitBreaker) {
        this.inventoryClient = inventoryClient;
        this.paymentClient = paymentClient;
        this.orderRepository = orderRepository;
        this.circuitBreaker = circuitBreaker;
    }

    public String processOnce(String orderId, long amountCents, String itemsJson, Duration budget) {
        Duration reserveTimeout = Duration.ofMillis(Math.max(100, budget.toMillis() - 10));
        boolean reserved = inventoryClient.reserve(orderId, itemsJson, reserveTimeout);
        if (!reserved) {
            return "REJECTED";
        }
        return "PENDING";
    }

    public boolean processUntilTerminal(String orderId, String itemsJson, Duration maxDeadline) {
        long deadline = System.currentTimeMillis() + maxDeadline.toMillis();

        while (System.currentTimeMillis() < deadline) {
            OrderRepository.Order order = orderRepository.findById(orderId);
            if (order == null) {
                return false;
            }
            if ("paid".equals(order.status()) || "rejected".equals(order.status())) {
                return true;
            }

            // Gate on the circuit breaker. allow() releases exactly ONE probe
            // when the breaker is HALF_OPEN, so a single worker pokes payment
            // and the rest wait.
            if (!circuitBreaker.allow()) {
                try {
                    Thread.sleep(2000);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
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
            PaymentClient.ChargeResult chargeResult = paymentClient.charge(orderId, order.amountCents(), Duration.ofSeconds(1));

            switch (chargeResult) {
                case CHARGED:
                    circuitBreaker.onSuccess();
                    if (orderRepository.transition(orderId, "paid") > 0) return true;
                    break;
                case DECLINED:
                    release(orderId);
                    if (orderRepository.transition(orderId, "rejected") > 0) return true;
                    break;
                case SERVER_ERROR:
                    PaymentClient.ChargeResult retryResult = paymentClient.charge(orderId, order.amountCents(), Duration.ofSeconds(1));
                    if (retryResult == PaymentClient.ChargeResult.CHARGED) {
                        circuitBreaker.onSuccess();
                        if (orderRepository.transition(orderId, "paid") > 0) return true;
                    } else if (retryResult == PaymentClient.ChargeResult.DECLINED) {
                        release(orderId);
                        if (orderRepository.transition(orderId, "rejected") > 0) return true;
                    }
                    break;
                case NETWORK:
                    // Timeout or lost response. The payment may have executed
                    // (mock records on receipt). Reconcile via hasCharges:
                    // found -> paid without touching the breaker;
                    // not found -> release inventory, reject order, open breaker.
                    boolean found = paymentClient.hasCharges(orderId, Duration.ofSeconds(1));
                    if (found) {
                        circuitBreaker.onSuccess();
                        if (orderRepository.transition(orderId, "paid") > 0) return true;
                        OrderRepository.Order current = orderRepository.findById(orderId);
                        if (current != null && ("paid".equals(current.status()) || "rejected".equals(current.status()))) {
                            return true;
                        }
                    } else {
                        circuitBreaker.onTimeoutOrConnError();
                        release(orderId);
                        if (orderRepository.transition(orderId, "rejected") > 0) return true;
                    }
                    break;
            }
        }
        return false;
    }

    private boolean release(String orderId) {
        return inventoryClient.release(orderId, Duration.ofSeconds(1));
    }
}
