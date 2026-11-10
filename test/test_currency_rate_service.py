import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from src.Services.currency_rate_service import (
    fetch_rate_for_date,
    parse_receipt_date,
    SUPPORTED_CURRENCIES,
)
from src.Models.schemas import Receipt
from src.Auth.identity import Identity
from src.Models.Receipts.receipt_repository import ReceiptRepository


def test_parse_receipt_date():
    assert parse_receipt_date(None) is None
    assert parse_receipt_date("") is None
    assert parse_receipt_date("invalid") is None

    d = date(2024, 3, 15)
    assert parse_receipt_date(d) == d

    dt = datetime(2024, 3, 15, 12, 30, 0, tzinfo=timezone.utc)
    assert parse_receipt_date(dt) == d

    assert parse_receipt_date("2024-03-15") == d
    assert parse_receipt_date("2024-03-15T10:30:00Z") == d
    assert parse_receipt_date("2024-03-15T10:30:00+08:00") == d


def test_fetch_rate_sgd_short_circuit():
    rate = asyncio.run(fetch_rate_for_date("SGD", date(2024, 3, 15)))
    assert rate == 1.0

    rate_lower = asyncio.run(fetch_rate_for_date("sgd", date(2024, 3, 15)))
    assert rate_lower == 1.0


def test_fetch_rate_invalid_and_unsupported_currency():
    assert asyncio.run(fetch_rate_for_date(None, date(2024, 3, 15))) is None
    assert asyncio.run(fetch_rate_for_date("", date(2024, 3, 15))) is None
    assert asyncio.run(fetch_rate_for_date("TOOLONG", date(2024, 3, 15))) is None
    assert asyncio.run(fetch_rate_for_date("12$", date(2024, 3, 15))) is None
    assert asyncio.run(fetch_rate_for_date("ZZZ", date(2024, 3, 15))) is None


def test_fetch_rate_success_mock():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "amount": 1.0,
        "base": "MYR",
        "date": "2024-03-15",
        "rates": {"SGD": 0.28419},
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        rate = asyncio.run(fetch_rate_for_date("MYR", date(2024, 3, 15)))
        assert rate == 0.28419
        mock_get.assert_called_once()
        assert "2024-03-15?from=MYR&to=SGD" in mock_get.call_args[0][0]


def test_fetch_rate_404_fallback_to_latest_mock():
    mock_404 = MagicMock()
    mock_404.status_code = 404

    mock_latest = MagicMock()
    mock_latest.status_code = 200
    mock_latest.json.return_value = {
        "amount": 1.0,
        "base": "USD",
        "date": "2026-10-31",
        "rates": {"SGD": 1.345},
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = [mock_404, mock_latest]
        rate = asyncio.run(fetch_rate_for_date("USD", date(2026, 11, 1)))
        assert rate == 1.345
        assert mock_get.call_count == 2
        assert "latest?from=USD&to=SGD" in mock_get.call_args_list[1][0][0]


def test_fetch_rate_timeout_fails_open_mock():
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = httpx.TimeoutException("Request timed out")
        rate = asyncio.run(fetch_rate_for_date("EUR", date(2024, 3, 15)))
        assert rate is None


def test_receipt_repository_rate_enrichment_on_create():
    mock_db = MagicMock()
    repo = ReceiptRepository(mock_db)

    receipt = Receipt(
        merchant_name="Test Store",
        total_amount=100.0,
        currency="MYR",
        date="2024-03-15T12:00:00Z",
    )
    identity = Identity(device_id="dev-123", user_id=None)

    mock_insert = MagicMock()
    mock_insert.execute = AsyncMock(return_value=MagicMock(data=[{
        "id": "rec-1",
        "user_id": None,
        "device_id": "dev-123",
        "receipt": {"merchant_name": "Test Store", "total_amount": 100.0, "currency": "MYR", "currency_rate_at_receipt_date": 0.28419},
        "enc_version": 0,
    }]))
    mock_db.table.return_value.insert.return_value = mock_insert

    with patch("src.Models.Receipts.receipt_repository.fetch_rate_for_date", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = 0.28419
        result = asyncio.run(repo.create(identity, receipt))
        assert receipt.currency_rate_at_receipt_date == 0.28419
        mock_fetch.assert_called_once_with("MYR", receipt.date)
