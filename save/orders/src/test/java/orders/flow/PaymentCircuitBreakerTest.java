package orders.flow;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class PaymentCircuitBreakerTest {

    private static PaymentCircuitBreaker breaker(long openForMs, long probeWindowMs) {
        return new PaymentCircuitBreaker(openForMs, probeWindowMs);
    }

    @Test
    void initialStateIsClosed() {
        assertEquals(PaymentCircuitBreaker.State.CLOSED, breaker(5000, 1000).getState());
    }

    @Test
    void firstNetworkErrorOpensCircuit() {
        PaymentCircuitBreaker cb = breaker(5000, 1000);
        assertTrue(cb.allow());
        cb.onUnavailable();
        assertEquals(PaymentCircuitBreaker.State.OPEN, cb.getState());
    }

    @Test
    void openBlocksRequests() {
        PaymentCircuitBreaker cb = breaker(5000, 1000);
        cb.onUnavailable();
        assertFalse(cb.allow());
    }

    @Test
    void lateUnavailableDoesNotRestartOpenWindow() throws InterruptedException {
        PaymentCircuitBreaker cb = breaker(100, 1000);
        cb.onUnavailable();
        cb.onUnavailable(); // late timeout of a charge that was already in flight
        Thread.sleep(150);
        assertTrue(cb.allow(), "open window must not have been restarted by the late report");
    }

    @Test
    void halfOpenAllowsOneProbe() throws InterruptedException {
        PaymentCircuitBreaker cb = breaker(100, 1000);
        cb.onUnavailable();
        Thread.sleep(150);
        assertTrue(cb.allow());
        assertFalse(cb.allow());
        assertEquals(PaymentCircuitBreaker.State.HALF_OPEN, cb.getState());
    }

    @Test
    void halfOpenReachableClosesCircuit() throws InterruptedException {
        PaymentCircuitBreaker cb = breaker(100, 1000);
        cb.onUnavailable();
        Thread.sleep(150);
        assertTrue(cb.allow()); // probe released
        cb.onReachable();
        assertEquals(PaymentCircuitBreaker.State.CLOSED, cb.getState());
        assertTrue(cb.allow());
    }

    @Test
    void openReachableClosesCircuit() {
        // S3/S5: charge timed out (breaker OPEN) while Payment is up; the immediate
        // reconciliation answer proves reachability and must close the breaker
        // without waiting for the full open window.
        PaymentCircuitBreaker cb = breaker(5000, 1000);
        cb.onUnavailable();
        cb.onReachable();
        assertEquals(PaymentCircuitBreaker.State.CLOSED, cb.getState());
        assertTrue(cb.allow());
    }

    @Test
    void halfOpenUnavailableReopensCircuit() throws InterruptedException {
        PaymentCircuitBreaker cb = breaker(100, 1000);
        cb.onUnavailable();
        Thread.sleep(150);
        assertTrue(cb.allow()); // probe released
        cb.onUnavailable();
        assertEquals(PaymentCircuitBreaker.State.OPEN, cb.getState());
        assertFalse(cb.allow());
    }

    @Test
    void stuckProbeWatchdogReleasesNewProbe() throws InterruptedException {
        PaymentCircuitBreaker cb = breaker(50, 100);
        cb.onUnavailable();
        Thread.sleep(80);
        assertTrue(cb.allow()); // HALF_OPEN, first probe — it never reports
        Thread.sleep(130);     // probe window (100 ms) elapsed
        assertTrue(cb.allow(), "watchdog must release a fresh probe");
        assertFalse(cb.allow(), "at most one probe in flight");
    }

    @Test
    void halfOpenReportedAfterWindowStillResolves() throws InterruptedException {
        // A probe that reports late (after the watchdog refreshed HALF_OPEN) still
        // resolves the state: its onUnavailable re-opens, its onReachable closes.
        PaymentCircuitBreaker cb = breaker(50, 100);
        cb.onUnavailable();
        Thread.sleep(80);
        assertTrue(cb.allow());
        Thread.sleep(130);
        assertTrue(cb.allow()); // watchdog probe
        cb.onUnavailable(); // late outcome of the first probe arrives
        assertEquals(PaymentCircuitBreaker.State.OPEN, cb.getState());
    }
}
