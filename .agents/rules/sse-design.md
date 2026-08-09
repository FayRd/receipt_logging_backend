# SSE Design Rule

In this project, SSE endpoints are used as bare notification signals only.

## Rules

1. **No JSON payloads in SSE `data:` fields.** The event name is the entire signal.
   - Correct: `event: batch_complete\ndata:\n\n`
   - Wrong:   `event: batch_complete\ndata: {"batch_id": "..."}\n\n`

2. **No Pydantic schemas or dataclasses for SSE events.** Since nothing is serialized
   over SSE, adding schemas is dead code.

3. **Client responsibility**: When the client receives an SSE event, it fires a
   follow-up GET request to retrieve the actual data payload.

4. **Keep-alive comments are allowed**: SSE `: keep-alive\n\n` comment lines
   (not events) are acceptable to prevent proxy timeouts.

## Pattern Summary

```
POST /bulk              → client gets batch_id
GET  /bulk/{id}/stream  → SSE open, server polls Redis, emits bare ping when done
GET  /bulk/{id}         → client fetches full results after receiving ping
```
