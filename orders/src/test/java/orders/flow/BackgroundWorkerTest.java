package orders.flow;

import orders.store.OrderRepository;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.BooleanSupplier;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class BackgroundWorkerTest {

    private final OrderRepository orderRepository = mock(OrderRepository.class);
    private final PaymentCircuitBreaker circuitBreaker = new PaymentCircuitBreaker(5000, 10000);

    @Test
    void duplicateQueueEntry_processedOnce() throws Exception {
        AtomicInteger calls = new AtomicInteger();
        CountDownLatch holdOrder = new CountDownLatch(1);
        OrderProcessor processor = mock(OrderProcessor.class);
        when(orderRepository.findById("order-1"))
                .thenReturn(new OrderRepository.Order("order-1", "user", "pending", 1000, "[]"));
        doAnswer(inv -> {
            calls.incrementAndGet();
            holdOrder.await(5, TimeUnit.SECONDS);
            return true;
        }).when(processor).processUntilTerminal(eq("order-1"), anyString(), any(Duration.class));

        BackgroundWorker worker = new BackgroundWorker(orderRepository, processor, circuitBreaker, 2, 5000);
        worker.enqueue("order-1");
        worker.enqueue("order-1");

        // One worker is blocked in processUntilTerminal, the other must drop
        // the duplicate entry instead of processing the order a second time.
        Thread.sleep(300);
        assertEquals(1, calls.get());

        holdOrder.countDown();
        awaitTrue(() -> worker.inFlight.isEmpty(), 5000);
        assertEquals(1, calls.get());
    }

    @Test
    void reaper_skipsInFlightOrder() {
        OrderProcessor processor = mock(OrderProcessor.class);
        BackgroundWorker worker = new BackgroundWorker(orderRepository, processor, circuitBreaker, 0, 5000);
        worker.inFlight.put("order-2", Boolean.TRUE);
        when(orderRepository.findPendingIds()).thenReturn(List.of("order-2"));

        worker.reaper();

        assertTrue(worker.queue.isEmpty());
    }

    @Test
    void reaper_enqueuesPendingOrder_withoutMarkingInFlight() {
        OrderProcessor processor = mock(OrderProcessor.class);
        BackgroundWorker worker = new BackgroundWorker(orderRepository, processor, circuitBreaker, 0, 5000);
        when(orderRepository.findPendingIds()).thenReturn(List.of("order-3"));

        worker.reaper();

        assertEquals(List.of("order-3"), List.of(worker.queue.toArray()));
        assertFalse(worker.inFlight.containsKey("order-3"));
    }

    private static void awaitTrue(BooleanSupplier condition, long timeoutMs) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            if (condition.getAsBoolean()) {
                return;
            }
            Thread.sleep(20);
        }
        fail("condition not met within " + timeoutMs + " ms");
    }
}
