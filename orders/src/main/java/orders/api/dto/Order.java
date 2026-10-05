package orders.api.dto;

public record Order(String id, String status, long amount_cents) {
}
