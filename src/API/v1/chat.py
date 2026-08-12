import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
from src.Auth.identity import Identity, get_user_identity, get_scoped_identity
from src.Auth.rate_limiter import rate_limit
from src.Models.schemas import (
    ChatMessageInput,
    ChatMessageRecord,
    ConversationCreateRequest,
    ConversationRecord,
    ChatQueryRequest,
    ChatQueryResponse,
    ChatHistoryResponse,
    ReceiptContextItem,
)
from src.Models.Conversations.conversation_repository import ConversationRepository
from src.Services.chat_service import ChatService
from src.config import get_settings

router = APIRouter(prefix="/chat", tags=["Chat"])


async def get_repo(db: AsyncClient = Depends(get_supabase_client)) -> ConversationRepository:
    return ConversationRepository(db)


async def get_service(db: AsyncClient = Depends(get_supabase_client)) -> ChatService:
    return ChatService(db)


# ── 1. POST /chat/create ──────────────────────────────────────────────────────
@router.post(
    "/create",
    response_model=ConversationRecord,
    status_code=201,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def create_conversation(
    body: ConversationCreateRequest = Body(default_factory=ConversationCreateRequest),
    identity: Identity = Depends(get_user_identity),
    repo: ConversationRepository = Depends(get_repo),
):
    """Create a new AI conversation bound to the caller's authenticated user identity.

    Requires X-User-Name and X-User-Token headers. Omits device headers.
    Body is fully optional — sending no body or empty `{}` creates a conversation
    with the default title "New Conversation".
    Enforces a hard cap of 10 active conversations per user identity.
    Returns HTTP 400 when the limit is reached.
    """
    settings = get_settings()
    current_count = await repo.count_conversations(identity)
    if current_count >= settings.max_conversations_per_identity:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum limit of {settings.max_conversations_per_identity} conversations reached for this user identity.",
        )
    data = await repo.create_conversation(identity, body.title)
    return data


# ── 2. GET /chat/list ─────────────────────────────────────────────────────────
@router.get(
    "/list",
    response_model=list[ConversationRecord],
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def list_conversations(
    limit: int = Query(20, ge=1, le=50, description="Number of conversations to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    identity: Identity = Depends(get_user_identity),
    repo: ConversationRepository = Depends(get_repo),
):
    """List all conversations owned by the caller's authenticated user identity, newest first."""
    data = await repo.list_conversations(identity, limit=limit, offset=offset)
    return data


# ── 3. GET /chat/history ──────────────────────────────────────────────────────
@router.get(
    "/history",
    response_model=ChatHistoryResponse,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def get_chat_history(
    conversation_id: str = Query(..., description="Conversation UUID to fetch history for"),
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    identity: Identity = Depends(get_user_identity),
    repo: ConversationRepository = Depends(get_repo),
):
    """Fetch paginated message history for a conversation.

    Verifies conversation ownership against the caller's authenticated user identity.
    Returns HTTP 404 if not found or not owned by caller.
    """
    conv = await repo.get_conversation(conversation_id, identity)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages, total_count = await repo.get_messages(
        conversation_id, limit=limit, offset=offset
    )
    has_more = (offset + len(messages)) < total_count

    return ChatHistoryResponse(
        conversation_id=conversation_id,
        messages=messages,
        total_count=total_count,
        has_more=has_more,
    )


@router.post(
    "/query",
    response_model=ChatQueryResponse,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_chat_per_minute))],
)
async def send_chat_query(
    body: ChatQueryRequest,
    identity: Identity = Depends(get_scoped_identity),
    repo: ConversationRepository = Depends(get_repo),
    service: ChatService = Depends(get_service),
):
    """Send a message to Gemini 3.6 Flash with multi-store support.

    Cloud Store Mode (conversation_id provided):
        - Verifies conversation ownership (user must be authenticated with X-User-Name/X-User-Token).
        - Fetches conversation history from Supabase DB for rolling context window.
        - Persists both user and assistant messages to Supabase DB.

    Local Store Mode (conversation_id null/omitted):
        - Accepts client-managed conversation_history and recent_receipts for AI RAG context.
        - No Supabase DB reads or writes (zero cloud storage).
        - Returns synthetic UUIDs for both messages so clients can save locally to Isar DB.
        - Supports both Guest (device identity) and User local mode.
    """
    # ── Cloud Store Mode ────────────────────────────────────────────────────────
    if body.conversation_id:
        # Cloud store requires user authentication (not guest)
        if not identity.is_authenticated:
            raise HTTPException(
                status_code=401,
                detail="Cloud store mode requires user authentication (X-Request-Type: user).",
            )
        conv = await repo.get_conversation(body.conversation_id, identity)
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")

        # Fetch recent message history for rolling context window
        history_messages, _ = await repo.get_messages(body.conversation_id, limit=20, offset=0)

        # Generate Gemini response with identity-scoped receipt context
        ai_response_text = await service.generate_response(identity, body.message, history_messages)

        # Persist both messages to Supabase DB
        user_msg = await repo.add_message(body.conversation_id, "user", body.message)
        ai_msg = await repo.add_message(body.conversation_id, "assistant", ai_response_text)

        return ChatQueryResponse(
            conversation_id=body.conversation_id,
            user_message=user_msg,
            assistant_message=ai_msg,
        )

    # ── Local Store Mode (Guest or User Local) ──────────────────────────────────
    ai_response_text = await service.generate_response_local(
        identity=identity,
        user_message=body.message,
        conversation_history=body.conversation_history,
        recent_receipts=body.recent_receipts,
    )

    now_iso = datetime.now(timezone.utc)
    user_msg = ChatMessageRecord(
        id=str(uuid.uuid4()),
        conversation_id=None,
        sender="user",
        content=body.message,
        created_at=now_iso,
    )
    ai_msg = ChatMessageRecord(
        id=str(uuid.uuid4()),
        conversation_id=None,
        sender="assistant",
        content=ai_response_text,
        created_at=now_iso,
    )

    return ChatQueryResponse(
        conversation_id=None,
        user_message=user_msg,
        assistant_message=ai_msg,
    )


# ── 5. DELETE /chat/{conversation_id} ─────────────────────────────────────────
@router.delete(
    "/{conversation_id}",
    status_code=200,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def delete_conversation(
    conversation_id: str,
    identity: Identity = Depends(get_user_identity),
    repo: ConversationRepository = Depends(get_repo),
):
    """Soft-delete a conversation by UUID.

    Verifies that the conversation is owned by the caller's authenticated user identity.
    Returns HTTP 404 if not found, already deleted, or not owned by caller.
    """
    deleted = await repo.soft_delete(conversation_id, identity)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Conversation not found or already deleted.",
        )
    return {"success": True, "conversation_id": conversation_id}
