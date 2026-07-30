---
name: fastapi-background-tasks
description: Implement background tasks in FastAPI for async operations like spending summaries, sync processing, and scheduled reports. Covers FastAPI BackgroundTasks and APScheduler.
metadata:
  version: "1.0.0"
---
# FastAPI Background Tasks

## Contents
- [Core Concepts](#core-concepts)
- [Workflow](#workflow)
- [Code Examples](#code-examples)

## Core Concepts
For a snappy API, operations that do not need to be completed immediately before returning a response should be run in the background.
- **FastAPI BackgroundTasks**: Best for fire-and-forget tasks linked to a specific request (e.g., processing a receipt asynchronously, sending a welcome email).
- **APScheduler**: Best for periodic or scheduled jobs running independently of requests (e.g., weekly spending summaries).
- **Robustness**: Background tasks run outside the main request context. Unhandled exceptions in background tasks might not crash the server but will fail silently if not caught and logged.
- **Dependency Injection**: Dependencies (like Supabase client) must be explicitly passed or instantiated within the background task scope.

## Workflow
### Task Progress
- [ ] Implement `FastAPI BackgroundTasks` in route handlers for immediate, post-response tasks.
- [ ] Define solid background task functions with built-in `try/except` error handling and robust logging.
- [ ] Install `apscheduler`.
- [ ] Configure an `AsyncIOScheduler` instance in the FastAPI `lifespan` context.
- [ ] Create periodic job functions (e.g., `generate_weekly_summary()`).
- [ ] Register periodic jobs with the scheduler upon app startup.

## Code Examples

### 1. Fire-and-Forget BackgroundTasks (src/API/v1/routes/sync.py)
```python
import logging
from fastapi import APIRouter, BackgroundTasks, Depends
from supabase import Client
from src.Infrastructure.dependencies import get_supabase_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sync", tags=["Sync"])

async def process_sync_background(user_id: str, db: Client):
    try:
        logger.info(f"Starting background sync for user {user_id}")
        # Perform heavy lifting, AI processing, etc.
        # e.g., db.table("receipts").update({"synced": True}).eq("user_id", user_id).execute()
        logger.info(f"Sync complete for user {user_id}")
    except Exception as e:
        logger.error(f"Background sync failed for user {user_id}: {e}", exc_info=True)

@router.post("/trigger")
async def trigger_sync(
    user_id: str,
    background_tasks: BackgroundTasks,
    db: Client = Depends(get_supabase_client)
):
    # Enqueue the background task
    background_tasks.add_task(process_sync_background, user_id, db)
    
    # Return immediately to the client
    return {"message": "Sync triggered successfully. Processing in background."}
```

### 2. Periodic Jobs with APScheduler (src/main.py)
```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import logging

logger = logging.getLogger(__name__)

async def weekly_spending_summary():
    """Generates weekly spending summaries for all users."""
    try:
        logger.info("Running weekly spending summary job...")
        # Since this runs outside a request, instantiate clients directly
        # from src.Infrastructure.config import get_settings
        # from src.Infrastructure.dependencies import get_supabase_client
        # settings = get_settings()
        # db = get_supabase_client(settings)
        # 
        # Perform DB aggregations...
        logger.info("Weekly summary job completed.")
    except Exception as e:
        logger.error(f"Weekly summary job failed: {e}", exc_info=True)

scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting scheduler...")
    # Run every Sunday at midnight
    scheduler.add_job(
        weekly_spending_summary,
        trigger=CronTrigger(day_of_week="sun", hour=0, minute=0),
        id="weekly_summary",
        replace_existing=True
    )
    scheduler.start()
    
    yield
    
    # Shutdown
    logger.info("Shutting down scheduler...")
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)
```
