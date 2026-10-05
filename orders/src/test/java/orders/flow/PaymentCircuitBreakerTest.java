package orders.flow;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class PaymentCircuitBreakerTest {

    @Test
    void initialStateIsClosed() {
        PaymentCircuitBreaker cb = new PaymentCircuitBreaker(5000);
        assertEquals(PaymentCircuitBreaker.State.CLOSED, cb.getState());
    }

    @Test
    void firstNetworkErrorOpensCircuit() {
        PaymentCircuitBreaker cb = new PaymentCircuitBreaker(5000);
        assertTrue(cb.allow());
        cb.onTimeoutOrConnError();
        assertEquals(PaymentCircuitBreaker.State.OPEN, cb.getState());
    }

    @Test
    void openBlocksRequests() {
        PaymentCircuitBreaker cb = new PaymentCircuitBreaker(5000);
        cb.onTimeoutOrConnError();
        assertFalse(cb.allow());
    }

    @Test
    void halfOpenAllowsOneProbe() throws InterruptedException {
        PaymentCircuitBreaker cb = new PaymentCircuitBreaker(100);
        cb.onTimeoutOrConnError();
        Thread.sleep(200);

        boolean first = cb.allow();
        System.out.println("First allow: " + first + ", state: " + cb.getState());
        Thread.sleep(10);
        boolean second = cb.allow();
        System.out.println("Second allow: " + second + ", state: " + cb.getState());
        assertTrue(first, "First allow should be true");
        assertFalse(second, "Second allow should be false, but was " + second);
    }

    @Test
    void halfOpenSuccessClosesCircuit() throws InterruptedException {
        PaymentCircuitBreaker cb = new PaymentCircuitBreaker(100);
        cb.onTimeoutOrConnError();
        Thread.sleep(200);

        assertTrue(cb.allow());
        cb.onSuccess();
        assertEquals(PaymentCircuitBreaker.State.CLOSED, cb.getState());
    }

    @Test
    void halfOpenFailureReopensCircuit() throws InterruptedException {
        PaymentCircuitBreaker cb = new PaymentCircuitBreaker(100);
        cb.onTimeoutOrConnError();
        Thread.sleep(200);

        assertTrue(cb.allow());
        cb.onTimeoutOrConnError();
        assertEquals(PaymentCircuitBreaker.State.OPEN, cb.getState());
    }

    @Test
    void serverError500DoesNotOpenBreaker() {
        PaymentCircuitBreaker cb = new PaymentCircuitBreaker(5000);
        cb.allow();
        cb.onSuccess();
        assertEquals(PaymentCircuitBreaker.State.CLOSED, cb.getState());
    }
}
