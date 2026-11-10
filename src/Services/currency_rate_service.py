import re
from datetime import date, datetime
import httpx
from src.Infrastructure.logger import get_logger

logger = get_logger("Services.currency_rate_service")


def parse_receipt_date(raw_date: object) -> date | None:
    """Parse raw date (str, datetime, or date) into a date object."""
    if raw_date is None:
        return None
    if isinstance(raw_date, datetime):
        return raw_date.date()
    if isinstance(raw_date, date):
        return raw_date
    if isinstance(raw_date, str):
        raw_str = raw_date.strip()
        if not raw_str:
            return None
        try:
            cleaned = raw_str.replace("Z", "+00:00")
            return datetime.fromisoformat(cleaned).date()
        except Exception:
            if len(raw_str) >= 10:
                try:
                    return date.fromisoformat(raw_str[:10])
                except Exception:
                    pass
    return None

# Currencies published by European Central Bank / Frankfurter API
SUPPORTED_CURRENCIES: set[str] = {
    "AUD", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "EUR", "GBP", "HKD",
    "HUF", "IDR", "ILS", "INR", "ISK", "JPY", "KRW", "MXN", "MYR", "NOK",
    "NZD", "PHP", "PLN", "RON", "SEK", "SGD", "THB", "TRY", "USD", "ZAR",
}

FRANKFURTER_BASE_URL = "https://api.frankfurter.dev/v1"
_CURRENCY_CODE_REGEX = re.compile(r"^[A-Z]{3}$")


async def fetch_rate_for_date(
    currency_code: str | None,
    receipt_date: date | datetime | str | None,
    timeout_seconds: float = 3.0,
) -> float | None:
    """Fetch 1 unit of `currency_code` expressed in SGD on `receipt_date`.

    Returns:
        float: 1 unit of receipt currency = rate SGD (e.g. 1 MYR = 0.315 SGD).
        None: On any error, timeout, or unsupported currency.

    OWASP Controls:
        A03: Strict ISO 4217 uppercase 3-char format check and allowlist validation.
        A04: 3.0-second timeout, strict TLS verification, fail-open (returns None so receipt write succeeds).
    """
    if not currency_code or not isinstance(currency_code, str):
        return None

    code = currency_code.strip().upper()
    if not _CURRENCY_CODE_REGEX.match(code):
        logger.warning("Invalid currency code format: %r", currency_code)
        return None

    if code == "SGD":
        return 1.0

    if code not in SUPPORTED_CURRENCIES:
        logger.debug("Currency %s not supported by Frankfurter, skipping historical lookup", code)
        return None

    parsed_date = parse_receipt_date(receipt_date)
    date_str = parsed_date.isoformat() if parsed_date else "latest"

    url = f"{FRANKFURTER_BASE_URL}/{date_str}?from={code}&to=SGD"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True, verify=True) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                rate = data.get("rates", {}).get("SGD")
                if isinstance(rate, (int, float)):
                    return float(rate)
                return None

            if response.status_code == 404 and date_str != "latest":
                # Rate for date not published yet (e.g. today before ECB publication). Fallback to latest.
                latest_url = f"{FRANKFURTER_BASE_URL}/latest?from={code}&to=SGD"
                latest_resp = await client.get(latest_url)
                if latest_resp.status_code == 200:
                    data = latest_resp.json()
                    rate = data.get("rates", {}).get("SGD")
                    if isinstance(rate, (int, float)):
                        return float(rate)

            logger.warning(
                "Frankfurter returned status %d for %s on %s",
                response.status_code,
                code,
                date_str,
            )
            return None
    except Exception as e:
        logger.warning(
            "Failed to fetch currency rate for %s on %s from Frankfurter: %s",
            code,
            date_str,
            e,
        )
        return None
