package orders.flow;

import orders.store.OrderRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.util.Map;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.LinkedBlockingQueue;

@Component
public class BackgroundWorker {

    private static final Logger log = LoggerFactory.getLogger(BackgroundWorker.class);

    private final OrderRepository orderRepository;
    private final OrderProcessor orderProcessor;
    private final PaymentCircuitBreaker circuitBreaker;
    private final BlockingQueue<String> queue = new LinkedBlockingQueue<>();
    private final Map<String, Boolean> inFlight = new ConcurrentHashMap<>();
    private static final int NUM_WORKERS = 8;
    private static final Duration MAX_DEADLINE = Duration.ofSeconds(25);

    public BackgroundWorker(OrderRepository orderRepository,
                            OrderProcessor orderProcessor,
                            PaymentCircuitBreaker circuitBreaker) {
        this.orderRepository = orderRepository;
        this.orderProcessor = orderProcessor;
        this.circuitBreaker = circuitBreaker;
        startWorkers();
    }

    private void startWorkers() {
        for (int i = 0; i < NUM_WORKERS; i++) {
            Thread t = new Thread(this::workerLoop, "bg-worker-" + i);
            t.setDaemon(true);
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
            inFlight.put(orderId, Boolean.TRUE);
            try {
                log.info("BackgroundWorker processing {}", orderId);
                OrderRepository.Order order = orderRepository.findById(orderId);
                String itemsJson = order != null ? order.itemsJson() : "[]";
                orderProcessor.processUntilTerminal(orderId, itemsJson, MAX_DEADLINE);
            } catch (Exception e) {
                log.warn("BackgroundWorker error on {}", orderId, e);
            } finally {
                inFlight.remove(orderId);
            }
        }
    }

    public void enqueue(String orderId) {
        queue.offer(orderId);
    }

    @Scheduled(fixedDelay = 500)
    public void reaper() {
        // Only skip if breaker is definitively OPEN (not just HALF_OPEN).
        // HALF_OPEN is needed to let workers try a probe; don't consume it here.
        if (circuitBreaker.getState() == PaymentCircuitBreaker.State.OPEN) {
            return;
        }
        for (String orderId : orderRepository.findPendingIds()) {
            if (inFlight.putIfAbsent(orderId, Boolean.TRUE) == null) {
                queue.offer(orderId);
            }
        }
    }
}
