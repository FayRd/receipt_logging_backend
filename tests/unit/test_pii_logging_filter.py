# File: receipt_logging_backend/tests/unit/test_pii_logging_filter.py
import logging
import time
import pytest
from src.Infrastructure.logger import (
    PIIFilter,
    sanitize_text,
    sanitize_object,
    mask_email,
    mask_phone,
    mask_identifier,
    hash_audit_id,
    setup_logging,
)
from src.config import Settings, get_settings


class TestPIILoggingFilter:
    """Verifies that all sensitive PII and authentication credentials are scrubbed from log streams."""

    @pytest.fixture(autouse=True)
    def setup_test_logger(self):
        self.logger = logging.getLogger("test_pii_logger")
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers.clear()
        self.logger.filters.clear()

        self.log_stream = []

        class MockHandler(logging.Handler):
            def __init__(handler_self):
                super().__init__()

            def emit(handler_self, record):
                handler_self.format(record)
                self.log_stream.append(record.getMessage())

        handler = MockHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.addFilter(PIIFilter())
        self.logger.addHandler(handler)
        self.logger.addFilter(PIIFilter())

    def test_email_redaction_in_message(self):
        self.logger.info("User registered with email alice.smith+dev@test-domain.co.uk successfully.")
        assert len(self.log_stream) == 1
        assert "alice.smith+dev@test-domain.co.uk" not in self.log_stream[0]
        assert "***REDACTED_EMAIL***" in self.log_stream[0]

    def test_jwt_bearer_redaction(self):
        jwt_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        self.logger.info("Authorization header received: Bearer %s", jwt_token)
        assert len(self.log_stream) == 1
        assert jwt_token not in self.log_stream[0]
        assert "Bearer ***REDACTED_JWT***" in self.log_stream[0]

    def test_password_and_secret_redaction(self):
        self.logger.debug("Failed payload: {'username': 'johndoe', 'password': 'SuperSecretPassword123!'}")
        assert len(self.log_stream) == 1
        assert "SuperSecretPassword123!" not in self.log_stream[0]
        assert "***REDACTED***" in self.log_stream[0]

    def test_api_key_redaction(self):
        self.logger.error("Call failed using key AIzaSyD5z9A_FakeGoogleApiKey1234567890X and sk-liveFakeOpenAIKey999999999999")
        assert len(self.log_stream) == 1
        assert "AIzaSyD5z9A_FakeGoogleApiKey1234567890X" not in self.log_stream[0]
        assert "sk-liveFakeOpenAIKey999999999999" not in self.log_stream[0]
        assert "***REDACTED_API_KEY***" in self.log_stream[0]

    def test_phone_number_redaction(self):
        self.logger.info("Dispatching SMS OTP to +65 9123 4567 and +14155552671")
        assert len(self.log_stream) == 1
        assert "9123 4567" not in self.log_stream[0]
        assert "+14155552671" not in self.log_stream[0]
        assert "***REDACTED_PHONE***" in self.log_stream[0]

    def test_credit_card_redaction(self):
        self.logger.warning("Payment failed for PAN: 4532-1234-5678-9012")
        assert len(self.log_stream) == 1
        assert "4532-1234-5678-9012" not in self.log_stream[0]
        assert "***REDACTED_CARD***" in self.log_stream[0]

    def test_recursive_extra_dictionary_sanitizer(self):
        extra_data = {
            "user_data": {
                "email": "victim@example.com",
                "nested": {
                    "password": "ClearTextPassword",
                    "auth_header": "Bearer eyJhbGciOi...",
                }
            },
            "system": "auth_service"
        }
        sanitized = sanitize_object(extra_data)
        assert sanitized["user_data"]["email"] == "***REDACTED_EMAIL***"
        assert sanitized["user_data"]["nested"]["password"] == "***REDACTED***"
        assert sanitized["user_data"]["nested"]["auth_header"] == "***REDACTED***"
        assert sanitized["system"] == "auth_service"

    def test_circular_reference_handling(self):
        cyclic_dict = {"name": "test"}
        cyclic_dict["self"] = cyclic_dict
        sanitized = sanitize_object(cyclic_dict)
        assert sanitized["self"] == "[CIRCULAR_REFERENCE]"

    def test_audit_masking_helpers(self):
        assert mask_email("alex.miller@domain.com") == "a***r@domain.com"
        assert mask_email("ab@domain.com") == "a***@domain.com"
        assert mask_phone("+6591234567") == "+65 **** 4567"
        assert mask_identifier("alex.miller@domain.com") == "a***r@domain.com"
        assert mask_identifier("+6591234567") == "+65 **** 4567"
        assert len(hash_audit_id("test@example.com")) == 16

    def test_immutability_of_input_object(self):
        original = {
            "nested": {
                "email": "user@example.com",
                "password": "my_password"
            }
        }
        sanitized = sanitize_object(original)
        assert original["nested"]["email"] == "user@example.com"
        assert original["nested"]["password"] == "my_password"
        assert sanitized["nested"]["email"] == "***REDACTED_EMAIL***"
        assert sanitized["nested"]["password"] == "***REDACTED***"

    def test_record_args_cleared_after_formatting(self):
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="User email is %s",
            args=("alice@example.com",),
            exc_info=None,
        )
        f = PIIFilter()
        f.filter(record)
        assert record.args == ()
        assert "***REDACTED_EMAIL***" in record.msg

    def test_record_extra_attributes_sanitized(self):
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="User action performed",
            args=(),
            exc_info=None,
        )
        record.client_payload = {"email": "secret@domain.com"}
        f = PIIFilter()
        f.filter(record)
        assert record.client_payload["email"] == "***REDACTED_EMAIL***"

    def test_unprefixed_local_phone_with_heuristic_keywords(self):
        """Verifies supervisor finding ADV-31-01: 'phone', 'mobile', 'tel', and 'sms' trigger fast-path."""
        self.logger.info("SMS dispatched to 91234567")
        assert len(self.log_stream) == 1
        assert "91234567" not in self.log_stream[0]
        assert "***REDACTED_PHONE***" in self.log_stream[0]

        self.logger.info("Customer mobile is 81234567")
        assert len(self.log_stream) == 2
        assert "81234567" not in self.log_stream[1]
        assert "***REDACTED_PHONE***" in self.log_stream[1]

        self.logger.info("Contact tel 98765432")
        assert len(self.log_stream) == 3
        assert "98765432" not in self.log_stream[2]
        assert "***REDACTED_PHONE***" in self.log_stream[2]

        self.logger.info("User phone: 87654321")
        assert len(self.log_stream) == 4
        assert "87654321" not in self.log_stream[3]
        assert "***REDACTED_PHONE***" in self.log_stream[3]

    def test_defensive_filter_exception_handling(self):
        """Filter never crashes the application if an unexpected error occurs during filtering."""
        class BrokenRepr:
            def __repr__(self):
                raise RuntimeError("Boom!")

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg=BrokenRepr(),
            args=(),
            exc_info=None,
        )
        f = PIIFilter()
        result = f.filter(record)
        assert result is True
        assert "[LOG REDACTION EXCEPTION:" in str(record.msg)


