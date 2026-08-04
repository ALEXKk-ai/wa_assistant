"""FastAPI application entrypoint.

Endpoints:
  GET  /webhook/whatsapp          - Meta's webhook verification handshake
  POST /webhook/whatsapp          - inbound customer messages
  POST /webhook/mpesa/{secret}    - M-Pesa STK Push result callback

Both webhook POST handlers always return 200 quickly, even on internal
errors, once the request is authenticated - this prevents Meta/Safaricom
from interpreting a transient application error as "delivery failed" and
retry-storming the endpoint. Real errors are logged, not surfaced to the
caller as a failure status.
"""
import time
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request, Response
from sqlalchemy import select

from app import engine, repositories as repo
from app.config import get_settings
from app.db import get_session, init_db
from app.logging_conf import configure_logging, get_logger, log_extra, new_correlation_id
from app.payments import reconcile_pending_payments
from app.security import verify_mpesa_callback_secret, verify_whatsapp_signature

settings = get_settings()
configure_logging(settings.log_level)
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Per-IP rate limiter for webhook endpoints
# ---------------------------------------------------------------------------
# Prevents abuse / retry-storm exhaustion of the downstream LLM API quota.
# Sliding window: max N requests per IP within a W-second window.  Generous
# enough that normal Meta traffic (a few messages per second at most for a
# single business) never hits the limit, but tight enough to stop a flood.
# ---------------------------------------------------------------------------

_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_MAX_REQUESTS = 60  # 1 req/s average over the window
_RATE_LIMIT_PURGE_EVERY = 200  # purge stale buckets every N checks


class _RateLimiter:
    """In-memory per-IP sliding-window rate limiter."""

    def __init__(
        self,
        window: float = _RATE_LIMIT_WINDOW_SECONDS,
        max_requests: int = _RATE_LIMIT_MAX_REQUESTS,
        purge_every: int = _RATE_LIMIT_PURGE_EVERY,
    ):
        self._window = window
        self._max = max_requests
        self._purge_every = purge_every
        self._buckets: dict[str, list[float]] = {}  # ip -> list of timestamps
        self._check_count = 0

    def is_rate_limited(self, ip: str) -> bool:
        now = time.monotonic()
        timestamps = self._buckets.get(ip)
        if timestamps is None:
            self._buckets[ip] = [now]
            self._maybe_purge(now)
            return False
        # Drop timestamps outside the window
        cutoff = now - self._window
        timestamps[:] = [t for t in timestamps if t > cutoff]
        if len(timestamps) >= self._max:
            return True
        timestamps.append(now)
        self._maybe_purge(now)
        return False

    def _maybe_purge(self, now: float) -> None:
        self._check_count += 1
        if self._check_count < self._purge_every:
            return
        cutoff = now - self._window
        stale_ips = [ip for ip, ts in self._buckets.items() if not ts or ts[-1] <= cutoff]
        for ip in stale_ips:
            del self._buckets[ip]
        self._check_count = 0


_webhook_rate_limiter = _RateLimiter()

scheduler = AsyncIOScheduler()


async def _business_lookup(business_id: int):
    async with get_session() as session:
        return await repo.get_business(session, business_id)


from app.engine import process_payment_completion_side_effects


async def _reconciliation_job() -> None:
    new_correlation_id()
    async with get_session() as session:
        async def _on_completed(payment):
            await process_payment_completion_side_effects(session, payment, _business_lookup)

        resolved = await reconcile_pending_payments(
            session, _business_lookup, on_payment_completed=_on_completed
        )
        if resolved:
            logger.info("Reconciliation job resolved stuck payments", extra=log_extra(count=resolved))
        expired = await repo.expire_stale_pending_deposit_bookings(session)
        if expired:
            logger.info("Reconciliation job expired stale pending-deposit bookings", extra=log_extra(count=expired))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler.add_job(_reconciliation_job, "interval", minutes=5, id="reconcile_payments")
    scheduler.start()
    logger.info("Application started", extra=log_extra(environment=settings.environment))
    yield
    scheduler.shutdown()


app = FastAPI(title="WA Business Assistant", lifespan=lifespan)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    new_correlation_id()
    response = await call_next(request)
    return response


@app.get("/webhook/whatsapp")
async def verify_whatsapp_webhook(request: Request) -> Response:
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == settings.whatsapp_webhook_verify_token
    ):
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    return Response(status_code=403)


@app.post("/webhook/whatsapp")
async def receive_whatsapp_webhook(request: Request) -> Response:
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_whatsapp_signature(body, signature):
        logger.warning("Rejected WhatsApp webhook with invalid signature")
        return Response(status_code=403)

    client_ip = request.client.host if request.client else "unknown"
    if _webhook_rate_limiter.is_rate_limited(client_ip):
        logger.warning("Rate-limited WhatsApp webhook", extra=log_extra(ip=client_ip))
        return Response(status_code=429)

    payload = await request.json()
    try:
        async with get_session() as session:
            await engine.handle_whatsapp_webhook(session, payload, settings.mpesa_callback_secret)
    except Exception:  # noqa: BLE001
        logger.exception("Unhandled error processing WhatsApp webhook")
    return Response(status_code=200)


@app.post("/webhook/mpesa/{path_secret}")
async def receive_mpesa_callback(path_secret: str, request: Request) -> Response:
    if not verify_mpesa_callback_secret(path_secret):
        logger.warning("Rejected M-Pesa callback with invalid path secret")
        return Response(status_code=403)

    client_ip = request.client.host if request.client else "unknown"
    if _webhook_rate_limiter.is_rate_limited(client_ip):
        logger.warning("Rate-limited M-Pesa callback", extra=log_extra(ip=client_ip))
        return Response(status_code=429)

    payload = await request.json()
    try:
        async with get_session() as session:
            await engine.handle_mpesa_callback(session, payload, _business_lookup)
    except Exception:  # noqa: BLE001
        logger.exception("Unhandled error processing M-Pesa callback")
    return Response(status_code=200)


@app.get("/healthz")
async def healthz(response: Response) -> dict:
    try:
        async with get_session() as session:
            await session.execute(select(1))
        return {"status": "ok", "db": "healthy"}
    except Exception as exc:
        logger.error("Health check failed - database query failed", extra=log_extra(error=str(exc)))
        response.status_code = 503
        return {"status": "unhealthy", "db": "error", "detail": str(exc)}
