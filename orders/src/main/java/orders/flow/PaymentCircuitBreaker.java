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
                // Watchdog: выпущенный проб так и не сообщил исход (мертвый воркер).
                // Живой проб сообщает за ~3 с (charge + retry + reconcile), поэтому
                // выпускаем новый проб сейчас; одновременно в полёте не более одного проба.
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
     * Payment ответил HTTP-ответом (любым статусом). Гипотеза о недоступности
     * опровергнута, поэтому брейкер закрывается даже из OPEN: например, в S3/S5
     * списание тайм-аутится, пока Payment работает, и немедленный reconciliation-GET
     * это подтверждает. В реальном blackhole ответить невозможно (мок висит),
     * поэтому бюджет отказа не затрагивается.
     */
    public synchronized void onReachable() {
        if (state != State.CLOSED) {
            state = State.CLOSED;
        }
    }

    /**
     * От Payment нет ответа (тайм-аут / ошибка соединения). No-op, пока уже
     * OPEN: поздние тайм-ауты списаний, которые были в полёте, когда брейкер
     * открылся, не должны перезапускать окно открытого состояния.
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