class TestLogLevelHardening:
    """Verifies that log levels are hardened against DEBUG in production and staging."""

    def test_production_demotes_debug_to_info(self):
        settings = Settings(environment="production", log_level="DEBUG")
        assert settings.log_level == "INFO"

    def test_staging_demotes_debug_to_info(self):
        settings = Settings(environment="staging", log_level="DEBUG")
        assert settings.log_level == "INFO"

    def test_development_allows_debug(self):
        settings = Settings(environment="development", log_level="DEBUG")
        assert settings.log_level == "DEBUG"

    def test_setup_logging_runtime_clamp_in_production(self, monkeypatch):
        test_settings = Settings(environment="production", log_level="DEBUG")
        monkeypatch.setattr("src.Infrastructure.logger.get_settings", lambda: test_settings)
        setup_logging()
        root_logger = logging.getLogger("receipt_api")
        assert root_logger.level == logging.INFO


def test_pii_filter_latency_overhead():
    """Benchmark validating that average redaction overhead is strictly under 0.05ms (50µs)."""
    sample_text = (
        "2026-11-11 12:00:00 [INFO] [req-abc12345] [receipt_api.HTTP] "
        "User registration initiated for email=test.user@domain.com with phone=+6591234567 "
        "and client_ip=192.168.1.100 status=200 duration=45.2ms"
    )
    iterations = 10_000
    start = time.perf_counter()
    for _ in range(iterations):
        sanitize_text(sample_text)
    total_time = time.perf_counter() - start
    avg_overhead_ms = (total_time / iterations) * 1000.0

    print(f"\n[BENCHMARK] Total: {total_time:.4f}s for {iterations} iterations ({avg_overhead_ms:.4f}ms/op)")
    # Target: Average overhead per log record must be < 0.05ms (50 microseconds)
    assert avg_overhead_ms < 0.05, f"Overhead {avg_overhead_ms:.4f}ms exceeds 0.05ms ceiling"
