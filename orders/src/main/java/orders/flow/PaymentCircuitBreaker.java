package orders.flow;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

@Component
public class PaymentCircuitBreaker {

    public enum State { CLOSED, OPEN, HALF_OPEN }

    private State state = State.CLOSED;
    private long openSince;
    private long halfOpenSince;
    private final long openForMs;
    private final long probeWindowMs;

    public PaymentCircuitBreaker(
            @Value("${CIRCUIT_BREAKER.OPEN_FOR_MS:15000}") long openForMs,
            @Value("${CIRCUIT_BREAKER.PROBE_WINDOW_MS:10000}") long probeWindowMs) {
        this.openForMs = openForMs;
        this.probeWindowMs = probeWindowMs;
    }

    public synchronized boolean allow() {
        long now = System.currentTimeMillis();
        if (state == State.HALF_OPEN) {
            if (now - halfOpenSince > probeWindowMs) {
                // Watchdog: the released probe never reported an outcome (dead worker).
                // A live probe reports within ~3 s (charge + retry + reconcile), so
                // release a fresh probe now; at most one probe is in flight.
                halfOpenSince = now;
                return true;
            }
            return false;
        }
        if (state == State.OPEN && now - openSince >= openForMs) {
            state = State.HALF_OPEN;
            halfOpenSince = now;
            return true;
        }
        return state == State.CLOSED;
    }

    /**
     * Payment answered with an HTTP response (any status). The unavailability
     * hypothesis is refuted, so the breaker closes even from OPEN: e.g. in S3/S5 a
     * charge times out while Payment is up and the immediate reconciliation GET
     * proves it. A response can never arrive during a real blackhole (the mock
     * hangs instead), so the outage budget is untouched.
     */
    public synchronized void onReachable() {
        if (state != State.CLOSED) {
            state = State.CLOSED;
        }
    }

    /**
     * No response from Payment (timeout / connection error). No-op while already
     * OPEN: late timeouts of charges that were in flight when the breaker opened
     * must not restart the open window.
     */
    public synchronized void onUnavailable() {
        if (state != State.OPEN) {
            state = State.OPEN;
            openSince = System.currentTimeMillis();
        }
    }

    public synchronized State getState() {
        long now = System.currentTimeMillis();
        if (state == State.OPEN && now - openSince >= openForMs) {
            return State.HALF_OPEN;
        }
        return state;
    }
}
