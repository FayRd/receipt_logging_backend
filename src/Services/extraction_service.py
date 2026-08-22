import asyncio
import base64
import json
import random
import httpx
from google import genai
from google.genai import types
from src.Infrastructure.logger import get_logger
from src.Models.schemas import Receipt, ScanContext
from src.config import get_settings

logger = get_logger("Services.extraction_service")

FRIENDLY_ERROR_MESSAGE = "Oops, something went wrong! Please try again later."


class ProviderOverloadedError(Exception):
    """Raised when the AI model provider returns 429, 500, 503, rate limit, quota, or high demand.

    This is common for both Google GenAI and OpenRouter-routed models when they experience
    high traffic, token quota exhaustion, or transient infrastructure errors.
    """

    def __init__(self, message: str = FRIENDLY_ERROR_MESSAGE, original_error: Exception | None = None):
        super().__init__(message)
        self.original_error = original_error


def is_provider_overload_error(e: Exception) -> bool:
    """Determine if an exception represents a 429/500/503/quota/high demand error.

    Applies to both Google GenAI SDK errors and OpenRouter HTTP errors, since both
    can signal the same classes of transient infrastructure and rate-limiting failures.
    """
    if isinstance(e, ProviderOverloadedError):
        return True

    err_str = str(e).lower()
    error_indicators = [
        "429",
        "500",
        "502",
        "503",
        "504",
        "resource_exhausted",
        "resourceexhausted",
        "unavailable",
        "internal error",
        "token usage limit",
        "model experiencing high demand",
        "high demand",
        "rate limit",
        "quota exceeded",
        "too many requests",
        "overloaded",
    ]
    return any(ind in err_str for ind in error_indicators)


SYSTEM_PROMPT = """
You are a specialized receipt and financial statement data extraction engine.
Your primary task is to identify, validate, and extract structured data from images of receipts, retail slips, invoices, bills, and financial statements.

Document Validation & Confidence Scoring Rules:
1. Document Verification & Confidence Score (Float 0.0 to 1.0):
   - Assign 0.8 to 1.0 if the image is a valid, legible receipt, retail slip, invoice, bill, bank statement, or financial document with clear merchant/institution header, line items, transaction date, or financial total.
   - Assign 0.0 to 0.79 if the image is NOT a financial receipt/statement (e.g. photos, landscapes, animals, memes, non-financial documents), if the image is completely illegible/corrupted, or if it is a random snippet lacking financial totals and merchant context.
2. merchant_name: Name of the merchant/store/institution. If missing, illegible, or the image is not a receipt, use "N/A" (do NOT output null).
3. total_amount: Transaction total number. If not found or the image is not a receipt, use 0.0 (do NOT output null).
4. date: Format as ISO 8601 (e.g. 2026-03-30T14:30:00Z). Use today's date if not visible.
5. raw_text: Transcribe every visible character from top to bottom.
6. category: Infer from context. Must be one of: Dining, Groceries, Transport, Utilities, Shopping, Entertainment, Health, Supplies, Other.
7. currency: ISO 4217 code (e.g. USD, SGD, MYR, EUR, GBP). Default to USD if unclear.
8. line_items: Extract all purchased products, items, and services. Also extract:
   - Surcharges (e.g. Service Charge, GST/VAT/Tax, Delivery Fee, Tips, Surcharge) as separate line items with positive unit_price and total_price values.
   - Discounts (e.g. Vouchers, Coupons, Member Discounts, Promo Codes, Special Reductions, Trade-ins, Deductions) as separate line items with NEGATIVE unit_price and total_price values (e.g. -2.50).
9. Set missing optional fields (subtotal, tax_amount, notes, line_items, category) to null.
10. Output ONLY valid JSON matching the schema. No prose, no markdown wrappers.
""".strip()

