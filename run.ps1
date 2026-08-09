#!/usr/bin/env pwsh
# run.ps1 — Start the Receipt Logger FastAPI backend & ARQ Queue Worker

$PORT = 8085
$HOST_ADDR = "0.0.0.0"
$VENV_UVICORN = ".venv\Scripts\uvicorn.exe"
$VENV_ARQ = ".venv\Scripts\arq.exe"

$ARQ_CMD = if (Test-Path $VENV_ARQ) { $VENV_ARQ } else { "arq" }

Write-Host "Starting ARQ Queue Worker..." -ForegroundColor Magenta
$workerProcess = Start-Process -FilePath $ARQ_CMD -ArgumentList "src.Queue.queue_worker.WorkerSettings" -PassThru -NoNewWindow

Write-Host "Starting Receipt Logger API on http://$HOST_ADDR`:$PORT" -ForegroundColor Cyan
Write-Host "Swagger docs: http://localhost:$PORT/docs" -ForegroundColor Green

if ($env:LOGFIRE_TOKEN) {
    Write-Host "Logfire Dashboard: https://logfire.pydantic.dev" -ForegroundColor Yellow
} elif ($env:OTEL_EXPORTER_OTLP_ENDPOINT) {
    Write-Host "Local Dashboard (Jaeger): http://localhost:16686" -ForegroundColor Yellow
} else {
    Write-Host "Console Logging: Active (Set LOGFIRE_TOKEN in .env for web dashboard)" -ForegroundColor Gray
}
Write-Host ""

try {
    & $VENV_UVICORN main:app --host $HOST_ADDR --port $PORT --reload
} finally {
    if ($workerProcess -and -not $workerProcess.HasExited) {
        Write-Host "Stopping ARQ Queue Worker (PID: $($workerProcess.Id))..." -ForegroundColor Magenta
        Stop-Process -Id $workerProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
