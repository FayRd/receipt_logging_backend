"""Module for verifying Google OAuth ID Tokens."""

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from src.Infrastructure.logger import get_logger

logger = get_logger("Auth.google_auth")


def verify_google_id_token(token_str: str, client_id: str | None = None) -> dict:
    """Verify Google ID token and return claims dictionary.

    Validates signature, expiration, issuer, audience, and email_verified.
    Raises ValueError if token is invalid or unverified.
    """
    try:
        request = google_requests.Request()
        # If client_id is set and non-empty, pass it to audience verification
        audience = client_id.strip() if client_id and client_id.strip() else None
        claims = id_token.verify_oauth2_token(
            token_str,
            request,
            audience=audience,
        )

        # OWASP Hardening: Verify Issuer
        issuer = claims.get("iss")
        if issuer not in ["accounts.google.com", "https://accounts.google.com"]:
            logger.warning("Google token rejected: invalid issuer '%s'", issuer)
            raise ValueError(f"Invalid token issuer: {issuer}")

        # OWASP Hardening: Verify email_verified claim
        if not claims.get("email_verified", False):
            logger.warning("Google token rejected: email is not verified for sub=%s", claims.get("sub"))
            raise ValueError("Google account email is not verified.")

        return claims
    except Exception as e:
        logger.warning("Google token verification failed: %s", e)
        raise ValueError(f"Invalid Google ID token: {e}") from e
