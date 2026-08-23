from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
import httpx

from src.Auth.rate_limiter import rate_limit
from src.config import get_settings
from src.Infrastructure.logger import get_logger
from src.Models.schemas import FeedbackSubmitRequest, FeedbackSubmitResponse

logger = get_logger("API.help")

router = APIRouter(prefix="/help", tags=["Help & Feedback"])


@router.post(
    "/feedback",
    response_model=FeedbackSubmitResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(rate_limit(1, window_seconds=30))],
    summary="Submit user or guest feedback via Discord Webhook",
    description="Accepts feedback submission and delivers it to a configured Discord webhook channel. Rate limited to 1 submission every 30 seconds.",
)
async def submit_feedback(payload: FeedbackSubmitRequest) -> FeedbackSubmitResponse:
    settings = get_settings()
    webhook_url = settings.discord_feedback_webhook_url.strip()

    logger.info(
        "Feedback submission received: sender=%s, device_id=%s, length=%d chars",
        payload.sender,
        payload.device_id,
        len(payload.description),
    )

    if not webhook_url:
        logger.warning("DISCORD_FEEDBACK_WEBHOOK_URL is not configured; feedback logged but not sent to Discord.")
        return FeedbackSubmitResponse(
            success=True,
            message="Feedback received successfully (webhook not configured in current environment).",
        )

    # Construct Discord Rich Embed payload (Emerald Green color: 0x10B981)
    discord_payload = {
        "embeds": [
            {
                "title": "App Feedback",
                "color": 0x10B981,
                "fields": [
                    {
                        "name": "Sender",
                        "value": payload.sender,
                        "inline": True,
                    },
                    {
                        "name": "Device / Identity",
                        "value": payload.device_id or "Anonymous Device",
                        "inline": True,
                    },
                    {
                        "name": "Platform & Version",
                        "value": f"{payload.platform or 'Mobile'} (v{payload.app_version or '1.0.0'})",
                        "inline": True,
                    },
                    {
                        "name": "Feedback",
                        "value": payload.description,
                        "inline": False,
                    },
                ],
                "footer": {
                    "text": "Seng Bot v1.0.0",
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(webhook_url, json=discord_payload)
            if response.status_code not in (200, 204):
                logger.error(
                    "Discord webhook rejected submission: status=%d, body=%s",
                    response.status_code,
                    response.text,
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Failed to deliver feedback to the notification service.",
                )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Exception sending feedback to Discord webhook: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while transmitting your feedback.",
        )

    logger.info("Feedback successfully delivered to Discord webhook.")
    return FeedbackSubmitResponse(
        success=True,
        message="Thank you! Your feedback has been submitted successfully.",
    )
