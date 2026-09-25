"""
rate_limiter.py — Rate limiting del propio servicio (independiente de los
límites de Gemini). Token bucket en memoria por clave de cliente
(API key si se manda X-API-Key, si no la IP).

Limitación conocida: en memoria de proceso -> no se comparte entre réplicas.
Para producción con >1 instancia, reemplazar `_BUCKETS` por Redis
(INCR + EXPIRE, o un script Lua de token bucket) manteniendo la misma
interfaz `check(key) -> (allowed, retry_after_seconds)`.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from fastapi import Request

from exceptions import ServiceRateLimitExceeded

# Config por defecto: 60 requests/minuto sostenidas, ráfaga de hasta 10.
DEFAULT_RATE_PER_SECOND = 1.0
DEFAULT_BURST = 10

# El endpoint de batch es más costoso (dispara N llamadas a Gemini) -> límite
# propio, más estricto, medido en conversaciones-por-minuto en vez de
# requests-por-minuto.
BATCH_RATE_PER_SECOND = 0.2   # ~12 conversaciones/min "equivalentes"
BATCH_BURST = 5


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketLimiter:
    def __init__(self, rate_per_second: float, burst: int):
        self.rate_per_second = rate_per_second
        self.burst = burst
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self.burst, last_refill=now)
                self._buckets[key] = bucket

            elapsed = now - bucket.last_refill
            bucket.tokens = min(self.burst, bucket.tokens + elapsed * self.rate_per_second)
            bucket.last_refill = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True, 0.0

            missing = cost - bucket.tokens
            retry_after = missing / self.rate_per_second
            return False, retry_after


_default_limiter = TokenBucketLimiter(DEFAULT_RATE_PER_SECOND, DEFAULT_BURST)
_batch_limiter = TokenBucketLimiter(BATCH_RATE_PER_SECOND, BATCH_BURST)


def _client_key(request: Request) -> str:
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return f"key:{api_key}"
    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


def enforce_rate_limit(request: Request) -> None:
    """Dependency para POST /evaluate."""
    allowed, retry_after = _default_limiter.check(_client_key(request))
    if not allowed:
        raise ServiceRateLimitExceeded(retry_after)


def enforce_batch_rate_limit(request: Request, n_conversations: int) -> None:
    """Dependency-like helper para POST /evaluate/batch: el costo del bucket
    es proporcional al tamaño del batch, para que un batch grande consuma
    varias "unidades" de una sola vez en vez de esquivar el límite."""
    allowed, retry_after = _batch_limiter.check(_client_key(request), cost=n_conversations)
    if not allowed:
        raise ServiceRateLimitExceeded(retry_after)
