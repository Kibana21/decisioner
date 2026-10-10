"""FastAPI service that routes insurance agent questions to a line of business (LOB).

Wraps the same LOBs, instructions, and routing guide as lob_classifier.py, but keeps
one pooled, warm HTTPS connection to Azure open for the life of the server, so each
request pays only the API round trip, not the TCP + TLS setup.

    uvicorn api:app --port 8000

    curl -X POST localhost:8000/classify -H 'Content-Type: application/json' \\
         -d '{"question": "Client wants to change the beneficiary on his policy"}'

Settings (environment or .env): AZURE_ENDPOINT, DEPLOYMENT_NAME, AZURE_API_KEY
(omit for Microsoft Entra ID), ROUTING_GUIDE (path, default context/routing_guide.md),
KEEPALIVE_SECONDS (default 60; 0 disables the keep-warm ping).
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from lob_classifier import (
    API_PATH,
    DEFAULT_CONTEXT,
    INSTRUCTIONS,
    LOBS,
    RETRY_STATUSES,
    REVIEW_THRESHOLD,
    ROOT,
    TOKEN_SCOPE,
    load_context,
    load_dotenv,
    retry_delay,
)

MAX_RETRIES = 3
log = logging.getLogger("uvicorn.error")


class ClassifyRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class Timing(BaseModel):
    api_round_trip_ms: float = Field(description="Request sent -> decision -> response received")
    connection_setup_ms: float = Field(description="TCP + TLS for a new connection; 0 when a warm one was reused")
    total_ms: float = Field(description="Time spent in this endpoint")


class ClassifyResponse(BaseModel):
    lob: str
    confidence: float | None
    needs_review: bool
    probabilities: dict[str, float]
    retries: int
    timing: Timing


class Tracer:
    """Collects httpcore trace events to split connection setup from the round trip."""

    def __init__(self) -> None:
        self.marks: dict[str, float] = {}

    async def __call__(self, event: str, info: dict) -> None:
        self.marks[event] = time.perf_counter()

    def span_ms(self, start: str, end: str) -> float:
        if start in self.marks and end in self.marks:
            return (self.marks[end] - self.marks[start]) * 1000
        return 0.0

    @property
    def connection_setup_ms(self) -> float:
        return self.span_ms("connection.connect_tcp.started", "connection.start_tls.complete")

    @property
    def round_trip_ms(self) -> float:
        return self.span_ms("http11.send_request_headers.started", "http11.receive_response_body.complete")


def tls_context() -> ssl.SSLContext:
    # Offer P-256 up front: OpenSSL's default first key share costs an extra
    # round trip with the Azure front end (~1,000 ms vs ~250 ms per handshake).
    context = ssl.create_default_context()
    context.set_ecdh_curve("prime256v1")
    return context


async def keep_warm(client: httpx.AsyncClient, interval: float) -> None:
    """Touch the endpoint periodically so Azure doesn't close the idle connection.

    Hits the resource root, not the decision API, so it doesn't run the model.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await client.get("/")
        except httpx.HTTPError as exc:
            log.warning("Keep-warm ping failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv(ROOT / ".env")
    endpoint = os.environ["AZURE_ENDPOINT"].rstrip("/").removesuffix("/models")
    api_key = os.environ.get("AZURE_API_KEY") or None

    app.state.deployment = os.environ["DEPLOYMENT_NAME"]
    app.state.context = load_context(Path(os.environ.get("ROUTING_GUIDE", DEFAULT_CONTEXT)))
    app.state.credential = None
    if api_key is None:
        from azure.identity import DefaultAzureCredential

        app.state.credential = DefaultAzureCredential()

    app.state.http = httpx.AsyncClient(
        base_url=endpoint,
        verify=tls_context(),
        timeout=60,
        headers={"api-key": api_key} if api_key else {},
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=10, keepalive_expiry=600),
    )

    # Open the connection now, so the first real request doesn't pay for it.
    start = time.perf_counter()
    try:
        await app.state.http.get("/")
        log.info("Warm connection to Azure opened in %.0f ms", (time.perf_counter() - start) * 1000)
    except httpx.HTTPError as exc:
        log.warning("Warm-up failed (first request will connect): %s", exc)

    interval = float(os.environ.get("KEEPALIVE_SECONDS", "60"))
    pinger = asyncio.create_task(keep_warm(app.state.http, interval)) if interval > 0 else None
    yield
    if pinger:
        pinger.cancel()
    await app.state.http.aclose()


app = FastAPI(title="LOB Classifier", lifespan=lifespan)


async def auth_headers(request: Request) -> dict[str, str]:
    credential = request.app.state.credential
    if credential is None:
        return {}
    # azure-identity caches tokens; get_token is sync, so keep it off the event loop.
    token = await asyncio.to_thread(credential.get_token, TOKEN_SCOPE)
    return {"Authorization": f"Bearer {token.token}"}


@app.post("/classify", response_model=ClassifyResponse)
async def classify(body: ClassifyRequest, request: Request) -> ClassifyResponse:
    start = time.perf_counter()
    state = request.app.state
    payload = {
        "model": state.deployment,
        "state": {"company_routing_guide": state.context, "agent_request": body.question},
        "questions": {"lob": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": LOBS}},
    }

    connection_setup_ms = 0.0
    for attempt in range(MAX_RETRIES + 1):
        tracer = Tracer()
        try:
            response = await state.http.post(
                API_PATH,
                json=payload,
                headers=await auth_headers(request),
                extensions={"trace": tracer},
            )
        except httpx.HTTPError as exc:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(retry_delay({}, attempt))
                continue
            raise HTTPException(502, f"Couldn't reach the decision API: {exc}") from exc
        connection_setup_ms += tracer.connection_setup_ms

        if response.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
            await asyncio.sleep(retry_delay(response.headers, attempt))
            continue
        if response.status_code == 429:
            raise HTTPException(429, "Decision API rate limit reached; try again shortly.")
        if response.is_error:
            raise HTTPException(502, f"Decision API returned HTTP {response.status_code}: {response.text[:300]}")
        break

    answer = response.json().get("answers", {}).get("lob", {})
    lob = answer.get("choice")
    if lob not in LOBS:
        raise HTTPException(502, f"Unexpected answer from decision API: {response.text[:300]}")
    confidence = answer.get("confidence")

    return ClassifyResponse(
        lob=lob,
        confidence=confidence,
        needs_review=(confidence or 0) < REVIEW_THRESHOLD,
        probabilities=answer.get("probabilities", {}),
        retries=attempt,
        timing=Timing(
            api_round_trip_ms=round(tracer.round_trip_ms, 1),
            connection_setup_ms=round(connection_setup_ms, 1),
            total_ms=round((time.perf_counter() - start) * 1000, 1),
        ),
    )


@app.get("/health")
async def health(request: Request) -> dict:
    return {"status": "ok", "deployment": request.app.state.deployment, "lobs": list(LOBS)}
