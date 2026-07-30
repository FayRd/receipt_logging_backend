---
name: rag-ai-chat-service
description: Build a personalised AI chat service with RAG (Retrieval-Augmented Generation) over receipt data using Supabase pgvector for embeddings and Gemini for generation. Use when implementing the AI chat feature.
metadata:
  version: "1.0.0"
---
# RAG AI Chat Service

## Contents
- [Core Concepts](#core-concepts)
- [Workflow](#workflow)
- [Code Examples](#code-examples)

## Core Concepts
To answer questions like "How much did I spend on coffee last month?", a Retrieval-Augmented Generation (RAG) system is required.
- **pgvector**: A Supabase/PostgreSQL extension for storing and querying high-dimensional vector embeddings.
- **Embeddings**: Represents text (e.g., receipt details) as numbers. Good models include Google's `text-embedding-004` or OpenAI's `text-embedding-3-small`.
- **RAG Pipeline**:
  1. Embed the user query.
  2. Perform a vector similarity search in Supabase using the query embedding to find relevant receipts.
  3. Inject the retrieved receipts (context) into a prompt.
  4. Use an LLM (Gemini) to generate the final response.
- **Server-Sent Events (SSE)**: For long generations, streaming the response character-by-character drastically improves perceived latency.
- **Privacy First**: Data only leaves the device when the user explicitly queries the chat. Embeddings are stored securely within Supabase using RLS (Row Level Security).

## Workflow
### Task Progress
- [ ] Setup Supabase `pgvector` extension via SQL and create the `receipt_embeddings` table.
- [ ] Define the Supabase RPC function for vector matching (`match_receipts`).
- [ ] Implement embedding generation using `google-generativeai` (`text-embedding-004`).
- [ ] Integrate embedding generation at the time of receipt insertion.
- [ ] Build the retrieval logic: query embedding -> pgvector search -> context assembly.
- [ ] Create the `/api/v1/chat` FastAPI endpoint returning a `StreamingResponse`.

## Code Examples

### 1. Supabase pgvector Setup (SQL)
Execute this in the Supabase SQL Editor:
```sql
create extension if not exists vector;

-- Table to store receipts and their embeddings
create table if not exists receipts (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null,
    content text,
    embedding vector(768) -- 768 for text-embedding-004
);

-- Function for similarity search
create or replace function match_receipts (
  query_embedding vector(768),
  match_threshold float,
  match_count int,
  p_user_id uuid
)
returns table (
  id uuid,
  content text,
  similarity float
)
language sql stable
as $$
  select
    receipts.id,
    receipts.content,
    1 - (receipts.embedding <=> query_embedding) as similarity
  from receipts
  where 1 - (receipts.embedding <=> query_embedding) > match_threshold
    and receipts.user_id = p_user_id
  order by receipts.embedding <=> query_embedding
  limit match_count;
$$;
```

### 2. Retrieval & Generation Pipeline (src/Services/rag_service.py)
```python
import google.generativeai as genai
from supabase import Client
from typing import AsyncGenerator
from src.Infrastructure.config import get_settings

settings = get_settings()
genai.configure(api_key=settings.GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

def get_embedding(text: str) -> list[float]:
    result = genai.embed_content(
        model="models/text-embedding-004",
        content=text,
        task_type="retrieval_query",
    )
    return result['embedding']

async def chat_with_receipts(query: str, user_id: str, db: Client) -> AsyncGenerator[str, None]:
    # 1. Embed query
    query_vector = get_embedding(query)
    
    # 2. Retrieve relevant receipts
    response = db.rpc('match_receipts', {
        'query_embedding': query_vector,
        'match_threshold': 0.7,
        'match_count': 5,
        'p_user_id': user_id
    }).execute()
    
    context_docs = response.data
    context_text = "\n\n".join([doc['content'] for doc in context_docs])
    
    # 3. Construct prompt
    prompt = f"""
    You are a helpful financial assistant. Answer the user's question using ONLY the provided receipt context.
    If the answer isn't in the context, say you don't know based on the provided receipts.
    
    Context Receipts:
    {context_text}
    
    Question: {query}
    """
    
    # 4. Stream response
    response_stream = model.generate_content(prompt, stream=True)
    for chunk in response_stream:
        yield f"data: {chunk.text}\n\n"
    yield "data: [DONE]\n\n"
```

### 3. Streaming FastAPI Endpoint (src/API/v1/routes/chat.py)
```python
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from supabase import Client
from src.Infrastructure.dependencies import get_supabase_client
from src.Services.rag_service import chat_with_receipts

router = APIRouter(prefix="/chat", tags=["Chat"])

class ChatRequest(BaseModel):
    query: str
    user_id: str  # In practice, get this from an Auth dependency

@router.post("/")
async def ai_chat(
    request: ChatRequest,
    db: Client = Depends(get_supabase_client)
):
    if not request.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty.")
    
    # Return Server-Sent Events (SSE) using StreamingResponse
    return StreamingResponse(
        chat_with_receipts(request.query, request.user_id, db),
        media_type="text/event-stream"
    )
```
