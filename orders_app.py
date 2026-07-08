import base64
import os
import time
import uuid
from collections import defaultdict, deque

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware

# --------------------------------------------------------------------------
# Assigned values
# --------------------------------------------------------------------------
TOTAL_ORDERS = int(os.environ.get("TOTAL_ORDERS", "59"))   # T
RATE_LIMIT_MAX = int(os.environ.get("RATE_LIMIT_MAX", "16"))  # R
RATE_LIMIT_WINDOW = 10.0  # seconds

app = FastAPI()

# --------------------------------------------------------------------------
# Per-client rate limiting middleware
# --------------------------------------------------------------------------
class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_requests: int, window_seconds: float):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.buckets: dict[str, deque] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        client_id = request.headers.get("X-Client-Id", "anonymous")
        now = time.monotonic()
        bucket = self.buckets[client_id]

        while bucket and now - bucket[0] > self.window_seconds:
            bucket.popleft()

        if len(bucket) >= self.max_requests:
            retry_after = max(1, int(self.window_seconds - (now - bucket[0])) + 1)
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded", "client_id": client_id},
                headers={"Retry-After": str(retry_after)},
            )

        bucket.append(now)
        return await call_next(request)


app.add_middleware(RateLimitMiddleware, max_requests=RATE_LIMIT_MAX, window_seconds=RATE_LIMIT_WINDOW)

# --------------------------------------------------------------------------
# CORS — added LAST so it is the OUTERMOST middleware. This guarantees every
# response (including 429s from the rate limiter above) passes back through
# CORSMiddleware and gets Access-Control-Allow-Origin attached.
# --------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Fixed catalog of orders 1..T
# --------------------------------------------------------------------------
CATALOG = [
    {"id": i, "name": f"Order {i}", "amount": round(i * 9.99, 2)}
    for i in range(1, TOTAL_ORDERS + 1)
]

# --------------------------------------------------------------------------
# Idempotency store: Idempotency-Key -> created order
# --------------------------------------------------------------------------
IDEMPOTENCY_STORE: dict[str, dict] = {}


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def decode_cursor(cursor: str) -> int:
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception:
        return 0


@app.post("/orders")
async def create_order(request: Request):
    idempotency_key = request.headers.get("Idempotency-Key")

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    if idempotency_key and idempotency_key in IDEMPOTENCY_STORE:
        existing = IDEMPOTENCY_STORE[idempotency_key]
        return JSONResponse(status_code=200, content=existing)

    order = {
        "id": str(uuid.uuid4()),
        "status": "created",
        **body,
    }

    if idempotency_key:
        IDEMPOTENCY_STORE[idempotency_key] = order

    return JSONResponse(status_code=201, content=order)


@app.get("/orders")
async def list_orders(limit: int = 10, cursor: str | None = None):
    start = decode_cursor(cursor) if cursor else 0
    limit = max(1, limit)

    items = CATALOG[start:start + limit]
    end = start + len(items)
    next_cursor = encode_cursor(end) if end < TOTAL_ORDERS else None

    return {
        "items": items,
        "orders": items,       # alias
        "next_cursor": next_cursor,
        "next": next_cursor,   # alias
    }


@app.get("/")
async def root():
    return {"status": "ok", "endpoints": ["POST /orders", "GET /orders"]}
