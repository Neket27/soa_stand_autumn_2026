package orders.api.dto;

/** Ответ GET /users/{id}/bonus (contracts/orders.yaml, схема Bonus). */
public record BonusBalance(long points) {
}
