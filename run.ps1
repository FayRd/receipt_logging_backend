#!/usr/bin/env pwsh
# run.ps1 — Start the Receipt Logger FastAPI backend

$PORT = 8085
$HOST_ADDR = "0.0.0.0"
$VENV_UVICORN = ".venv\Scripts\uvicorn.exe"

Write-Host "Starting Receipt Logger API on http://$HOST_ADDR`:$PORT" -ForegroundColor Cyan
Write-Host "Swagger docs: http://localhost:$PORT/docs" -ForegroundColor Green
Write-Host ""

& $VENV_UVICORN main:app --host $HOST_ADDR --port $PORT --reload
