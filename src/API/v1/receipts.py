from fastapi import APIRouter, HTTPException, Depends
from supabase import Client
from src.Infrastructure.database import get_supabase_client
from src.Models.schemas import ReceiptRecord
from typing import List

router = APIRouter(prefix="/receipts", tags=["Receipts"])

@router.get("/", response_model=List[ReceiptRecord])
async def list_receipts(
    limit: int = 50,
    offset: int = 0,
    supabase: Client = Depends(get_supabase_client)
):
    """List all receipts from Supabase."""
    try:
        response = supabase.table("receipts").select("*").range(offset, offset + limit - 1).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/{receipt_id}", response_model=ReceiptRecord)
async def get_receipt(
    receipt_id: str,
    supabase: Client = Depends(get_supabase_client)
):
    """Get a single receipt by ID."""
    try:
        response = supabase.table("receipts").select("*").eq("id", receipt_id).single().execute()
        if not response.data:
            raise HTTPException(status_code=404, detail="Receipt not found")
        return response.data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{receipt_id}")
async def delete_receipt(
    receipt_id: str,
    supabase: Client = Depends(get_supabase_client)
):
    """Delete a receipt by ID."""
    try:
        supabase.table("receipts").delete().eq("id", receipt_id).execute()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
