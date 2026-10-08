package orders.api;

import jakarta.servlet.http.HttpServletRequest;
import orders.api.dto.Error;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataAccessException;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
public class GlobalExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(GlobalExceptionHandler.class);

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<Error> handleValidation(MethodArgumentNotValidException ex) {
        return ResponseEntity.badRequest().body(
                new Error("validation_error", "invalid request body"));
    }

    @ExceptionHandler(OrderNotFoundException.class)
    public ResponseEntity<Error> handleNotFound(OrderNotFoundException ex) {
        log.info("order not found: {}", ex.getOrderId());
        return ResponseEntity.status(404).body(
                new Error("not_found", "order not found"));
    }

    @ExceptionHandler(DataAccessException.class)
    public ResponseEntity<Error> handleDatabase(DataAccessException ex) {
        log.warn("database error: {}", ex.getMessage());
        return ResponseEntity.status(500).body(
                new Error("db_error", "database error"));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<Error> handleUnhandled(Exception ex, HttpServletRequest request) {
        log.warn("unhandled exception on {} {}: {}",
                request.getMethod(), request.getRequestURI(), ex.getMessage(), ex);
        return ResponseEntity.status(500).body(
                new Error("internal", "internal server error"));
    }
}
