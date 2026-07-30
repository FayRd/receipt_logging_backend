import re, json
import google.generativeai as genai
from src.Models.schemas import Receipt, ScanRequest
from src.config import get_settings

class ExtractionService:
    def __init__(self):
        self.settings = get_settings()
    
    async def extract_from_ocr(self, request: ScanRequest) -> ReceiptExtraction:
        """Extract structured data from OCR text. Uses AI if key is available, else regex fallback."""
        # TODO: Modify this to input image into gemini model and get structured output
        if self.settings.gemini_api_key:
            return await self._extract_with_gemini(request)
        return self._extract_with_regex(request)
    
    def _extract_with_regex(self, request: ScanRequest) -> ReceiptExtraction:
        # TODO: Remove this helper function
        text = request.ocr_text
        # Find total amount - look for patterns like 'Total: $12.34' or 'TOTAL 12.34'
        total_pattern = re.compile(r'(?:total|amount|sum)[:\s]*\$?([\d,]+\.\d{2})', re.IGNORECASE)
        total_match = total_pattern.search(text)
        total = float(total_match.group(1).replace(',', '')) if total_match else None
        
        # Find date patterns
        date_pattern = re.compile(r'(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})')
        date_match = date_pattern.search(text)
        date = date_match.group(1) if date_match else None
        
        # First non-empty line often contains merchant name
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        merchant = lines[0] if lines else None
        
        return ReceiptExtraction(
            merchant_name=merchant,
            total_amount=total,
            date=date,
            raw_ocr_text=text,
            confidence_score=0.5,
        )
    
    async def _extract_with_gemini(self, request: ScanRequest) -> ReceiptExtraction:
        try:
            # TODO: Refactor with new business logic
            genai.configure(api_key=self.settings.gemini_api_key)
            model = genai.GenerativeModel('gemini-1.5-flash')
            
            image_bytes = None  # TODO: Replace with actual image bytes
            prompt = f"""
                    Task: Extract data from this receipt image into a JSON object strictly matching the schema requirements.
                    Instructions:
                    1. Date: Format the receipt transaction date as an ISO 8601 datetime string (e.g., 2026-03-30T14:30:00Z).
                    2. Raw Text: Transcribe all visible text from top to bottom into raw_text.
                    3. Confidence Score: Assign a float from 0.0 to 1.0 reflecting extraction accuracy based on image legibility.
                    4. Category: Infer a general expense category (e.g., Dining, Groceries, Supplies, Gas).
                    5. Missing Fields: Set any missing or non-applicable optional fields to null.
                    6. Output only valid JSON.
                    """
            
            response = model.generate_content(
                [image_bytes, prompt],
                generation_config=genai.GenerationConfig(
                    response_mime_type="application/json",
                    response_schema=Receipt,
                ),
            )
            # Strip markdown code blocks if present
            content = response.text.strip()
            data = json.loads(content.strip())
            return Receipt(data)
        except Exception as e:
            # Fallback to regex
            result = self._extract_with_regex(request)
            result.notes = f"AI extraction failed: {str(e)}"
            return result
