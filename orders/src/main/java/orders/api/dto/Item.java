package orders.api.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

public record Item(
        @JsonProperty("sku") String sku,
        @JsonProperty("qty") int qty,
        @JsonProperty("price_cents") long priceCents) {
}
