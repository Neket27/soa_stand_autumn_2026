package orders.flow;

import orders.store.OrderRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import jakarta.annotation.PreDestroy;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.LinkedBlockingQueue;

@Component
public class BackgroundWorker {

    private static final Logger log = LoggerFactory.getLogger(BackgroundWorker.class);

    private final OrderRepository orderRepository;
    private final OrderFulfiller orderFulfiller;
    private final PaymentCircuitBreaker circuitBreaker;
    private final long deadlineMs;
    final BlockingQueue<String> queue = new LinkedBlockingQueue<>();
    final Map<String, Boolean> inFlight = new ConcurrentHashMap<>();
    private final int workerCount;
    private final List<Thread> workers = new CopyOnWriteArrayList<>();

    public BackgroundWorker(OrderRepository orderRepository,
                            OrderFulfiller orderFulfiller,
                            PaymentCircuitBreaker circuitBreaker,
                            @Value("${orders.worker.count:8}") int workerCount,
                            @Value("${orders.worker.deadline-ms:25000}") long deadlineMs) {
        this.orderRepository = orderRepository;
        this.orderFulfiller = orderFulfiller;
        this.circuitBreaker = circuitBreaker;
        this.workerCount = workerCount;
        this.deadlineMs = deadlineMs;
        startWorkers(workerCount);
    }

    private void startWorkers(int numWorkers) {
        for (int i = 0; i < numWorkers; i++) {
            Thread t = new Thread(this::workerLoop, "bg-worker-" + i);
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
            // Single-flight: only the first pickup of a duplicate queue entry
            // processes the order; the second worker drops it.
            if (inFlight.putIfAbsent(orderId, Boolean.TRUE) != null) {
                continue;
            }
            try {
                log.info("BackgroundWorker processing {}", orderId);
                Duration deadline = Duration.ofMillis(deadlineMs);
                boolean done = orderFulfiller.fulfill(orderId, deadline);
                if (!done) {
                    log.warn("BackgroundWorker deadline exceeded for order {}", orderId);
                }
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
        // The in-flight mark is owned by the worker (set in workerLoop via
        // putIfAbsent); the reaper only checks it, so a duplicate enqueued
        // here is dropped by the second pickup instead of double-processed.
        for (String orderId : orderRepository.findPendingIds()) {
            if (!inFlight.containsKey(orderId)) {
                queue.offer(orderId);
            }
        }
    }

    @PreDestroy
    void shutdown() {
        workers.forEach(Thread::interrupt);
        log.info("BackgroundWorker shutdown, interrupted {} workers", workers.size());
    }
}
