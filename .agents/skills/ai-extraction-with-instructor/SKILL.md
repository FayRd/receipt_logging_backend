---
name: ai-extraction-with-instructor
description: Use the Instructor library with Google Gemini (or OpenAI) to extract structured receipt data from raw OCR text. Output is validated Pydantic models. Use when building the AI parsing engine.
metadata:
  version: "1.0.0"
---
# AI Extraction with Instructor

## Contents
- [Core Concepts](#core-concepts)
- [Workflow](#workflow)
- [Code Examples](#code-examples)

## Core Concepts
When parsing raw OCR text from a receipt, unstructured responses are brittle. By using `instructor` combined with LLMs (Google Gemini, with an OpenAI fallback), you can enforce structured output that matches your `Pydantic v2` models directly.
- **Instructor**: A library that patches LLM clients to support strict structured output via Pydantic.
- **Pydantic Validation**: Ensures fields like `total_amount` are numbers and missing fields are handled correctly using `Optional`.
- **Fallback Mechanism**: Since LLMs can sometimes fail or timeout, implementing a fallback to another model (e.g., GPT-4o-mini) ensures robustness.

Required Packages:
```bash
pip install instructor google-generativeai pydantic openai
```

## Workflow
### Task Progress
- [ ] Define the `ReceiptExtraction` Pydantic model with fields like `merchant_name`, `total_amount`, `date`, `line_items`, `category`, and `confidence_score`. Use `Optional` where appropriate.
- [ ] Set up the Google Gemini client and patch it using `instructor.from_gemini()`.
- [ ] Set up a fallback OpenAI client (for GPT-4o-mini).
- [ ] Create a prompt template instructing the LLM to extract specific receipt entities.
- [ ] Write an extraction service function that calls the LLM with `response_model=ReceiptExtraction`.
- [ ] Build the `/api/v1/scan/parse` FastAPI endpoint to accept OCR text and return the structured JSON.

## Code Examples

### 1. Pydantic Model (src/Models/extraction.py)
```python
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import date

class LineItem(BaseModel):
    description: str = Field(description="The name or description of the purchased item.")
    price: Optional[float] = Field(None, description="The price of the item.")
    quantity: Optional[int] = Field(1, description="The quantity purchased.")

class ReceiptExtraction(BaseModel):
    merchant_name: Optional[str] = Field(None, description="The name of the store or merchant.")
    total_amount: float = Field(description="The final total amount paid.")
    currency: Optional[str] = Field("USD", description="The currency symbol or code.")
    date: Optional[date] = Field(None, description="The date of the transaction.")
    line_items: List[LineItem] = Field(default_factory=list, description="List of items purchased.")
    category: Optional[str] = Field(None, description="A general category for the expense (e.g. Groceries, Dining).")
    confidence_score: float = Field(description="A score from 0.0 to 1.0 reflecting how confident the AI is in this extraction.")
```

### 2. Service Logic with Fallback (src/Services/ai_parser.py)
```python
import instructor
import google.generativeai as genai
from openai import OpenAI
from src.Models.extraction import ReceiptExtraction
from src.Infrastructure.config import get_settings
import logging

logger = logging.getLogger(__name__)
settings = get_settings()

genai.configure(api_key=settings.GEMINI_API_KEY)
gemini_client = genai.GenerativeModel("gemini-1.5-pro-latest")
instructor_gemini = instructor.from_gemini(
    client=gemini_client,
    mode=instructor.Mode.GEMINI_JSON,
)

# Optional fallback setup
openai_client = OpenAI(api_key="your_openai_key")
instructor_openai = instructor.from_openai(openai_client)

def parse_receipt_text(ocr_text: str) -> ReceiptExtraction:
    prompt = f"""
    Extract the receipt details from the following OCR text.
    If some information is missing, leave it as null/empty.
    
    OCR TEXT:
    {ocr_text}
    """
    
    try:
        # Try with Gemini First
        response = instructor_gemini.messages.create(
            messages=[{"role": "user", "content": prompt}],
            response_model=ReceiptExtraction,
        )
        return response
    except Exception as e:
        logger.warning(f"Gemini extraction failed: {e}. Falling back to GPT-4o-mini.")
        # Fallback to GPT-4o-mini
        response = instructor_openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_model=ReceiptExtraction,
        )
        return response
```

### 3. FastAPI Endpoint (src/API/v1/routes/scan.py)
```python
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from src.Models.extraction import ReceiptExtraction
from src.Services.ai_parser import parse_receipt_text

router = APIRouter(prefix="/scan", tags=["Scan"])

class OCRRequest(BaseModel):
    ocr_text: str

@router.post("/parse", response_model=ReceiptExtraction)
async def parse_receipt(request: OCRRequest):
    if not request.ocr_text.strip():
        raise HTTPException(status_code=400, detail="OCR text cannot be empty.")
    
    try:
        extraction = parse_receipt_text(request.ocr_text)
        return extraction
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse receipt: {str(e)}")
```
