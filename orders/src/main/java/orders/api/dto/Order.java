package orders.api.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

public record Order(String id, String status,
                    @JsonProperty("amount_cents") long amountCents) {
}
