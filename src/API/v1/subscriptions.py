import json
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from supabase import AsyncClient

from src.Infrastructure.database import get_supabase_client
from src.Auth.identity import Identity, get_user_identity
from src.Auth.rate_limiter import rate_limit
from src.Infrastructure.logger import get_logger
from src.Models.Users.user_repository import UserRepository
from src.Models.schemas import (
    SubscriptionStatusResponse,
    SubscriptionSyncRequest,
)
from src.config import get_settings

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])
logger = get_logger("API.subscriptions")


async def get_repo(db: AsyncClient = Depends(get_supabase_client)) -> UserRepository:
    return UserRepository(db)


# ── GET /subscriptions/status ─────────────────────────────────────────────────
@router.get(
    "/status",
    response_model=SubscriptionStatusResponse,
    summary="Get current user subscription, reverse-trial and downgrade discount status",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def get_subscription_status(
    identity: Identity = Depends(get_user_identity),
    repo: UserRepository = Depends(get_repo),
):
    """Retrieve subscription tier, remaining trial days, and 7-day downgrade discount window."""
    logger.debug("Entering get_subscription_status: user_id=%s", identity.user_id)
    user = await repo.get_by_id(identity.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    # Check and apply trial expiration if 14 days elapsed
    user = await repo.check_and_apply_trial_expiration(user)

    tier = user.get("tier", "free").lower()
    prefs = user.get("preferences") or {}
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    # 1. Reverse Trial calculations
    trial_start_str = prefs.get("trial_start_at")
    trial_days_remaining = None
    is_trial_expired = False
    is_in_trial = prefs.get("is_in_trial", False)

    if trial_start_str:
        try:
            trial_start = datetime.fromisoformat(trial_start_str.replace("Z", "+00:00"))
            elapsed = now - trial_start
            days_left = 14 - elapsed.days
            trial_days_remaining = max(0, days_left)
            is_trial_expired = elapsed > timedelta(days=14)
            if is_trial_expired:
                is_in_trial = False
        except Exception as e:
            logger.warning("Error parsing trial_start_at '%s': %s", trial_start_str, e)

    # 2. Downgrade Discount Offer (7-day window) calculations
    discount_shown_str = prefs.get("discount_offer_shown_at")
    discount_days_remaining = None
    is_discount_active = False

    had_trial = (trial_start_str is not None) and not prefs.get("trial_ineligible", False)
    offer_claimed = prefs.get("discount_offer_claimed", False)

    if discount_shown_str and had_trial and not offer_claimed and tier == "free":
        try:
            discount_start = datetime.fromisoformat(discount_shown_str.replace("Z", "+00:00"))
            elapsed_discount = now - discount_start
            days_left = 7 - elapsed_discount.days
            discount_days_remaining = max(0, days_left)
            is_discount_active = elapsed_discount <= timedelta(days=7)
        except Exception as e:
            logger.warning("Error parsing discount_offer_shown_at '%s': %s", discount_shown_str, e)
    elif discount_shown_str and not had_trial:
        # Ineligible user who never had a trial; ignore any errant timestamp
        discount_shown_str = None

    # 3. Ad scan quota
    ad_scans_today = prefs.get("ad_scans_today", 0) if prefs.get("ad_scans_date") == today_str else 0
    ad_scans_remaining = max(0, 5 - ad_scans_today)

    return SubscriptionStatusResponse(
        success=True,
        tier=tier,
        is_in_trial=is_in_trial,
        trial_start_at=trial_start_str,
        trial_days_remaining=trial_days_remaining,
        is_trial_expired=is_trial_expired,
        discount_offer_shown_at=discount_shown_str,
        discount_days_remaining=discount_days_remaining,
        is_discount_active=is_discount_active,
        ad_scans_today=ad_scans_today,
        ad_scans_remaining=ad_scans_remaining,
    )


# ── POST /subscriptions/sync ──────────────────────────────────────────────────
@router.post(
    "/sync",
    response_model=SubscriptionStatusResponse,
    summary="Synchronize client-side RevenueCat subscription status with backend",
    dependencies=[Depends(rate_limit(lambda s: s.rate_limit_crud_per_minute))],
)
async def sync_subscription(
    body: SubscriptionSyncRequest,
    identity: Identity = Depends(get_user_identity),
    repo: UserRepository = Depends(get_repo),
):
    """Called by mobile app when RevenueCat entitlement changes (purchase, restore, expiry)."""
    logger.info(
        "Syncing subscription for user_id=%s: is_premium=%s, product=%s",
        identity.user_id,
        body.is_premium,
        body.product_identifier,
    )
    user = await repo.get_by_id(identity.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    prefs = user.get("preferences") or {}
    now = datetime.now(timezone.utc).isoformat()

    if body.is_premium:
        subscription_pref = {
            "is_active": True,
            "product_id": body.product_identifier,
            "original_purchase_date": body.original_purchase_date,
            "expiration_date": body.expiration_date,
            "updated_at": now,
        }
        await repo.set_tier(
            identity.user_id,
            "premium",
            updated_preferences={
                "subscription": subscription_pref,
                "discount_offer_claimed": True,
                "discount_offer_shown_at": None,
            },
        )
    else:
        # User not paid premium in RevenueCat
        # Check if they still have active 14-day trial
        trial_start_str = prefs.get("trial_start_at")
        in_trial = False
        if trial_start_str and prefs.get("is_in_trial", False):
            try:
                t_start = datetime.fromisoformat(trial_start_str.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - t_start <= timedelta(days=14):
                    in_trial = True
            except Exception:
                pass

        if not in_trial:
            await repo.set_tier(identity.user_id, "free")
            # Only trigger discount offer if user actually had a trial and is eligible
            had_trial = (trial_start_str is not None) and not prefs.get("trial_ineligible", False)
            if had_trial and not prefs.get("discount_offer_claimed", False):
                await repo.set_discount_offer_shown(identity.user_id)

    return await get_subscription_status(identity=identity, repo=repo)


# ── POST /subscriptions/webhook ───────────────────────────────────────────────
@router.post(
    "/webhook",
    summary="RevenueCat server-to-server webhook endpoint",
    status_code=200,
)
async def revenuecat_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
    repo: UserRepository = Depends(get_repo),
):
    """Handle RevenueCat Server-to-Server webhook events.

    Updates Supabase user tier and preferences in real time upon StoreKit / Play Billing lifecycle events.
    """
    settings = get_settings()
    expected_auth = settings.revenuecat_webhook_auth_header
    if expected_auth and authorization != expected_auth:
        logger.warning("Unauthorized RevenueCat webhook attempt: auth header mismatch")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization header.")

    try:
        payload = await request.json()
    except Exception as e:
        logger.warning("Failed to parse RevenueCat webhook payload: %s", e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON payload.")

    event = payload.get("event", {})
    event_type = event.get("type", "")
    app_user_id = event.get("app_user_id", "")
    product_id = event.get("product_id", "")

    logger.info(
        "RevenueCat webhook received: type=%s, app_user_id=%s, product_id=%s",
        event_type,
        app_user_id,
        product_id,
    )

    if not app_user_id:
        return {"status": "ignored", "reason": "No app_user_id provided"}

    user = await repo.get_by_id(app_user_id)
    if not user:
        logger.warning("RevenueCat webhook: User not found for app_user_id=%s", app_user_id)
        return {"status": "ignored", "reason": "User not found"}

    now_iso = datetime.now(timezone.utc).isoformat()
    prefs = user.get("preferences") or {}

    # Event handling based on RevenueCat event types
    # https://www.revenuecat.com/docs/integrations/webhooks/event-types-and-fields
    if event_type in ("INITIAL_PURCHASE", "RENEWAL", "PRODUCT_CHANGE", "UNCANCELLATION"):
        logger.info("Granting Premium to user %s via webhook (%s)", app_user_id, event_type)
        subscription_pref = {
            "is_active": True,
            "product_id": product_id,
            "last_event": event_type,
            "updated_at": now_iso,
        }
        await repo.set_tier(
            app_user_id,
            "premium",
            updated_preferences={
                "subscription": subscription_pref,
                "discount_offer_claimed": True,
                "discount_offer_shown_at": None,
            },
        )

    elif event_type in ("CANCELLATION", "EXPIRATION"):
        logger.info("Downgrading user %s to Free via webhook (%s)", app_user_id, event_type)
        subscription_pref = {
            "is_active": False,
            "product_id": product_id,
            "last_event": event_type,
            "updated_at": now_iso,
        }
        await repo.set_tier(
            app_user_id,
            "free",
            updated_preferences={"subscription": subscription_pref},
        )

    elif event_type == "BILLING_ISSUE":
        logger.warning("Billing issue reported for user %s", app_user_id)
        prefs["subscription_billing_issue"] = True

    return {"status": "success", "event_type": event_type}
