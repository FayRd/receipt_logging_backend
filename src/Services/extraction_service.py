from google import genai
from google.genai import types
from src.Models.schemas import Receipt, ScanContext
from src.config import get_settings

SYSTEM_PROMPT = """
You are a receipt data extraction engine. Extract all fields from the provided receipt image strictly as JSON.

Rules:
1. date: Format as ISO 8601 (e.g. 2026-03-30T14:30:00Z). Use today's date if not visible.
2. raw_text: Transcribe every visible character from the receipt, top-to-bottom.
3. confidence_score: Float 0.0–1.0 reflecting extraction accuracy based on image legibility.
4. category: Infer from context. Must be one of: Dining, Groceries, Transport, Utilities, Shopping, Entertainment, Health, Supplies, Other.
5. currency: ISO 4217 code (e.g. USD, SGD, MYR, GBP). Default to USD if unclear.
6. Set missing optional fields to null.
7. Output ONLY valid JSON matching the schema. No markdown, no prose.
""".strip()


class ExtractionService:
    def __init__(self):
        self.settings = get_settings()
        self.client = genai.Client(api_key=self.settings.gemini_api_key)

    async def extract_from_image(self, context: ScanContext) -> Receipt:
        """Send a receipt image to Gemini 1.5 Flash Vision and return a structured Receipt.

        Args:
            context: ScanContext containing raw image bytes, MIME type, and client metadata.

        Returns:
            A validated Receipt Pydantic model.

        Raises:
            Exception: Propagated directly to the caller on any API or parsing failure.
        """
        response = self.client.models.generate_content(
            model="gemini-3.6-flash",
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
        return Receipt.model_validate_json(response.text)
