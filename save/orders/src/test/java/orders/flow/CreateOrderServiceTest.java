package orders.flow;

import orders.api.dto.Item;
import orders.api.dto.OrderRequest;
import orders.inventory.InventoryClient;
import orders.store.OrderRepository;
import org.junit.jupiter.api.Test;

import java.math.BigDecimal;
import java.time.Duration;
import java.util.List;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

class CreateOrderServiceTest {

    private final OrderRepository orderRepository = mock(OrderRepository.class);
    private final InventoryClient inventoryClient = mock(InventoryClient.class);
    private final InMemoryChargeDispatcher backgroundWorker = mock(InMemoryChargeDispatcher.class);
    private final CreateOrderService service =
            new CreateOrderService(orderRepository, inventoryClient, backgroundWorker, 900);

    @Test
    void createOrder_reserveSucceeds_returnsPendingAndEnqueues() {
        OrderRequest request = new OrderRequest("u-1", List.of(new Item("sku-1", 2, new BigDecimal("49900"))));
        when(orderRepository.insertPending(eq("u-1"), anyList(), eq(new BigDecimal("99800")))).thenReturn("order-1");
        when(inventoryClient.reserve(eq("order-1"), anyList(), eq(Duration.ofMillis(890)))).thenReturn(true);

        CreateOrderService.CreateResult result = service.createOrder(request);

        assertEquals("order-1", result.orderId());
        assertEquals(OrderStatus.PENDING, result.status());
        verify(backgroundWorker).enqueue("order-1");
        verify(orderRepository, never()).transition(anyString(), any(OrderStatus.class));
    }

    @Test
    void createOrder_reserveFails_returnsRejectedWithDutyAndWithoutEnqueue() {
        // L2: reserve()==false is an indeterminate outcome (timeout != "no
        // reservation"), so the rejected transition records the durable release
        // duty for the reaper sweep. No extra Inventory call on the HTTP path.
        OrderRequest request = new OrderRequest("u-1", List.of(new Item("sku-1", 1, new BigDecimal("1000"))));
        when(orderRepository.insertPending(eq("u-1"), anyList(), eq(new BigDecimal("1000")))).thenReturn("order-2");
        when(inventoryClient.reserve(eq("order-2"), anyList(), any(Duration.class))).thenReturn(false);

        CreateOrderService.CreateResult result = service.createOrder(request);

        assertEquals("order-2", result.orderId());
        assertEquals(OrderStatus.REJECTED, result.status());
        verify(orderRepository).transition("order-2", OrderStatus.REJECTED, true);
        verify(inventoryClient, never()).release(anyString(), any(Duration.class));
        verify(backgroundWorker, never()).enqueue(anyString());
    }

    @Test
    void amount_sumsQtyTimesPrice() {
        OrderRequest request = new OrderRequest("u-1", List.of(
                new Item("a", 2, new BigDecimal("49900")), new Item("b", 1, new BigDecimal("1000"))));

        assertEquals(new BigDecimal("100800"), request.amountCents());
    }

    @Test
    void reserveTimeout_isBudgetMinus10() {
        OrderRequest request = new OrderRequest("u-1", List.of(new Item("sku-1", 1, new BigDecimal("1000"))));
        when(orderRepository.insertPending(anyString(), anyList(), any(BigDecimal.class))).thenReturn("order-3");
        when(inventoryClient.reserve(eq("order-3"), anyList(), eq(Duration.ofMillis(890)))).thenReturn(true);

        service.createOrder(request);

        verify(inventoryClient).reserve(eq("order-3"), anyList(), eq(Duration.ofMillis(890)));
    }

    @Test
    void reserveTimeout_shortBudget_usesMinTimeout() {
        CreateOrderService shortService =
                new CreateOrderService(orderRepository, inventoryClient, backgroundWorker, 50);
        OrderRequest request = new OrderRequest("u-1", List.of(new Item("sku-1", 1, new BigDecimal("1000"))));
        when(orderRepository.insertPending(anyString(), anyList(), any(BigDecimal.class))).thenReturn("order-4");
        when(inventoryClient.reserve(eq("order-4"), anyList(), eq(Duration.ofMillis(100)))).thenReturn(true);

        shortService.createOrder(request);

        verify(inventoryClient).reserve(eq("order-4"), anyList(), eq(Duration.ofMillis(100)));
    }
}
