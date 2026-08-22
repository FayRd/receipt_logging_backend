#!/usr/bin/env bash
# run.sh — Start the Receipt Logger FastAPI backend & ARQ Queue Worker

PORT=8085
HOST_ADDR="0.0.0.0"
VENV_UVICORN=".venv/bin/uvicorn"
VENV_ARQ=".venv/bin/arq"

# Resolve ARQ CLI binary
if [ -f "$VENV_ARQ" ]; then
    ARQ_CMD="$VENV_ARQ"
else
    ARQ_CMD="arq"
fi

# Start ARQ worker process in background
echo "Starting ARQ Queue Worker..."
"$ARQ_CMD" src.Queue.queue_worker.WorkerSettings &
WORKER_PID=$!

# Ensure background worker process is terminated when script exits
cleanup() {
    echo ""
    echo "Stopping ARQ Queue Worker (PID: $WORKER_PID)..."
    kill "$WORKER_PID" 2>/dev/null
}
trap cleanup EXIT INT TERM

echo "Starting Receipt Logger API on http://$HOST_ADDR:$PORT"
echo "Swagger docs: http://localhost:$PORT/docs"

if [ -n "$LOGFIRE_TOKEN" ]; then
    echo "Logfire Dashboard: https://logfire.pydantic.dev"
elif [ -n "$OTEL_EXPORTER_OTLP_ENDPOINT" ]; then
    echo "Local Dashboard (Jaeger): http://localhost:16686"
else
    echo "Console Logging: Active (Set LOGFIRE_TOKEN in .env for web dashboard)"
fi
echo ""

if [ -f "$VENV_UVICORN" ]; then
    "$VENV_UVICORN" main:app --host "$HOST_ADDR" --port "$PORT" --reload
else
    uvicorn main:app --host "$HOST_ADDR" --port "$PORT" --reload
fi
