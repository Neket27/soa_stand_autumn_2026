package orders.api;

import orders.api.dto.Error;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
public class ValidationExceptionHandler {

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<Error> handleValidation(MethodArgumentNotValidException ex) {
        return ResponseEntity.badRequest().body(
                new Error("validation_error", "invalid request body"));
    }
}
