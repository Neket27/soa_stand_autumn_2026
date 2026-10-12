package orders.flow;

import orders.inventory.InventoryClient;
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

class InMemoryChargeDispatcherTest {

    private final OrderRepository orderRepository = mock(OrderRepository.class);
    private final InventoryClient inventoryClient = mock(InventoryClient.class);
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

        InMemoryChargeDispatcher worker = new InMemoryChargeDispatcher(orderRepository, charger, inventoryClient, circuitBreaker, 2, 5000);
        worker.enqueue("order-1");
        worker.enqueue("order-1");

        // One worker is blocked in charge, the other must drop
        // the duplicate entry instead of processing the order a second time.
        Thread.sleep(300);
        assertEquals(1, calls.get());

        holdOrder.countDown();
        awaitTrue(() -> worker.getInFlight().isEmpty(), 5000);
        assertEquals(1, calls.get());
    }

    @Test
    void reaper_skipsInFlightOrder() {
        OrderCharger charger = mock(OrderCharger.class);
        InMemoryChargeDispatcher worker = new InMemoryChargeDispatcher(orderRepository, charger, inventoryClient, circuitBreaker, 0, 5000);
        worker.getInFlight().put("order-2", Boolean.TRUE);
        when(orderRepository.findPendingIds()).thenReturn(List.of("order-2"));

        worker.reaper();

        assertTrue(worker.getQueue().isEmpty());
    }

    @Test
    void reaper_enqueuesPendingOrder_withoutMarkingInFlight() {
        OrderCharger charger = mock(OrderCharger.class);
        InMemoryChargeDispatcher worker = new InMemoryChargeDispatcher(orderRepository, charger, inventoryClient, circuitBreaker, 0, 5000);
        when(orderRepository.findPendingIds()).thenReturn(List.of("order-3"));

        worker.reaper();

        assertEquals(List.of("order-3"), List.of(worker.getQueue().toArray()));
        assertFalse(worker.getInFlight().containsKey("order-3"));
    }

    @Test
    void reaper_releaseSweep_releasesAndClearsDuty() {
        InMemoryChargeDispatcher worker = new InMemoryChargeDispatcher(orderRepository, mock(OrderCharger.class), inventoryClient, circuitBreaker, 0, 5000);
        when(orderRepository.findReleasePendingIds()).thenReturn(List.of("order-r1"));
        when(inventoryClient.release(eq("order-r1"), any(Duration.class))).thenReturn(true);

        worker.reaper();

        verify(inventoryClient).release(eq("order-r1"), any(Duration.class));
        verify(orderRepository).clearReleaseDuty("order-r1");
    }

    @Test
    void reaper_releaseSweep_failedRelease_keepsDuty() {
        InMemoryChargeDispatcher worker = new InMemoryChargeDispatcher(orderRepository, mock(OrderCharger.class), inventoryClient, circuitBreaker, 0, 5000);
        when(orderRepository.findReleasePendingIds()).thenReturn(List.of("order-r2"));
        when(inventoryClient.release(eq("order-r2"), any(Duration.class))).thenReturn(false);

        worker.reaper();

        verify(inventoryClient).release(eq("order-r2"), any(Duration.class));
        verify(orderRepository, never()).clearReleaseDuty(anyString());
    }

    @Test
    void reaper_breakerOpen_releaseSweepStillRuns_pendingSweepSkipped() {
        // Payment blackhole: the charge sweep must pause, but reservations of
        // rejected orders still have to be released (Inventory is a different service).
        InMemoryChargeDispatcher worker = new InMemoryChargeDispatcher(orderRepository, mock(OrderCharger.class), inventoryClient, circuitBreaker, 0, 5000);
        circuitBreaker.onUnavailable();
        when(orderRepository.findReleasePendingIds()).thenReturn(List.of("order-r3"));
        when(inventoryClient.release(eq("order-r3"), any(Duration.class))).thenReturn(true);
        when(orderRepository.findPendingIds()).thenReturn(List.of("order-p1"));

        worker.reaper();

        verify(inventoryClient).release(eq("order-r3"), any(Duration.class));
        verify(orderRepository).clearReleaseDuty("order-r3");
        assertTrue(worker.getQueue().isEmpty(), "pending sweep must be skipped while breaker is OPEN");
    }

    @Test
    void reaper_releaseSweep_onlyReleasesDutyOrders() {
        InMemoryChargeDispatcher worker = new InMemoryChargeDispatcher(orderRepository, mock(OrderCharger.class), inventoryClient, circuitBreaker, 0, 5000);
        when(orderRepository.findReleasePendingIds()).thenReturn(List.of());

        worker.reaper();

        verify(inventoryClient, never()).release(anyString(), any(Duration.class));
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
