package orders.api;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import lombok.extern.slf4j.Slf4j;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import java.io.IOException;

/**
 * Диагностический фильтр: фиксирует КАЖДЫЙ запрос, обработанный дольше
 * порога. Зачем: стресс-тест на 1000 заказов/с даёт хвост латентности
 * на КЛИЕНТЕ (раннер видит ответы дольше 1000 мс при медиане 0.00 с),
 * и нужно разделить «сервер действительно обрабатывал долго» и
 * «задержка до/после сервлета: accept-queue, сеть, клиент».
 *
 * Сравнение с клиентской латентностью:
 *  - фильтр пишет SLOW → время ушло В обработке сервлета (по этапам
 *    см. лог CreateOrderService: reserve vs БД);
 *  - фильтр НЕ пишет, а клиент видит > 1 с → задержка вне сервлета
 *    (accept-queue Tomcat, сеть, сам раннер).
 *
 * Порог 250 мс: бюджет 1000 мс, а запас нужен, чтобы поймать и
 * «почти попадания» (~0.3–0.9 с) — по их разбросу видно, растёт ли
 * очередь (системный голод) или это дискретные ~1 с (таймер/GC).
 */
@Slf4j
@Component
@Order(0)
public class SlowRequestFilter extends OncePerRequestFilter {

    /** Порог в миллисекундах (см. javadoc). */
    private static final long THRESHOLD_MS = 250;

    @Override
    protected void doFilterInternal(HttpServletRequest request,
                                    HttpServletResponse response,
                                    FilterChain filterChain)
            throws ServletException, IOException {
        long start = System.nanoTime();
        try {
            filterChain.doFilter(request, response);
        } finally {
            long ms = (System.nanoTime() - start) / 1_000_000;
            if (ms >= THRESHOLD_MS) {
                log.warn("SLOW {} {} → {} за {} мс",
                        request.getMethod(),
                        request.getRequestURI(),
                        response.getStatus(),
                        ms);
            }
        }
    }
}