# JSON schema description embedded in the OpenRouter system prompt so models that do not support
# structured output via a schema parameter can still produce a validated, structured Receipt object.
OPENROUTER_JSON_SCHEMA_HINT = """
Output JSON must match this exact schema (all field names are snake_case):
{
  "merchant_name": string (use "N/A" if unknown or not a receipt, do NOT use null),
  "line_items": [ { "description": string, "quantity": number|null, "unit_price": number|null, "total_price": number|null } ] | null,
  "subtotal": number|null,
  "tax_amount": number|null,
  "total_amount": number (use 0.0 if not found, do NOT use null),
  "currency": string,
  "category": string|null,
  "date": string (ISO 8601),
  "raw_text": string,
  "confidence_score": number (0.0-1.0),
  "notes": string|null
}
"""


class ExtractionService:
    """AI-powered receipt extraction service supporting Google GenAI (Gemini) and OpenRouter.

    The active provider is determined by ``settings.effective_ai_provider``.
    Both providers share the same validation rules, confidence threshold enforcement,
    retry backoff with randomized jitter (up to MAX_RETRIES), and the same SYSTEM_PROMPT
    to ensure consistent extraction quality regardless of which backend is active.

    Activate via .env:
        AI_PROVIDER=gemini        # (default) Uses GEMINI_API_KEY + GEMINI_VISION_MODEL
        AI_PROVIDER=openrouter    # Uses OPENROUTER_API_KEY + OPENROUTER_VISION_MODEL
    """

    MAX_RETRIES = 3
    BASE_DELAY_SECONDS = 1.0

    def __init__(self):
        self.settings = get_settings()
        provider = self.settings.effective_ai_provider

        # Initialise Google GenAI client only when Gemini is the active provider
        self._gemini_client: genai.Client | None = None
        if provider == "gemini":
            self._gemini_client = genai.Client(api_key=self.settings.gemini_api_key)
            logger.info(
                "ExtractionService initialised with Google GenAI provider (model=%s)",
                self.settings.gemini_vision_model,
            )

        # Initialise shared async httpx client for OpenRouter REST calls
        self._http_client: httpx.AsyncClient | None = None
        if provider == "openrouter":
            self._http_client = httpx.AsyncClient(
                base_url=self.settings.openrouter_base_url,
                headers={
                    "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=60.0,
            )
            logger.info(
                "ExtractionService initialised with OpenRouter provider (model=%s, base_url=%s)",
                self.settings.openrouter_vision_model,
                self.settings.openrouter_base_url,
            )

    async def _extract_gemini(self, context: ScanContext) -> Receipt:
        """Send the image to Google GenAI (Gemini) Vision and return a structured Receipt.

        Uses the Gemini SDK's native response_schema enforcement to ensure JSON conformance.
        """
        response = await self._gemini_client.aio.models.generate_content(
            model=self.settings.gemini_vision_model,
            contents=[
                types.Part.from_bytes(
                    data=context.image_bytes,
                    mime_type=context.content_type,
                ),
                types.Part.from_text(text=SYSTEM_PROMPT),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=Receipt,
            ),
        )
        text = response.text or ""
        usage = getattr(response, "usage_metadata", None)
        logger.info(
            "Gemini vision response received. Output text len=%d, usage_metadata=%s",
            len(text),
            usage,
        )
        return text

    async def _extract_openrouter(self, context: ScanContext) -> str:
        """Send the image to OpenRouter via the OpenAI-compatible chat completions API.

        Images are encoded as base64 data URLs per the OpenAI vision message format.
        ``response_format={"type": "json_object"}`` is set for broad model compatibility.
        Returns the raw JSON string for downstream validation.
        """
        # Encode image bytes to base64 data URL (OpenAI vision format)
        encoded = base64.b64encode(context.image_bytes).decode("utf-8")
        data_url = f"data:{context.content_type};base64,{encoded}"

        system_content = f"{SYSTEM_PROMPT}\n\n{OPENROUTER_JSON_SCHEMA_HINT}"

        payload = {
            "model": self.settings.openrouter_vision_model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_content},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                        {
                            "type": "text",
                            "text": "Extract receipt data from the image and return valid JSON only.",
                        },
                    ],
                },
            ],
        }

        resp = await self._http_client.post("/chat/completions", content=json.dumps(payload))
        resp.raise_for_status()
        result = resp.json()
        text = result["choices"][0]["message"]["content"]
        logger.info(
            "OpenRouter vision response received. Output text len=%d, model=%s",
            len(text),
            self.settings.openrouter_vision_model,
        )
        return text

    async def extract_from_image(self, context: ScanContext) -> Receipt:
        """Send a receipt image to the configured AI provider and return a structured Receipt.

        Dispatches to either Google GenAI (Gemini) or OpenRouter based on
        ``settings.effective_ai_provider``. Both paths apply the same extraction prompt,
        confidence validation, and retry policy.

        Retry Policy:
            Applies exponential backoff with randomized jitter on transient 429/500/503 provider
            errors (up to MAX_RETRIES retries). Raises ProviderOverloadedError with a sanitized
            friendly message if all retries are exhausted.

        The same confidence_threshold rule applies regardless of provider:
            Receipts with confidence_score < settings.confidence_threshold are rejected
            upstream in the batch worker (process_batch_worker in scan.py).
        """
        provider = self.settings.effective_ai_provider
        image_size = len(context.image_bytes) if context.image_bytes else 0
        model_name = (
            self.settings.gemini_vision_model
            if provider == "gemini"
            else self.settings.openrouter_vision_model
        )
        logger.debug(
            "extract_from_image called: provider=%s, model=%s, mime_type=%s, bytes_size=%d",
            provider,
            model_name,
            context.content_type,
            image_size,
        )

        last_exception: Exception | None = None

        for attempt in range(self.MAX_RETRIES + 1):
            logger.info(
                "Triggering %s vision model %s (attempt %d/%d, %d bytes, mime=%s)",
                provider,
                model_name,
                attempt + 1,
                self.MAX_RETRIES + 1,
                image_size,
                context.content_type,
            )
            try:
                # Dispatch to the appropriate provider backend
                if provider == "gemini":
                    text = await self._extract_gemini(context)
                else:
                    text = await self._extract_openrouter(context)

                # Clean markdown wrappers that some models may emit despite prompt instructions
                if text.startswith("```json"):
                    text = text[7:]
                if text.startswith("```"):
                    text = text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

                receipt = Receipt.model_validate_json(text)
                logger.info(
                    "Successfully extracted receipt via %s: merchant='%s', total=%.2f, confidence=%.2f, items_count=%d",
                    provider,
                    receipt.merchant_name,
                    receipt.total_amount or 0.0,
                    receipt.confidence_score or 0.0,
                    len(receipt.line_items or []),
                )
                return receipt

            except Exception as e:
                last_exception = e
                is_overload = is_provider_overload_error(e)

                if is_overload and attempt < self.MAX_RETRIES:
                    # Exponential backoff with full jitter: delay * 2^attempt * uniform(0.5, 1.5)
                    jitter = random.uniform(0.5, 1.5)
                    backoff = min(self.BASE_DELAY_SECONDS * (2**attempt) * jitter, 10.0)
                    logger.warning(
                        "%s vision provider error on attempt %d/%d (%s). Retrying in %.2fs with jitter...",
                        provider,
                        attempt + 1,
                        self.MAX_RETRIES + 1,
                        e,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue

                if is_overload:
                    logger.error(
                        "%s vision provider overload retries exhausted (%d attempts): %s",
                        provider,
                        self.MAX_RETRIES + 1,
                        e,
                        exc_info=True,
                    )
                    raise ProviderOverloadedError(
                        message=FRIENDLY_ERROR_MESSAGE,
                        original_error=e,
                    ) from e

                logger.error("Non-retryable extraction error via %s: %s", provider, e, exc_info=True)
                raise
