package orders.flow;

import orders.inventory.InventoryClient;
import orders.payment.PaymentClient;
import orders.store.OrderRepository;
import org.junit.jupiter.api.Test;
import org.mockito.InOrder;

import java.math.BigDecimal;
import java.time.Duration;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class OrderChargerTest {

    private final InventoryClient inventoryClient = mock(InventoryClient.class);
    private final PaymentClient paymentClient = mock(PaymentClient.class);
    private final OrderRepository orderRepository = mock(OrderRepository.class);
    private final PaymentCircuitBreaker circuitBreaker = new PaymentCircuitBreaker(5000, 10000);
    private final OrderCharger charger = new OrderCharger(
            inventoryClient, paymentClient, orderRepository, circuitBreaker, 1000, 2000);

    @Test
    void charge_alreadyPaid_returnsTrue() {
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-1", "user", "paid", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-1")).thenReturn(order);

        assertTrue(charger.charge("order-1", Duration.ofSeconds(5)));
        verify(paymentClient, never()).charge(anyString(), any(BigDecimal.class), any(Duration.class));
    }

    @Test
    void charge_alreadyRejected_returnsTrue() {
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-1", "user", "rejected", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-1")).thenReturn(order);

        assertTrue(charger.charge("order-1", Duration.ofSeconds(5)));
    }

    @Test
    void charge_orderNotFound_returnsFalse() {
        when(orderRepository.findById("order-1")).thenReturn(null);

        assertFalse(charger.charge("order-1", Duration.ofSeconds(5)));
    }

    @Test
    void charge_charged_returnsTrue() {
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-1", "user", "pending", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-1")).thenReturn(order);
        when(orderRepository.transition("order-1", OrderStatus.PAID)).thenReturn(1);
        when(paymentClient.charge(eq("order-1"), eq(new BigDecimal("1000")), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.CHARGED);

        assertTrue(charger.charge("order-1", Duration.ofSeconds(5)));
        // POST-first: no reconciliation GET before/without a NETWORK outcome
        verify(paymentClient, never()).reconcile(eq("order-1"), any(Duration.class));
        verify(paymentClient).charge(eq("order-1"), eq(new BigDecimal("1000")), any(Duration.class));
        verify(orderRepository).transition("order-1", OrderStatus.PAID);
    }

    @Test
    void charge_declined_returnsTrue() {
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-1", "user", "pending", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-1")).thenReturn(order);
        when(paymentClient.charge(anyString(), any(BigDecimal.class), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.DECLINED);
        when(orderRepository.transition("order-1", OrderStatus.REJECTED)).thenReturn(1);
        when(inventoryClient.release(anyString(), any(Duration.class))).thenReturn(true);

        assertTrue(charger.charge("order-1", Duration.ofSeconds(5)));
        verify(paymentClient).charge(eq("order-1"), eq(new BigDecimal("1000")), any(Duration.class));
        verify(inventoryClient).release(eq("order-1"), any(Duration.class));
        verify(orderRepository).transition("order-1", OrderStatus.REJECTED);
    }

    @Test
    void charge_serverError_thenSucceed_returnsTrue() {
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-3", "user", "pending", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-3")).thenReturn(order);
        when(paymentClient.charge(eq("order-3"), eq(new BigDecimal("1000")), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.SERVER_ERROR)
                .thenReturn(PaymentClient.ChargeResult.CHARGED);
        when(orderRepository.transition("order-3", OrderStatus.PAID)).thenReturn(1);

        assertTrue(charger.charge("order-3", Duration.ofSeconds(5)));
        verify(paymentClient, times(2)).charge(eq("order-3"), eq(new BigDecimal("1000")), any(Duration.class));
    }

    @Test
    void charge_serverErrorDeclined_returnsTrue() {
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-8", "user", "pending", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-8")).thenReturn(order);
        when(paymentClient.charge(eq("order-8"), eq(new BigDecimal("1000")), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.SERVER_ERROR)
                .thenReturn(PaymentClient.ChargeResult.DECLINED);
        when(orderRepository.transition("order-8", OrderStatus.REJECTED)).thenReturn(1);
        when(inventoryClient.release(anyString(), any(Duration.class))).thenReturn(true);

        assertTrue(charger.charge("order-8", Duration.ofSeconds(5)));
        verify(paymentClient, times(2)).charge(eq("order-8"), eq(new BigDecimal("1000")), any(Duration.class));
        verify(inventoryClient).release(eq("order-8"), any(Duration.class));
    }

    @Test
    void charge_networkReconciledCharged_returnsTrue() {
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-4", "user", "pending", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-4")).thenReturn(order);
        when(paymentClient.charge(anyString(), any(BigDecimal.class), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.NETWORK);
        when(paymentClient.reconcile(eq("order-4"), any(Duration.class)))
                .thenReturn(PaymentClient.ReconciliationResult.CHARGED);
        when(orderRepository.transition("order-4", OrderStatus.PAID)).thenReturn(1);

        assertTrue(charger.charge("order-4", Duration.ofSeconds(5)));
        verify(paymentClient).charge(eq("order-4"), eq(new BigDecimal("1000")), any(Duration.class));
        // reconciliation GET happens exactly once — after the NETWORK outcome
        verify(paymentClient, times(1)).reconcile(eq("order-4"), any(Duration.class));
        verify(orderRepository).transition("order-4", OrderStatus.PAID);
        // the successful lookup proves Payment is reachable: breaker closes from OPEN
        assertEquals(PaymentCircuitBreaker.State.CLOSED, circuitBreaker.getState());
    }

    @Test
    void charge_networkNotCharged_rejectsAndOpensBreaker() {
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-5", "user", "pending", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-5")).thenReturn(order);
        when(paymentClient.charge(anyString(), any(BigDecimal.class), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.NETWORK);
        when(paymentClient.reconcile(eq("order-5"), any(Duration.class)))
                .thenReturn(PaymentClient.ReconciliationResult.NOT_CHARGED);
        when(inventoryClient.release(anyString(), any(Duration.class))).thenReturn(true);
        when(orderRepository.transition("order-5", OrderStatus.REJECTED)).thenReturn(1);

        assertTrue(charger.charge("order-5", Duration.ofSeconds(5)));
        verify(paymentClient).charge(eq("order-5"), eq(new BigDecimal("1000")), any(Duration.class));
        // single reconciliation GET after the NETWORK outcome
        verify(paymentClient, times(1)).reconcile(eq("order-5"), any(Duration.class));
        verify(inventoryClient).release(eq("order-5"), any(Duration.class));
        verify(orderRepository).transition("order-5", OrderStatus.REJECTED);
        // breaker should be OPEN after NETWORK with no charge found
        assertEquals(PaymentCircuitBreaker.State.OPEN, circuitBreaker.getState());
    }

    @Test
    void charge_networkUnknown_staysPending() {
        // A failed lookup is "unable to verify", not "no charge": the order must
        // not be rejected (the money may have been charged), it stays pending.
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-15", "user", "pending", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-15")).thenReturn(order);
        when(paymentClient.charge(eq("order-15"), eq(new BigDecimal("1000")), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.NETWORK);
        when(paymentClient.reconcile(eq("order-15"), any(Duration.class)))
                .thenReturn(PaymentClient.ReconciliationResult.UNKNOWN);

        assertFalse(charger.charge("order-15", Duration.ofMillis(300)));
        verify(orderRepository, never()).transition(eq("order-15"), any(OrderStatus.class));
        verify(inventoryClient, never()).release(anyString(), any(Duration.class));
        verify(paymentClient, times(1)).charge(eq("order-15"), eq(new BigDecimal("1000")), any(Duration.class));
        verify(paymentClient, times(1)).reconcile(eq("order-15"), any(Duration.class));
        assertEquals(PaymentCircuitBreaker.State.OPEN, circuitBreaker.getState());
    }

    @Test
    void breakerOpenedBeforeReconcile() {
        // The spec requires the breaker to be OPEN within the charge timeout,
        // i.e. before the reconciliation GET is sent.
        PaymentCircuitBreaker breaker = mock(PaymentCircuitBreaker.class);
        when(breaker.allow()).thenReturn(true);
        OrderCharger c = new OrderCharger(inventoryClient, paymentClient, orderRepository, breaker, 1000, 2000);
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-16", "user", "pending", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-16")).thenReturn(order);
        when(paymentClient.charge(eq("order-16"), eq(new BigDecimal("1000")), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.NETWORK);
        when(paymentClient.reconcile(eq("order-16"), any(Duration.class)))
                .thenReturn(PaymentClient.ReconciliationResult.NOT_CHARGED);
        when(inventoryClient.release(anyString(), any(Duration.class))).thenReturn(true);
        when(orderRepository.transition("order-16", OrderStatus.REJECTED)).thenReturn(1);

        assertTrue(c.charge("order-16", Duration.ofSeconds(5)));

        InOrder inOrder = inOrder(breaker, paymentClient);
        inOrder.verify(breaker).onUnavailable();
        inOrder.verify(paymentClient).reconcile(eq("order-16"), any(Duration.class));
    }

    @Test
    void charge_idempotentChargeFound_returnsTrue() {
        // A charge executed in a previous attempt (response lost) is found by
        // the idempotent POST itself: same Idempotency-Key and body -> 200,
        // which PaymentClient maps to CHARGED. No reconciliation GET needed.
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-9", "user", "pending", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-9")).thenReturn(order);
        when(paymentClient.charge(eq("order-9"), eq(new BigDecimal("1000")), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.CHARGED);
        when(orderRepository.transition("order-9", OrderStatus.PAID)).thenReturn(1);

        assertTrue(charger.charge("order-9", Duration.ofSeconds(5)));
        verify(paymentClient, times(1)).charge(eq("order-9"), eq(new BigDecimal("1000")), any(Duration.class));
        verify(paymentClient, never()).reconcile(eq("order-9"), any(Duration.class));
        verify(orderRepository).transition("order-9", OrderStatus.PAID);
    }

    @Test
    void charge_deadlineExpires_returnsFalse() {
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-10", "user", "pending", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-10")).thenReturn(order);
        when(paymentClient.charge(anyString(), any(BigDecimal.class), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.NETWORK);

        // 0ms deadline means immediate timeout
        assertFalse(charger.charge("order-10", Duration.ZERO));
    }

    @Test
    void charge_transitionCompetitor_returnsTrue() {
        // Another thread/process transitioned to "paid" between findById and transition
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-11", "user", "pending", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-11")).thenReturn(order);
        when(paymentClient.charge(eq("order-11"), eq(new BigDecimal("1000")), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.CHARGED);
        // First transition fails (another process did it), but findById now returns "paid"
        when(orderRepository.transition("order-11", OrderStatus.PAID)).thenReturn(0);
        when(orderRepository.findById("order-11")).thenReturn(
                new OrderRepository.StoredOrder("order-11", "user", "paid", new BigDecimal("1000"), "[]"));

        assertTrue(charger.charge("order-11", Duration.ofSeconds(5)));
    }

    @Test
    void charge_declined_closesBreaker() {
        // 402 on a probe must close the breaker, not wedge it in HALF_OPEN/OPEN.
        PaymentCircuitBreaker breaker = new PaymentCircuitBreaker(50, 10000);
        breaker.onUnavailable();
        OrderCharger c = new OrderCharger(inventoryClient, paymentClient, orderRepository, breaker, 1000, 2000);
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-17", "user", "pending", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-17")).thenReturn(order);
        when(paymentClient.charge(eq("order-17"), eq(new BigDecimal("1000")), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.DECLINED);
        when(inventoryClient.release(anyString(), any(Duration.class))).thenReturn(true);
        when(orderRepository.transition("order-17", OrderStatus.REJECTED)).thenReturn(1);

        assertTrue(c.charge("order-17", Duration.ofSeconds(10)));
        verify(inventoryClient).release(eq("order-17"), any(Duration.class));
        verify(orderRepository).transition("order-17", OrderStatus.REJECTED);
        assertEquals(PaymentCircuitBreaker.State.CLOSED, breaker.getState());
    }

    @Test
    void charge_serverErrorTwice_closesBreaker() {
        PaymentCircuitBreaker breaker = new PaymentCircuitBreaker(50, 10000);
        breaker.onUnavailable();
        OrderCharger c = new OrderCharger(inventoryClient, paymentClient, orderRepository, breaker, 1000, 2000);
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-18", "user", "pending", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-18")).thenReturn(order);
        when(paymentClient.charge(eq("order-18"), eq(new BigDecimal("1000")), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.SERVER_ERROR)
                .thenReturn(PaymentClient.ChargeResult.SERVER_ERROR)
                .thenReturn(PaymentClient.ChargeResult.CHARGED);
        when(orderRepository.transition("order-18", OrderStatus.PAID)).thenReturn(1);

        assertTrue(c.charge("order-18", Duration.ofSeconds(10)));
        // 500 is an answer: reachable. The loop continues and the order is paid.
        assertEquals(PaymentCircuitBreaker.State.CLOSED, breaker.getState());
        verify(paymentClient, times(3)).charge(eq("order-18"), eq(new BigDecimal("1000")), any(Duration.class));
        verify(orderRepository).transition("order-18", OrderStatus.PAID);
    }

    @Test
    void charge_serverErrorThenNetwork_reconciles() {
        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-19", "user", "pending", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-19")).thenReturn(order);
        when(paymentClient.charge(eq("order-19"), eq(new BigDecimal("1000")), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.SERVER_ERROR)
                .thenReturn(PaymentClient.ChargeResult.NETWORK);
        when(paymentClient.reconcile(eq("order-19"), any(Duration.class)))
                .thenReturn(PaymentClient.ReconciliationResult.CHARGED);
        when(orderRepository.transition("order-19", OrderStatus.PAID)).thenReturn(1);

        assertTrue(charger.charge("order-19", Duration.ofSeconds(5)));
        verify(paymentClient, times(1)).reconcile(eq("order-19"), any(Duration.class));
        verify(orderRepository).transition("order-19", OrderStatus.PAID);
    }

    @Test
    void charge_breakerOpen_waitsAndRetries() throws InterruptedException {
        // Set breaker to OPEN
        circuitBreaker.onUnavailable();
        assertEquals(PaymentCircuitBreaker.State.OPEN, circuitBreaker.getState());

        OrderRepository.StoredOrder order = new OrderRepository.StoredOrder("order-12", "user", "pending", new BigDecimal("1000"), "[]");
        when(orderRepository.findById("order-12")).thenReturn(order);
        // When breaker transitions to HALF_OPEN, charge succeeds
        when(paymentClient.charge(eq("order-12"), eq(new BigDecimal("1000")), any(Duration.class)))
                .thenReturn(PaymentClient.ChargeResult.CHARGED);
        when(orderRepository.transition("order-12", OrderStatus.PAID)).thenReturn(1);

        // 30s deadline — long enough for breaker to transition (openForMs=5s in this test)
        assertTrue(charger.charge("order-12", Duration.ofSeconds(30)));
        assertEquals(PaymentCircuitBreaker.State.CLOSED, circuitBreaker.getState());
    }
}
