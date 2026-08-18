import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from supabase import AsyncClient
from src.Infrastructure.database import get_supabase_client
from src.Auth.identity import Identity, get_user_identity, get_scoped_identity
from src.Auth.rate_limiter import rate_limit
from src.Infrastructure.logger import get_logger
from src.Models.schemas import (
    ChatMessageInput,
    ChatMessageRecord,
    ConversationCreateRequest,
    ConversationUpdateRequest,
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
logger = get_logger("API.chat")


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
    logger.debug("Entering create_conversation: title=%s, identity (user_id=%s)", body.title, identity.user_id)
    settings = get_settings()
    current_count = await repo.count_conversations(identity)
    if current_count >= settings.max_conversations_per_identity:
        logger.warning(
            "Conversation limit reached for user_id=%s: current_count=%d, max=%d",
            identity.user_id,
            current_count,
            settings.max_conversations_per_identity,
        )
        raise HTTPException(
            status_code=400,
            detail=f"Maximum limit of {settings.max_conversations_per_identity} conversations reached for this user identity.",
        )
    data = await repo.create_conversation(identity, body.title)
    logger.info("Conversation created successfully: conv_id=%s, title=%s, user_id=%s", data.get("id"), data.get("title"), identity.user_id)
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
    logger.debug("Entering list_conversations: limit=%d, offset=%d, identity (user_id=%s)", limit, offset, identity.user_id)
    data = await repo.list_conversations(identity, limit=limit, offset=offset)
    logger.info("list_conversations returned %d records for user_id=%s", len(data), identity.user_id)
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
    logger.debug(
        "Entering get_chat_history: conversation_id=%s, limit=%d, offset=%d, identity (user_id=%s)",
        conversation_id,
        limit,
        offset,
        identity.user_id,
    )
    conv = await repo.get_conversation(conversation_id, identity)
    if not conv:
        logger.warning(
            "Chat history failed: Conversation not found or not owned by user_id=%s (conv_id=%s)",
            identity.user_id,
            conversation_id,
        )
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages, total_count = await repo.get_messages(
        conversation_id, limit=limit, offset=offset
    )
    has_more = (offset + len(messages)) < total_count

    logger.info(
        "Retrieved chat history for conv_id=%s: fetched_count=%d, total_count=%d",
        conversation_id,
        len(messages),
        total_count,
    )

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
    logger.debug(
        "Entering send_chat_query: cloud_mode=%s, conv_id=%s, msg_len=%d, identity (user_id=%s, device_id=%s)",
        bool(body.conversation_id),
        body.conversation_id,
        len(body.message),
        identity.user_id,
        identity.device_id,
    )
    # ── Cloud Store Mode (Authenticated User) ──────────────────────────────────
    if identity.is_authenticated:
        if body.conversation_id:
            conv = await repo.get_conversation(body.conversation_id, identity)
            if not conv:
                logger.warning(
                    "Chat query failed: Conversation %s not found for user_id=%s",
                    body.conversation_id,
                    identity.user_id,
                )
                raise HTTPException(status_code=404, detail="Conversation not found")
            conv_id = body.conversation_id
            # Fetch recent message history for rolling context window
            history_messages, _ = await repo.get_messages(
                conv_id,
                limit=service.settings.rag_history_messages_limit,
                offset=0,
            )
        else:
            conv_id = None
            history_messages = []

        # Generate Gemini response with identity-scoped receipt context FIRST
        try:
            ai_response_text = await service.generate_response(
                identity, body.message, history_messages
            )
        except Exception as e:
            logger.error("Failed to generate AI response in cloud mode: %s", e, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail="Failed to generate AI response. Please try again.",
            )

        # Only after Gemini succeeds, create new conversation if first turn
        if not conv_id:
            conv = await repo.create_conversation(identity, title="New Conversation")
            conv_id = conv["id"]
            logger.info(
                "Auto-created new conversation %s for user_id=%s after successful AI generation",
                conv_id,
                identity.user_id,
            )

        # Persist both messages to Supabase DB
        user_msg = await repo.add_message(conv_id, "user", body.message)
        ai_msg = await repo.add_message(conv_id, "assistant", ai_response_text)

        logger.info(
            "Chat query processed (Cloud Mode): conv_id=%s, user_msg_id=%s, assistant_msg_id=%s",
            conv_id,
            user_msg.get("id"),
            ai_msg.get("id"),
        )

        return ChatQueryResponse(
            conversation_id=conv_id,
            user_message=user_msg,
            assistant_message=ai_msg,
        )

    # ── Local Store Mode (Guest) ────────────────────────────────────────────────
    try:
        ai_response_text = await service.generate_response_local(
            identity=identity,
            user_message=body.message,
            conversation_history=body.conversation_history,
            recent_receipts=body.receipts,
        )
    except Exception as e:
        logger.error("Failed to generate AI response in local mode: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to generate AI response. Please try again.",
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

    logger.info(
        "Chat query processed (Local Mode): synthetic user_msg_id=%s, assistant_msg_id=%s",
        user_msg.id,
        ai_msg.id,
    )

    return ChatQueryResponse(
        conversation_id=None,
        user_message=user_msg,
        assistant_message=ai_msg,
    )


# ── 5. PATCH /chat/{conversation_id} ─────────────────────────────────────────
@router.patch(
    "/{conversation_id}",
    response_model=ConversationRecord,
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def update_conversation(
    conversation_id: str,
    body: ConversationUpdateRequest,
    identity: Identity = Depends(get_user_identity),
    repo: ConversationRepository = Depends(get_repo),
):
    """Update conversation title by UUID. Requires X-User-Name and X-User-Token headers."""
    logger.debug(
        "Entering update_conversation: conversation_id=%s, title=%s, identity (user_id=%s)",
        conversation_id,
        body.title,
        identity.user_id,
    )
    updated = await repo.update_title(conversation_id, identity, body.title)
    if not updated:
        logger.warning(
            "Update conversation failed: Conv_id %s not found or already deleted for user_id=%s",
            conversation_id,
            identity.user_id,
        )
        raise HTTPException(
            status_code=404,
            detail="Conversation not found or access denied.",
        )
    logger.info(
        "Conversation title updated successfully: conv_id=%s, new_title=%s, user_id=%s",
        conversation_id,
        updated.get("title"),
        identity.user_id,
    )
    return updated


# ── 6. DELETE /chat/{conversation_id} ─────────────────────────────────────────
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
    logger.debug("Entering delete_conversation: conversation_id=%s, identity (user_id=%s)", conversation_id, identity.user_id)
    deleted = await repo.soft_delete(conversation_id, identity)
    if not deleted:
        logger.warning(
            "Delete conversation failed: Conv_id %s not found or already deleted for user_id=%s",
            conversation_id,
            identity.user_id,
        )
        raise HTTPException(
            status_code=404,
            detail="Conversation not found or already deleted.",
        )
    logger.info("Conversation soft-deleted successfully: conv_id=%s, user_id=%s", conversation_id, identity.user_id)
    return {"success": True, "conversation_id": conversation_id}

