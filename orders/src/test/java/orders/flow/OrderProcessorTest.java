package orders.flow;

import orders.inventory.InventoryClient;
import orders.payment.PaymentClient;
import orders.store.OrderRepository;
import org.junit.jupiter.api.Test;

import java.time.Duration;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class OrderProcessorTest {

    private final InventoryClient inventoryClient = mock(InventoryClient.class);
    private final PaymentClient paymentClient = mock(PaymentClient.class);
    private final OrderRepository orderRepository = mock(OrderRepository.class);
    private final PaymentCircuitBreaker circuitBreaker = new PaymentCircuitBreaker(5000);
    private final OrderProcessor processor = new OrderProcessor(
            inventoryClient, paymentClient, orderRepository, circuitBreaker);

    // --- processOnce tests ---

    @Test
    void processOnce_reserveSucceeds_returnsPending() {
        when(inventoryClient.reserve(anyString(), anyString(), any(Duration.class))).thenReturn(true);

        String result = processor.processOnce("order-1", 1000, "[]", Duration.ofSeconds(5));
        assertEquals("PENDING", result);
        // processOnce does not call payment — that's in the background worker
        verify(paymentClient, never()).charge(anyString(), anyLong(), any(Duration.class));
        verify(paymentClient, never()).hasCharges(anyString(), any(Duration.class));
    }

    @Test
    void processOnce_reserveFails_returnsRejected() {
        when(inventoryClient.reserve(anyString(), anyString(), any(Duration.class))).thenReturn(false);

        String result = processor.processOnce("order-6", 1000, "[]", Duration.ofSeconds(5));
        assertEquals("REJECTED", result);
        verify(paymentClient, never()).charge(anyString(), anyLong(), any(Duration.class));
    }

    @Test
    void processOnce_shortBudget_usesMinTimeout() {
        when(inventoryClient.reserve(anyString(), anyString(), any(Duration.class))).thenReturn(true);

        processor.processOnce("order-short", 1000, "[]", Duration.ofMillis(50));

        // budget.toMillis() - 10 = 40, Math.max(100, 40) = 100
        verify(inventoryClient).reserve(eq("order-short"), eq("[]"), eq(Duration.ofMillis(100)));
    }

    // --- processUntilTerminal tests ---

    @Test
    void processUntilTerminal_alreadyPaid_returnsTrue() {
        OrderRepository.Order order = new OrderRepository.Order("order-1", "user", "paid", 1000, "[]");
        when(orderRepository.findById("order-1")).thenReturn(order);

        assertTrue(processor.processUntilTerminal("order-1", "[]", Duration.ofSeconds(5)));
        verify(paymentClient, never()).charge(anyString(), anyLong(), any(Duration.class));
    }

    @Test
    void processUntilTerminal_alreadyRejected_returnsTrue() {
        OrderRepository.Order order = new OrderRepository.Order("order-1", "user", "rejected", 1000, "[]");
        when(orderRepository.findById("order-1")).thenReturn(order);

        assertTrue(processor.processUntilTerminal("order-1", "[]", Duration.ofSeconds(5)));
    }

    @Test
    void processUntilTerminal_orderNotFound_returnsFalse() {
        when(orderRepository.findById("order-1")).thenReturn(null);

        assertFalse(processor.processUntilTerminal("order-1", "[]", Duration.ofSeconds(5)));
    }

    @Test
    void processUntilTerminal_charged_returnsTrue() {
        OrderRepository.Order order = new OrderRepository.Order("order-1", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-1")).thenReturn(order);
        when(orderRepository.transition("order-1", "paid")).thenReturn(1);
        when(paymentClient.charge(eq("order-1"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.CHARGED);

        assertTrue(processor.processUntilTerminal("order-1", "[]", Duration.ofSeconds(5)));
        verify(paymentClient).hasCharges(eq("order-1"), any(Duration.class));
        verify(paymentClient).charge(eq("order-1"), eq(1000L), any(Duration.class));
        verify(orderRepository).transition("order-1", "paid");
    }

    @Test
    void processUntilTerminal_declined_returnsTrue() {
        OrderRepository.Order order = new OrderRepository.Order("order-1", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-1")).thenReturn(order);
        when(paymentClient.charge(anyString(), anyLong(), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.DECLINED);
        when(orderRepository.transition("order-1", "rejected")).thenReturn(1);
        when(inventoryClient.release(anyString(), any(Duration.class))).thenReturn(true);

        assertTrue(processor.processUntilTerminal("order-1", "[]", Duration.ofSeconds(5)));
        verify(paymentClient).charge(eq("order-1"), eq(1000L), any(Duration.class));
        verify(inventoryClient).release(eq("order-1"), any(Duration.class));
        verify(orderRepository).transition("order-1", "rejected");
    }

    @Test
    void processUntilTerminal_serverError_thenSucceed_returnsTrue() {
        OrderRepository.Order order = new OrderRepository.Order("order-3", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-3")).thenReturn(order);
        when(paymentClient.charge(eq("order-3"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.SERVER_ERROR)
                .thenReturn(PaymentClient.ChargeResult.CHARGED);
        when(orderRepository.transition("order-3", "paid")).thenReturn(1);

        assertTrue(processor.processUntilTerminal("order-3", "[]", Duration.ofSeconds(5)));
        verify(paymentClient, times(2)).charge(eq("order-3"), eq(1000L), any(Duration.class));
    }

    @Test
    void processUntilTerminal_serverErrorDeclined_returnsTrue() {
        OrderRepository.Order order = new OrderRepository.Order("order-8", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-8")).thenReturn(order);
        when(paymentClient.charge(eq("order-8"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.SERVER_ERROR)
                .thenReturn(PaymentClient.ChargeResult.DECLINED);
        when(orderRepository.transition("order-8", "rejected")).thenReturn(1);
        when(inventoryClient.release(anyString(), any(Duration.class))).thenReturn(true);

        assertTrue(processor.processUntilTerminal("order-8", "[]", Duration.ofSeconds(5)));
        verify(paymentClient, times(2)).charge(eq("order-8"), eq(1000L), any(Duration.class));
        verify(inventoryClient).release(eq("order-8"), any(Duration.class));
    }

    @Test
    void processUntilTerminal_networkWithHasCharges_returnsTrue() {
        OrderRepository.Order order = new OrderRepository.Order("order-4", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-4")).thenReturn(order);
        when(paymentClient.charge(anyString(), anyLong(), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.NETWORK);
        when(paymentClient.hasCharges(eq("order-4"), any(Duration.class)))
                .thenReturn(false).thenReturn(true);
        when(orderRepository.transition("order-4", "paid")).thenReturn(1);

        assertTrue(processor.processUntilTerminal("order-4", "[]", Duration.ofSeconds(5)));
        verify(paymentClient).charge(eq("order-4"), eq(1000L), any(Duration.class));
        verify(paymentClient, times(2)).hasCharges(eq("order-4"), any(Duration.class));
        verify(orderRepository).transition("order-4", "paid");
    }

    @Test
    void processUntilTerminal_networkNoHasCharges_opensBreaker() {
        OrderRepository.Order order = new OrderRepository.Order("order-5", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-5")).thenReturn(order);
        when(paymentClient.charge(anyString(), anyLong(), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.NETWORK);
        when(paymentClient.hasCharges(eq("order-5"), any(Duration.class))).thenReturn(false);
        when(inventoryClient.release(anyString(), any(Duration.class))).thenReturn(true);
        when(orderRepository.transition("order-5", "rejected")).thenReturn(1);

        assertTrue(processor.processUntilTerminal("order-5", "[]", Duration.ofSeconds(5)));
        verify(paymentClient).charge(eq("order-5"), eq(1000L), any(Duration.class));
        // breaker should be OPEN after NETWORK with no charge found
        assertEquals(PaymentCircuitBreaker.State.OPEN, circuitBreaker.getState());
    }

    @Test
    void processUntilTerminal_hasChargesFirst_returnsTrue() {
        OrderRepository.Order order = new OrderRepository.Order("order-9", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-9")).thenReturn(order);
        when(paymentClient.hasCharges(eq("order-9"), any(Duration.class))).thenReturn(true);
        when(orderRepository.transition("order-9", "paid")).thenReturn(1);

        assertTrue(processor.processUntilTerminal("order-9", "[]", Duration.ofSeconds(5)));
        // Should not call charge — hasCharges found it already
        verify(paymentClient, never()).charge(eq("order-9"), anyLong(), any(Duration.class));
        verify(orderRepository).transition("order-9", "paid");
    }

    @Test
    void processUntilTerminal_deadlineExpires_returnsFalse() {
        OrderRepository.Order order = new OrderRepository.Order("order-10", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-10")).thenReturn(order);
        when(paymentClient.charge(anyString(), anyLong(), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.NETWORK);
        when(paymentClient.hasCharges(eq("order-10"), any(Duration.class))).thenReturn(false);

        // 0ms deadline means immediate timeout
        assertFalse(processor.processUntilTerminal("order-10", "[]", Duration.ZERO));
    }

    @Test
    void processUntilTerminal_transitionCompetitor_returnsTrue() {
        // Another thread/process transitioned to "paid" between findById and transition
        OrderRepository.Order order = new OrderRepository.Order("order-11", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-11")).thenReturn(order);
        when(paymentClient.charge(eq("order-11"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.CHARGED);
        // First transition fails (another process did it), but findById now returns "paid"
        when(orderRepository.transition("order-11", "paid")).thenReturn(0);
        when(orderRepository.findById("order-11")).thenReturn(
                new OrderRepository.Order("order-11", "user", "paid", 1000, "[]"));

        assertTrue(processor.processUntilTerminal("order-11", "[]", Duration.ofSeconds(5)));
    }

    @Test
    void processUntilTerminal_breakerOpen_waitsAndRetries() throws InterruptedException {
        // Set breaker to OPEN
        circuitBreaker.onTimeoutOrConnError();
        assertEquals(PaymentCircuitBreaker.State.OPEN, circuitBreaker.getState());

        OrderRepository.Order order = new OrderRepository.Order("order-12", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-12")).thenReturn(order);
        // When breaker transitions to HALF_OPEN, charge succeeds
        when(paymentClient.charge(eq("order-12"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.CHARGED);
        when(orderRepository.transition("order-12", "paid")).thenReturn(1);

        // 30s deadline — long enough for breaker to transition (openForMs=5s in this test)
        assertTrue(processor.processUntilTerminal("order-12", "[]", Duration.ofSeconds(30)));
        assertEquals(PaymentCircuitBreaker.State.CLOSED, circuitBreaker.getState());
    }
}
