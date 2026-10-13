package orders.flow;

import lombok.extern.slf4j.Slf4j;
import orders.inventory.InventoryClient;
import orders.store.OrderRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import jakarta.annotation.PreDestroy;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.LinkedBlockingQueue;

@Slf4j
@Component
public class InMemoryChargeDispatcher {

    private final OrderRepository orderRepository;
    private final OrderCharger orderCharger;
    private final InventoryClient inventoryClient;
    private final PaymentCircuitBreaker circuitBreaker;
    private final long deadlineMs;
    private final BlockingQueue<String> queue = new LinkedBlockingQueue<>();
    private final Map<String, Boolean> inFlight = new ConcurrentHashMap<>();
    private final List<Thread> workers = new ArrayList<>();

    BlockingQueue<String> getQueue() { return queue; }
    Map<String, Boolean> getInFlight() { return inFlight; }

    public InMemoryChargeDispatcher(OrderRepository orderRepository,
                             OrderCharger orderCharger,
                             InventoryClient inventoryClient,
                             PaymentCircuitBreaker circuitBreaker,
                             @Value("${orders.worker.count:8}") int workerCount,
                             @Value("${orders.worker.deadline-ms:25000}") long deadlineMs) {
        this.orderRepository = orderRepository;
        this.orderCharger = orderCharger;
        this.inventoryClient = inventoryClient;
        this.circuitBreaker = circuitBreaker;
        this.deadlineMs = deadlineMs;
        startWorkers(workerCount);
    }

    private void startWorkers(int numWorkers) {
        for (int i = 0; i < numWorkers; i++) {
            Thread t = new Thread(this::workerLoop, "charge-worker-" + i);
            t.setDaemon(true);
            workers.add(t);
            t.start();
        }
    }

    private void workerLoop() {
        while (!Thread.currentThread().isInterrupted()) {
            String orderId;
            try {
                orderId = queue.take();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            }
            // Single-flight: из дублирующей записи в очереди заказ обрабатывает
            // только первый забравший её воркер; второй воркер её отбрасывает.
            if (inFlight.putIfAbsent(orderId, Boolean.TRUE) != null)
                continue;

            try {
                log.info("InMemoryChargeDispatcher processing {}", orderId);
                Duration timeBudget = Duration.ofMillis(deadlineMs);
                boolean done = orderCharger.charge(orderId, timeBudget);
                if (!done)
                    log.warn("InMemoryChargeDispatcher deadline exceeded for order {}", orderId);

            } catch (Exception e) {
                log.warn("InMemoryChargeDispatcher error on {}", orderId, e);
            } finally {
                inFlight.remove(orderId);
            }
        }
    }

    public boolean enqueue(String orderId) {
        return queue.offer(orderId);
    }

    @Scheduled(fixedDelay = 500)
    public void reaper() {
        // Release-sweep идёт безусловно: брейкер гейтит только трафик PAYMENT,
        // а blackhole в Payment — не повод перестать снимать резервы Inventory
        // (отказ в стиле S4 не должен держать товар rejected-заказов на складе).
        sweepReleases();
        // Charge-sweep пропускаем только если брейкер безусловно OPEN (не просто
        // HALF_OPEN). HALF_OPEN нужен, чтобы воркеры попробовали проб; здесь его
        // нельзя «потратить».
        if (circuitBreaker.getState() == PaymentCircuitBreaker.State.OPEN) {
            return;
        }
        // In-flight-метка принадлежит воркеру (ставится в workerLoop через
        // putIfAbsent); reaper только её проверяет, поэтому дубликат, поставленный
        // в очередь здесь, отбрасывается при втором заборе, а не обрабатывается дважды.
        for (String orderId : orderRepository.findPendingIds()) {
            if (!inFlight.containsKey(orderId)) {
                queue.offer(orderId);
            }
        }
    }

    /**
     * Durable снятие резерва (фикс утечек L1/L2): ретраим идемпотентный
     * DELETE /reservations/{order_id} (204 даже если ничего не было зарезервировано)
     * для каждого rejected-заказа, у которого ещё стоит release duty, пока 204
     * не подтвердит, что резерв снят.
     */
    private void sweepReleases() {
        for (String orderId : orderRepository.findReleasePendingIds()) {
            if (inventoryClient.release(orderId, Duration.ofSeconds(1))) {
                orderRepository.clearReleaseDuty(orderId);
                log.info("order={} release sweep: duty cleared", orderId);
            } else {
                log.warn("order={} release sweep: duty kept, will retry", orderId);
            }
        }
    }

    @PreDestroy
    void shutdown() {
        workers.forEach(Thread::interrupt);
        log.info("InMemoryChargeDispatcher shutdown, interrupted {} workers", workers.size());
    }
}
