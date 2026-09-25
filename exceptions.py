"""
exceptions.py — Clasificación de errores externos (Gemini) y de negocio, con
sus handlers de FastAPI. Filosofía:

  - Errores TRANSITORIOS de Gemini (429/500/502/503/504/timeout) ya se
    reintentan dentro de gemini_evaluator con backoff; si igual se agotan,
    gemini_evaluator produce un PipelineResult con
    status="fallback_manual_review" (HTTP 200) — NO es un error de la API,
    es un resultado legítimo del pipeline que el cliente debe encolar para
    revisión manual.
  - Errores FATALES (auth/config inválida, cuota agotada de forma
    permanente, modelo inexistente) NO deben reintentarse ni esconderse
    detrás de un 200: se propagan como excepción y se mapean a 502.
  - Errores del PROPIO servicio (rate limit interno, timeout HTTP del
    request completo) se mapean a 429 / 504 respectivamente.
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from observability import get_logger, get_request_context_from
from schemas import ErrorResponse

_logger = get_logger("api.exceptions")


class UpstreamGeminiFatalError(Exception):
    """Error no recuperable del lado de Gemini (401/403, modelo inválido,
    API key ausente, etc.). No debe reintentarse."""


class ServiceRateLimitExceeded(Exception):
    def __init__(self, retry_after_seconds: float):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Rate limit excedido, reintentar en {retry_after_seconds:.1f}s")


class RequestTimeoutError(Exception):
    """Timeout del request end-to-end (más allá de lo que ya cubren los
    reintentos internos de gemini_evaluator)."""


def classify_gemini_exception(exc: Exception) -> str:
    """Clasifica una excepción cruda del SDK de Gemini. Usado por
    gemini_evaluator/el router para decidir si es fatal o transitoria."""
    msg = str(exc).lower()
    if any(code in msg for code in ("401", "403", "permission_denied", "unauthenticated", "api key")):
        return "fatal"
    if any(code in msg for code in ("429", "500", "502", "503", "504", "timeout", "deadline")):
        return "transient"
    return "unknown"


def register_exception_handlers(app: FastAPI) -> None:

    @app.exception_handler(UpstreamGeminiFatalError)
    async def handle_fatal(request: Request, exc: UpstreamGeminiFatalError):
        ctx = get_request_context_from(request)
        return JSONResponse(
            status_code=502,
            content=ErrorResponse(
                request_id=ctx.request_id,
                trace_id=ctx.trace_id,
                error="upstream_gemini_fatal_error",
                detail=str(exc),
            ).model_dump(),
        )

    @app.exception_handler(ServiceRateLimitExceeded)
    async def handle_rate_limit(request: Request, exc: ServiceRateLimitExceeded):
        ctx = get_request_context_from(request)
        response = JSONResponse(
            status_code=429,
            content=ErrorResponse(
                request_id=ctx.request_id,
                trace_id=ctx.trace_id,
                error="service_rate_limit_exceeded",
                detail=str(exc),
                retry_after_seconds=exc.retry_after_seconds,
            ).model_dump(),
        )
        response.headers["Retry-After"] = str(int(exc.retry_after_seconds) + 1)
        return response

    @app.exception_handler(RequestTimeoutError)
    async def handle_timeout(request: Request, exc: RequestTimeoutError):
        ctx = get_request_context_from(request)
        return JSONResponse(
            status_code=504,
            content=ErrorResponse(
                request_id=ctx.request_id,
                trace_id=ctx.trace_id,
                error="request_timeout",
                detail=str(exc),
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        ctx = get_request_context_from(request)
        # Traceback completo al log del servidor (con request_id/trace_id
        # para correlacionar) — esto es lo que antes faltaba: el comentario
        # decía "sí queda en el log" pero no había ninguna llamada de
        # logging real aquí, así que un 500 no dejaba rastro diagnosticable.
        _logger.error(
            "unhandled_exception",
            exc_info=exc,
            extra={
                "request_id": ctx.request_id,
                "trace_id": ctx.trace_id,
                "extra_fields": {"path": request.url.path, "method": request.method},
            },
        )
        # No se expone el detalle interno al cliente (mensaje genérico);
        # el detalle completo queda en el log de arriba.
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                request_id=ctx.request_id,
                trace_id=ctx.trace_id,
                error="internal_error",
            ).model_dump(),
        )
