from google import genai
from google.genai import types
from src.Models.schemas import Receipt, ScanContext
from src.config import get_settings

SYSTEM_PROMPT = """
You are a specialized receipt and financial statement data extraction engine.
Your primary task is to identify, validate, and extract structured data from images of receipts, retail slips, invoices, bills, and financial statements.

Document Validation & Confidence Scoring Rules:
1. Document Verification & Confidence Score (Float 0.0 to 1.0):
   - Assign 0.8 to 1.0 if the image is a valid, legible receipt, retail slip, invoice, bill, bank statement, or financial document with clear merchant/institution header, line items, transaction date, or financial total.
   - Assign 0.0 to 0.79 if the image is NOT a financial receipt/statement (e.g. photos, landscapes, animals, memes, non-financial documents), if the image is completely illegible/corrupted, or if it is a random snippet lacking financial totals and merchant context.
2. date: Format as ISO 8601 (e.g. 2026-03-30T14:30:00Z). Use today's date if not visible.
3. raw_text: Transcribe every visible character from top to bottom.
4. category: Infer from context. Must be one of: Dining, Groceries, Transport, Utilities, Shopping, Entertainment, Health, Supplies, Other.
5. currency: ISO 4217 code (e.g. USD, SGD, MYR, EUR, GBP). Default to USD if unclear.
6. Set missing optional fields to null.
7. Output ONLY valid JSON matching the schema. No prose, no markdown wrappers.
""".strip()


class ExtractionService:
    def __init__(self):
        self.settings = get_settings()
        self.client = genai.Client(api_key=self.settings.gemini_api_key)

    async def extract_from_image(self, context: ScanContext) -> Receipt:
        """Send a receipt image to Gemini 3.6 Flash Vision and return a structured Receipt.

        Args:
            context: ScanContext containing raw image bytes, MIME type, and client metadata.

        Returns:
            A validated Receipt Pydantic model with confidence_score reflecting document type validity.

        Raises:
            Exception: Propagated directly to the caller on any API or parsing failure.
        """
        # client.aio is the async namespace of the google-genai 2.x SDK
        response = await self.client.aio.models.generate_content(
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

        # Clean markdown wrappers if returned by Gemini
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        return Receipt.model_validate_json(text)
