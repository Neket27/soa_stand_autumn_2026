package orders.flow;

public class PaymentCircuitBreaker {

    public enum State { CLOSED, OPEN, HALF_OPEN }

    private State state = State.CLOSED;
    private long openSince;
    private final long openForMs;

    public PaymentCircuitBreaker(long openForMs) {
        this.openForMs = openForMs;
    }

    public synchronized boolean allow() {
        switch (state) {
            case CLOSED:
                return true;
            case OPEN:
                if (System.currentTimeMillis() - openSince >= openForMs) {
                    state = State.HALF_OPEN;
                    return true;
                }
                return false;
            case HALF_OPEN:
                // Only the first probe is allowed (from OPEN transition above)
                return false;
            default:
                return false;
        }
    }

    public synchronized void onSuccess() {
        if (state == State.HALF_OPEN) {
            state = State.CLOSED;
        }
    }

    public synchronized void onTimeoutOrConnError() {
        if (state == State.HALF_OPEN) {
            state = State.OPEN;
            openSince = System.currentTimeMillis();
        } else if (state == State.CLOSED) {
            state = State.OPEN;
            openSince = System.currentTimeMillis();
        }
    }

    public synchronized State getState() {
        if (state == State.OPEN && System.currentTimeMillis() - openSince >= openForMs) {
            return State.HALF_OPEN;
        }
        return state;
    }
}
