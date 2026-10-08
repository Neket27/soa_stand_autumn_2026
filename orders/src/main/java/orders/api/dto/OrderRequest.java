package orders.api.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;

import java.util.List;

public record OrderRequest(
        @NotBlank @JsonProperty("user_id") String user_id,
        @NotEmpty @JsonProperty("items") List<Item> items) {
}
