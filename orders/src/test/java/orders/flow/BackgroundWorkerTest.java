package orders.flow;

import orders.store.OrderRepository;
import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
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
        OrderCharger charger = mock(OrderCharger.class);
        doAnswer(inv -> {
            calls.incrementAndGet();
            holdOrder.await(5, TimeUnit.SECONDS);
            return true;
        }).when(charger).charge(eq("order-1"), any(Duration.class));

        BackgroundWorker worker = new BackgroundWorker(orderRepository, charger, circuitBreaker, 2, 5000);
        worker.enqueue("order-1");
        worker.enqueue("order-1");

        // One worker is blocked in charge, the other must drop
        // the duplicate entry instead of processing the order a second time.
        Thread.sleep(300);
        assertEquals(1, calls.get());

        holdOrder.countDown();
        awaitTrue(() -> worker.inFlight.isEmpty(), 5000);
        assertEquals(1, calls.get());
    }

    @Test
    void reaper_skipsInFlightOrder() {
        OrderCharger charger = mock(OrderCharger.class);
        BackgroundWorker worker = new BackgroundWorker(orderRepository, charger, circuitBreaker, 0, 5000);
        worker.inFlight.put("order-2", Boolean.TRUE);
        when(orderRepository.findPendingIds()).thenReturn(List.of("order-2"));

        worker.reaper();

        assertTrue(worker.queue.isEmpty());
    }

    @Test
    void reaper_enqueuesPendingOrder_withoutMarkingInFlight() {
        OrderCharger charger = mock(OrderCharger.class);
        BackgroundWorker worker = new BackgroundWorker(orderRepository, charger, circuitBreaker, 0, 5000);
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
