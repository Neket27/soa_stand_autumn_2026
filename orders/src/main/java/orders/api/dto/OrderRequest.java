package orders.api.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

public record OrderRequest(
        @JsonProperty("user_id") String user_id,
        @JsonProperty("items") List<Item> items) {
}
