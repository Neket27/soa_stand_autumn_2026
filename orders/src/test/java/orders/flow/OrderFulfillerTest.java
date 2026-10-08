package orders.flow;

import orders.inventory.InventoryClient;
import orders.payment.PaymentClient;
import orders.store.OrderRepository;
import org.junit.jupiter.api.Test;
import org.mockito.InOrder;

import java.time.Duration;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class OrderFulfillerTest {

    private final InventoryClient inventoryClient = mock(InventoryClient.class);
    private final PaymentClient paymentClient = mock(PaymentClient.class);
    private final OrderRepository orderRepository = mock(OrderRepository.class);
    private final PaymentCircuitBreaker circuitBreaker = new PaymentCircuitBreaker(5000, 10000);
    private final OrderFulfiller fulfiller = new OrderFulfiller(
            inventoryClient, paymentClient, orderRepository, circuitBreaker, 1000, 2000);

    @Test
    void fulfill_alreadyPaid_returnsTrue() {
        OrderRepository.Order order = new OrderRepository.Order("order-1", "user", "paid", 1000, "[]");
        when(orderRepository.findById("order-1")).thenReturn(order);

        assertTrue(fulfiller.fulfill("order-1", Duration.ofSeconds(5)));
        verify(paymentClient, never()).charge(anyString(), anyLong(), any(Duration.class));
    }

    @Test
    void fulfill_alreadyRejected_returnsTrue() {
        OrderRepository.Order order = new OrderRepository.Order("order-1", "user", "rejected", 1000, "[]");
        when(orderRepository.findById("order-1")).thenReturn(order);

        assertTrue(fulfiller.fulfill("order-1", Duration.ofSeconds(5)));
    }

    @Test
    void fulfill_orderNotFound_returnsFalse() {
        when(orderRepository.findById("order-1")).thenReturn(null);

        assertFalse(fulfiller.fulfill("order-1", Duration.ofSeconds(5)));
    }

    @Test
    void fulfill_charged_returnsTrue() {
        OrderRepository.Order order = new OrderRepository.Order("order-1", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-1")).thenReturn(order);
        when(orderRepository.transition("order-1", OrderStatus.PAID)).thenReturn(1);
        when(paymentClient.charge(eq("order-1"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.CHARGED);

        assertTrue(fulfiller.fulfill("order-1", Duration.ofSeconds(5)));
        // POST-first: no reconciliation GET before/without a NETWORK outcome
        verify(paymentClient, never()).reconcile(eq("order-1"), any(Duration.class));
        verify(paymentClient).charge(eq("order-1"), eq(1000L), any(Duration.class));
        verify(orderRepository).transition("order-1", OrderStatus.PAID);
    }

    @Test
    void fulfill_declined_returnsTrue() {
        OrderRepository.Order order = new OrderRepository.Order("order-1", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-1")).thenReturn(order);
        when(paymentClient.charge(anyString(), anyLong(), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.DECLINED);
        when(orderRepository.transition("order-1", OrderStatus.REJECTED)).thenReturn(1);
        when(inventoryClient.release(anyString(), any(Duration.class))).thenReturn(true);

        assertTrue(fulfiller.fulfill("order-1", Duration.ofSeconds(5)));
        verify(paymentClient).charge(eq("order-1"), eq(1000L), any(Duration.class));
        verify(inventoryClient).release(eq("order-1"), any(Duration.class));
        verify(orderRepository).transition("order-1", OrderStatus.REJECTED);
    }

    @Test
    void fulfill_serverError_thenSucceed_returnsTrue() {
        OrderRepository.Order order = new OrderRepository.Order("order-3", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-3")).thenReturn(order);
        when(paymentClient.charge(eq("order-3"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.SERVER_ERROR)
                .thenReturn(PaymentClient.ChargeResult.CHARGED);
        when(orderRepository.transition("order-3", OrderStatus.PAID)).thenReturn(1);

        assertTrue(fulfiller.fulfill("order-3", Duration.ofSeconds(5)));
        verify(paymentClient, times(2)).charge(eq("order-3"), eq(1000L), any(Duration.class));
    }

    @Test
    void fulfill_serverErrorDeclined_returnsTrue() {
        OrderRepository.Order order = new OrderRepository.Order("order-8", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-8")).thenReturn(order);
        when(paymentClient.charge(eq("order-8"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.SERVER_ERROR)
                .thenReturn(PaymentClient.ChargeResult.DECLINED);
        when(orderRepository.transition("order-8", OrderStatus.REJECTED)).thenReturn(1);
        when(inventoryClient.release(anyString(), any(Duration.class))).thenReturn(true);

        assertTrue(fulfiller.fulfill("order-8", Duration.ofSeconds(5)));
        verify(paymentClient, times(2)).charge(eq("order-8"), eq(1000L), any(Duration.class));
        verify(inventoryClient).release(eq("order-8"), any(Duration.class));
    }

    @Test
    void fulfill_networkReconciledCharged_returnsTrue() {
        OrderRepository.Order order = new OrderRepository.Order("order-4", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-4")).thenReturn(order);
        when(paymentClient.charge(anyString(), anyLong(), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.NETWORK);
        when(paymentClient.reconcile(eq("order-4"), any(Duration.class)))
                .thenReturn(PaymentClient.ReconciliationResult.CHARGED);
        when(orderRepository.transition("order-4", OrderStatus.PAID)).thenReturn(1);

        assertTrue(fulfiller.fulfill("order-4", Duration.ofSeconds(5)));
        verify(paymentClient).charge(eq("order-4"), eq(1000L), any(Duration.class));
        // reconciliation GET happens exactly once — after the NETWORK outcome
        verify(paymentClient, times(1)).reconcile(eq("order-4"), any(Duration.class));
        verify(orderRepository).transition("order-4", OrderStatus.PAID);
        // the successful lookup proves Payment is reachable: breaker closes from OPEN
        assertEquals(PaymentCircuitBreaker.State.CLOSED, circuitBreaker.getState());
    }

    @Test
    void fulfill_networkNotCharged_rejectsAndOpensBreaker() {
        OrderRepository.Order order = new OrderRepository.Order("order-5", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-5")).thenReturn(order);
        when(paymentClient.charge(anyString(), anyLong(), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.NETWORK);
        when(paymentClient.reconcile(eq("order-5"), any(Duration.class)))
                .thenReturn(PaymentClient.ReconciliationResult.NOT_CHARGED);
        when(inventoryClient.release(anyString(), any(Duration.class))).thenReturn(true);
        when(orderRepository.transition("order-5", OrderStatus.REJECTED)).thenReturn(1);

        assertTrue(fulfiller.fulfill("order-5", Duration.ofSeconds(5)));
        verify(paymentClient).charge(eq("order-5"), eq(1000L), any(Duration.class));
        // single reconciliation GET after the NETWORK outcome
        verify(paymentClient, times(1)).reconcile(eq("order-5"), any(Duration.class));
        verify(inventoryClient).release(eq("order-5"), any(Duration.class));
        verify(orderRepository).transition("order-5", OrderStatus.REJECTED);
        // breaker should be OPEN after NETWORK with no charge found
        assertEquals(PaymentCircuitBreaker.State.OPEN, circuitBreaker.getState());
    }

    @Test
    void fulfill_networkUnknown_staysPending() {
        // A failed lookup is "unable to verify", not "no charge": the order must
        // not be rejected (the money may have been charged), it stays pending.
        OrderRepository.Order order = new OrderRepository.Order("order-15", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-15")).thenReturn(order);
        when(paymentClient.charge(eq("order-15"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.NETWORK);
        when(paymentClient.reconcile(eq("order-15"), any(Duration.class)))
                .thenReturn(PaymentClient.ReconciliationResult.UNKNOWN);

        assertFalse(fulfiller.fulfill("order-15", Duration.ofMillis(300)));
        verify(orderRepository, never()).transition(eq("order-15"), any(OrderStatus.class));
        verify(inventoryClient, never()).release(anyString(), any(Duration.class));
        verify(paymentClient, times(1)).charge(eq("order-15"), eq(1000L), any(Duration.class));
        verify(paymentClient, times(1)).reconcile(eq("order-15"), any(Duration.class));
        assertEquals(PaymentCircuitBreaker.State.OPEN, circuitBreaker.getState());
    }

    @Test
    void breakerOpenedBeforeReconcile() {
        // The spec requires the breaker to be OPEN within the charge timeout,
        // i.e. before the reconciliation GET is sent.
        PaymentCircuitBreaker breaker = mock(PaymentCircuitBreaker.class);
        when(breaker.allow()).thenReturn(true);
        OrderFulfiller f = new OrderFulfiller(inventoryClient, paymentClient, orderRepository, breaker, 1000, 2000);
        OrderRepository.Order order = new OrderRepository.Order("order-16", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-16")).thenReturn(order);
        when(paymentClient.charge(eq("order-16"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.NETWORK);
        when(paymentClient.reconcile(eq("order-16"), any(Duration.class)))
                .thenReturn(PaymentClient.ReconciliationResult.NOT_CHARGED);
        when(inventoryClient.release(anyString(), any(Duration.class))).thenReturn(true);
        when(orderRepository.transition("order-16", OrderStatus.REJECTED)).thenReturn(1);

        assertTrue(f.fulfill("order-16", Duration.ofSeconds(5)));

        InOrder inOrder = inOrder(breaker, paymentClient);
        inOrder.verify(breaker).onUnavailable();
        inOrder.verify(paymentClient).reconcile(eq("order-16"), any(Duration.class));
    }

    @Test
    void fulfill_idempotentChargeFound_returnsTrue() {
        // A charge executed in a previous attempt (response lost) is found by
        // the idempotent POST itself: same Idempotency-Key and body -> 200,
        // which PaymentClient maps to CHARGED. No reconciliation GET needed.
        OrderRepository.Order order = new OrderRepository.Order("order-9", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-9")).thenReturn(order);
        when(paymentClient.charge(eq("order-9"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.CHARGED);
        when(orderRepository.transition("order-9", OrderStatus.PAID)).thenReturn(1);

        assertTrue(fulfiller.fulfill("order-9", Duration.ofSeconds(5)));
        verify(paymentClient, times(1)).charge(eq("order-9"), eq(1000L), any(Duration.class));
        verify(paymentClient, never()).reconcile(eq("order-9"), any(Duration.class));
        verify(orderRepository).transition("order-9", OrderStatus.PAID);
    }

    @Test
    void fulfill_deadlineExpires_returnsFalse() {
        OrderRepository.Order order = new OrderRepository.Order("order-10", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-10")).thenReturn(order);
        when(paymentClient.charge(anyString(), anyLong(), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.NETWORK);

        // 0ms deadline means immediate timeout
        assertFalse(fulfiller.fulfill("order-10", Duration.ZERO));
    }

    @Test
    void fulfill_transitionCompetitor_returnsTrue() {
        // Another thread/process transitioned to "paid" between findById and transition
        OrderRepository.Order order = new OrderRepository.Order("order-11", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-11")).thenReturn(order);
        when(paymentClient.charge(eq("order-11"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.CHARGED);
        // First transition fails (another process did it), but findById now returns "paid"
        when(orderRepository.transition("order-11", OrderStatus.PAID)).thenReturn(0);
        when(orderRepository.findById("order-11")).thenReturn(
                new OrderRepository.Order("order-11", "user", "paid", 1000, "[]"));

        assertTrue(fulfiller.fulfill("order-11", Duration.ofSeconds(5)));
    }

    @Test
    void fulfill_declined_closesBreaker() {
        // 402 on a probe must close the breaker, not wedge it in HALF_OPEN/OPEN.
        PaymentCircuitBreaker breaker = new PaymentCircuitBreaker(50, 10000);
        breaker.onUnavailable();
        OrderFulfiller f = new OrderFulfiller(inventoryClient, paymentClient, orderRepository, breaker, 1000, 2000);
        OrderRepository.Order order = new OrderRepository.Order("order-17", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-17")).thenReturn(order);
        when(paymentClient.charge(eq("order-17"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.DECLINED);
        when(inventoryClient.release(anyString(), any(Duration.class))).thenReturn(true);
        when(orderRepository.transition("order-17", OrderStatus.REJECTED)).thenReturn(1);

        assertTrue(f.fulfill("order-17", Duration.ofSeconds(10)));
        verify(inventoryClient).release(eq("order-17"), any(Duration.class));
        verify(orderRepository).transition("order-17", OrderStatus.REJECTED);
        assertEquals(PaymentCircuitBreaker.State.CLOSED, breaker.getState());
    }

    @Test
    void fulfill_serverErrorTwice_closesBreaker() {
        PaymentCircuitBreaker breaker = new PaymentCircuitBreaker(50, 10000);
        breaker.onUnavailable();
        OrderFulfiller f = new OrderFulfiller(inventoryClient, paymentClient, orderRepository, breaker, 1000, 2000);
        OrderRepository.Order order = new OrderRepository.Order("order-18", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-18")).thenReturn(order);
        when(paymentClient.charge(eq("order-18"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.SERVER_ERROR)
                .thenReturn(PaymentClient.ChargeResult.SERVER_ERROR)
                .thenReturn(PaymentClient.ChargeResult.CHARGED);
        when(orderRepository.transition("order-18", OrderStatus.PAID)).thenReturn(1);

        assertTrue(f.fulfill("order-18", Duration.ofSeconds(10)));
        // 500 is an answer: reachable. The loop continues and the order is paid.
        assertEquals(PaymentCircuitBreaker.State.CLOSED, breaker.getState());
        verify(paymentClient, times(3)).charge(eq("order-18"), eq(1000L), any(Duration.class));
        verify(orderRepository).transition("order-18", OrderStatus.PAID);
    }

    @Test
    void fulfill_serverErrorThenNetwork_reconciles() {
        OrderRepository.Order order = new OrderRepository.Order("order-19", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-19")).thenReturn(order);
        when(paymentClient.charge(eq("order-19"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.SERVER_ERROR)
                .thenReturn(PaymentClient.ChargeResult.NETWORK);
        when(paymentClient.reconcile(eq("order-19"), any(Duration.class)))
                .thenReturn(PaymentClient.ReconciliationResult.CHARGED);
        when(orderRepository.transition("order-19", OrderStatus.PAID)).thenReturn(1);

        assertTrue(fulfiller.fulfill("order-19", Duration.ofSeconds(5)));
        verify(paymentClient, times(1)).reconcile(eq("order-19"), any(Duration.class));
        verify(orderRepository).transition("order-19", OrderStatus.PAID);
    }

    @Test
    void fulfill_breakerOpen_waitsAndRetries() throws InterruptedException {
        // Set breaker to OPEN
        circuitBreaker.onUnavailable();
        assertEquals(PaymentCircuitBreaker.State.OPEN, circuitBreaker.getState());

        OrderRepository.Order order = new OrderRepository.Order("order-12", "user", "pending", 1000, "[]");
        when(orderRepository.findById("order-12")).thenReturn(order);
        // When breaker transitions to HALF_OPEN, charge succeeds
        when(paymentClient.charge(eq("order-12"), eq(1000L), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.CHARGED);
        when(orderRepository.transition("order-12", OrderStatus.PAID)).thenReturn(1);

        // 30s deadline — long enough for breaker to transition (openForMs=5s in this test)
        assertTrue(fulfiller.fulfill("order-12", Duration.ofSeconds(30)));
        assertEquals(PaymentCircuitBreaker.State.CLOSED, circuitBreaker.getState());
    }
}
