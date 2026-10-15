package orders.api;

import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.validation.FieldError;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.util.Map;
import java.util.stream.Collectors;

/**
 * Глобальный обработчик исключений.
 *
 * === ЧТО СДЕЛАТЬ САМОМУ (S1-S2) ===
 *
 * S1: При отклонении банком (402) заказ должен вернуть 201 + rejected.
 *     — Если здесь выбрасывается исключение → будет 500, и S1 упадёт.
 *
 * S2: При 500 от Payment заказ должен вернуть 201 + pending (или paid).
 *     — Если здесь выбрасывается исключение → будет 500, и S2 упадёт.
 */
@RestControllerAdvice
public class GlobalExceptionHandler {

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<Map<String, String>> handleBadRequest(IllegalArgumentException e) {
        return ResponseEntity.badRequest()
                .body(Map.of("error", "bad_request", "message", e.getMessage()));
    }

    // Нарушения Bean Validation на DTO (@NotBlank, @Min, @NotEmpty и т.д.).
    // Формат ответа совпадает с IllegalArgumentException: 400 {"error":"bad_request",...}
    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<Map<String, String>> handleValidation(MethodArgumentNotValidException e) {
        String message = e.getBindingResult().getFieldErrors().stream()
                .map(this::formatFieldError)
                .collect(Collectors.joining("; "));
        return ResponseEntity.badRequest()
                .body(Map.of("error", "bad_request", "message", message));
    }

    private String formatFieldError(FieldError fe) {
        return fe.getField() + ": " + fe.getDefaultMessage();
    }

    // Доменное исключение из OrderQueryService → HTTP 404.
    // Формат ответа тот же, что и раньше: {"error":"not_found", ...}
    @ExceptionHandler(OrderNotFoundException.class)
    public ResponseEntity<Map<String, String>> handleNotFound(OrderNotFoundException e) {
        return ResponseEntity.status(HttpStatus.NOT_FOUND)
                .body(Map.of("error", "not_found", "message", e.getMessage()));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<Map<String, String>> handleInternal(Exception e) {
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(Map.of("error", "internal", "message", e.getMessage()));
    }
}
