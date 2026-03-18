from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request

from dani.service import DaniService
from dani.webhook import normalize_event, parse_body, verify_github_signature


def create_app(service: DaniService) -> FastAPI:
    app = FastAPI(title="dani")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/github")
    async def github_webhook(request: Request) -> dict[str, object]:
        body = await request.body()
        signature = request.headers.get("x-hub-signature-256")
        if not verify_github_signature(service.config.webhook_secret, body, signature):
            raise HTTPException(status_code=401, detail="invalid signature")
        event_name = request.headers.get("x-github-event")
        if event_name is None:
            raise HTTPException(status_code=400, detail="missing event name")
        payload = parse_body(body)
        event = normalize_event(event_name, payload)
        if event is None:
            return {"status": "ignored", "reason": "unsupported_event"}
        return service.handle_event(event)

    return app
