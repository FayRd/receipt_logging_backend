import re
from src.Models.schemas import ReceiptExtraction, LineItem, ScanRequest
from src.config import get_settings

class ExtractionService:
    def __init__(self):
        self.settings = get_settings()
    
    async def extract_from_ocr(self, request: ScanRequest) -> ReceiptExtraction:
        """Extract structured data from OCR text. Uses AI if key is available, else regex fallback."""
        if self.settings.gemini_api_key:
            return await self._extract_with_gemini(request)
        return self._extract_with_regex(request)
    
    def _extract_with_regex(self, request: ScanRequest) -> ReceiptExtraction:
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
            import google.generativeai as genai
            genai.configure(api_key=self.settings.gemini_api_key)
            model = genai.GenerativeModel('gemini-1.5-flash')
            
            prompt = f"""Extract structured receipt data from this OCR text. Return JSON only.
            
OCR Text:
{request.ocr_text}

Return JSON with these fields:
- merchant_name: string or null
- total_amount: number or null  
- subtotal: number or null
- tax_amount: number or null
- currency: string (ISO 4217, default 'USD')
- date: string (YYYY-MM-DD format) or null
- category: one of [Food & Dining, Shopping, Transportation, Entertainment, Healthcare, Utilities, Other] or null
- line_items: array of {{description: string, quantity: number|null, unit_price: number|null, total_price: number|null}}
- confidence_score: number between 0 and 1"""
            
            response = model.generate_content(prompt)
            import json
            # Strip markdown code blocks if present
            content = response.text.strip()
            if content.startswith('```'):
                content = content.split('```')[1]
                if content.startswith('json'):
                    content = content[4:]
            data = json.loads(content.strip())
            return ReceiptExtraction(
                **data,
                raw_ocr_text=request.ocr_text,
            )
        except Exception as e:
            # Fallback to regex
            result = self._extract_with_regex(request)
            result.notes = f"AI extraction failed: {str(e)}"
            return result
