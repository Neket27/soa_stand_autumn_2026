package orders.api;

import orders.api.dto.Order;
import orders.store.OrderRepository;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

class OrderQueryServiceTest {

    private final OrderRepository orderRepository = mock(OrderRepository.class);
    private final OrderQueryService service = new OrderQueryService(orderRepository);

    @Test
    void getOrder_found_returnsMappedDto() {
        when(orderRepository.findById("order-1")).thenReturn(
                new OrderRepository.Order("order-1", "u-1", "paid", 99800, "[]"));

        Order order = service.getOrder("order-1");

        assertEquals("order-1", order.id());
        assertEquals("paid", order.status());
        assertEquals(99800, order.amountCents());
    }

    @Test
    void getOrder_notFound_throwsOrderNotFoundException() {
        when(orderRepository.findById("ghost")).thenReturn(null);

        OrderNotFoundException ex = assertThrows(OrderNotFoundException.class,
                () -> service.getOrder("ghost"));

        assertEquals("ghost", ex.getOrderId());
    }
}
