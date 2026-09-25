"""
main.py — API FastAPI para evaluar llamadas de Lina contra la rúbrica R1-R10
usando gemini_evaluator.py.

Endpoints:
  POST /evaluate        — evalúa una conversación.
  POST /evaluate/batch   — evalúa varias conversaciones con concurrencia acotada.

Ejecutar: uvicorn main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from google import genai
from google.genai import types as genai_types

from dotenv import load_dotenv

# Solo tiene efecto en desarrollo local (si existe un .env junto al código);
# en Render/Railway/Fly.io las env vars ya las inyecta la plataforma
# directamente al proceso, así que load_dotenv() no encuentra archivo y no
# hace nada — es seguro dejarlo siempre activo.
load_dotenv()

import gemini_evaluator as ge
from exceptions import (
    RequestTimeoutError,
    UpstreamGeminiFatalError,
    classify_gemini_exception,
    register_exception_handlers,
)
from observability import (
    RequestContextMiddleware,
    configure_logging,
    get_logger,
    get_request_context,
    log_event,
)
from rate_limiter import enforce_batch_rate_limit, enforce_rate_limit
from schemas import (
    BatchEvaluateRequest,
    BatchEvaluateResponse,
    ConversationInput,
    EvaluateRequest,
    EvaluateResponse,
)

# ---------------------------------------------------------------------------
# Configuración de timeouts HTTP
# ---------------------------------------------------------------------------

# Timeout del SDK de Gemini por llamada individual (se aplica DENTRO de cada
# intento de gemini_evaluator.call_gemini_structured, antes de sus propios
# reintentos por transporte). gemini-3.8-flash es un modelo "thinking": aun
# con thinking_level="low" puede tardar más que gemini-2.5-flash, así que
# se deja margen amplio en vez del valor ajustado para el modelo anterior.
GEMINI_HTTP_TIMEOUT_MS = 45_000

# Timeout end-to-end para /evaluate: cubre TODOS los reintentos internos de
# gemini_evaluator (transporte + schema). Con margen sobre
# GEMINI_HTTP_TIMEOUT_MS para permitir al menos un reintento completo sin
# cortar la request antes de que gemini_evaluator termine su propio ciclo.
SINGLE_EVALUATE_TIMEOUT_S = 90.0

# Timeout por conversación individual DENTRO de un batch (más corto: si una
# conversación se cuelga, no debe arrastrar todo el batch).
BATCH_ITEM_TIMEOUT_S = 60.0

# Tamaño máximo de body aceptado, en bytes. Se aplica ANTES de que Pydantic
# parsee nada: un batch de 50 conversaciones x 60 turnos x 1000 caracteres
# más overhead de JSON cabe holgadamente en 3 MB; cualquier request mayor
# se rechaza de inmediato (413), sin gastar CPU parseando ni tokens de
# Gemini. Ver DEPLOY_SECURITY.md, "Riesgo de tamaño de payload".
MAX_BODY_BYTES = 3 * 1024 * 1024  # 3 MB


class MaxBodySizeMiddleware:
    """
    ASGI puro (no BaseHTTPMiddleware) para poder cortar el body EN STREAMING:
    no confía solo en el header Content-Length (que un cliente puede omitir
    o mentir) — cuenta los bytes reales a medida que llegan y aborta apenas
    se supera el límite, antes de que FastAPI/Pydantic reciban el body
    completo.
    """

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = dict(scope.get("headers") or []).get(b"content-length")
        if content_length is not None and int(content_length) > self.max_bytes:
            await self._reject(send)
            return

        total = 0

        async def guarded_receive():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                raise _PayloadTooLarge()
            return message

        try:
            await self.app(scope, guarded_receive, send)
        except _PayloadTooLarge:
            await self._reject(send)

    async def _reject(self, send):
        body = json.dumps({
            "error": "payload_too_large",
            "detail": f"El body excede el límite de {MAX_BODY_BYTES // (1024*1024)} MB.",
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": body})


class _PayloadTooLarge(Exception):
    pass


configure_logging(level=logging.INFO)
logger = get_logger("api.evaluate")

app = FastAPI(title="Lina Evaluation API", version="1.0.0")
# Orden importa: se agrega primero (queda más adentro) para que el tamaño
# se valide ANTES de que el resto de la app vea el body; RequestContextMiddleware
# se agrega después (queda más afuera) para que incluso un 413 tenga
# request_id/trace_id en el log y en las cabeceras de respuesta.
app.add_middleware(MaxBodySizeMiddleware)
app.add_middleware(RequestContextMiddleware)
register_exception_handlers(app)

@functools.lru_cache(maxsize=1)
def get_gemini_client() -> genai.Client:
    """
    Construcción diferida (lazy) y cacheada del cliente real. Diferida a
    propósito: si el cliente se construyera a nivel de módulo, importar
    `main` fallaría sin una API key válida, haciendo imposible testear la
    API con un cliente simulado (ver tests/test_api_smoke.py, que reemplaza
    esta dependencia vía `app.dependency_overrides`).
    """
    return genai.Client(
        http_options=genai_types.HttpOptions(timeout=GEMINI_HTTP_TIMEOUT_MS),
    )


# ---------------------------------------------------------------------------
# Helper compartido: ejecuta gemini_evaluator.evaluate_conversation (sync,
# bloqueante) en un thread, con timeout propio, y lo traduce a EvaluateResponse.
# ---------------------------------------------------------------------------

async def _run_one(
    conversation: ConversationInput, timeout_s: float, client: genai.Client
) -> EvaluateResponse:
    ctx = get_request_context()
    conv_dict = conversation.model_dump(mode="json")
    start = time.perf_counter()

    try:
        pipeline_result: ge.PipelineResult = await asyncio.wait_for(
            asyncio.to_thread(ge.evaluate_conversation, client, conv_dict),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError as e:
        latency_ms = (time.perf_counter() - start) * 1000
        log_event(
            logger, "evaluation_timeout", level=logging.ERROR,
            conversation_id=conversation.id, latency_ms=round(latency_ms, 2),
            timeout_s=timeout_s,
        )
        raise RequestTimeoutError(
            f"Evaluación de '{conversation.id}' excedió {timeout_s}s"
        ) from e

    latency_ms = (time.perf_counter() - start) * 1000

    # Errores fatales (auth/config) no deben esconderse como un
    # "fallback_manual_review" más: se detectan por el mensaje de error
    # capturado dentro del pipeline y se propagan para alertar/500-502.
    if pipeline_result.status == "fallback_manual_review" and pipeline_result.error:
        if classify_gemini_exception(Exception(pipeline_result.error)) == "fatal":
            log_event(
                logger, "gemini_fatal_error", level=logging.CRITICAL,
                conversation_id=conversation.id, error=pipeline_result.error,
            )
            raise UpstreamGeminiFatalError(pipeline_result.error)

    log_event(
        logger, "evaluation_finished",
        conversation_id=conversation.id, status=pipeline_result.status,
        attempts=pipeline_result.attempts, latency_ms=round(latency_ms, 2),
        evidence_warning_count=len(pipeline_result.evidence_warnings),
        injection_warning_count=len(pipeline_result.injection_warnings),
    )

    return EvaluateResponse(
        request_id=ctx.request_id,
        trace_id=ctx.trace_id,
        conversation_id=pipeline_result.conversation_id,
        status=pipeline_result.status,
        attempts=pipeline_result.attempts,
        latency_ms=round(latency_ms, 2),
        evaluation=pipeline_result.evaluation,
        evidence_warnings=pipeline_result.evidence_warnings,
        injection_warnings=pipeline_result.injection_warnings,
        error=pipeline_result.error,
    )


# ---------------------------------------------------------------------------
# POST /evaluate
# ---------------------------------------------------------------------------

@app.post("/evaluate", response_model=EvaluateResponse)
async def evaluate(
    body: EvaluateRequest,
    request: Request,
    _rate_limit: None = Depends(enforce_rate_limit),
    client: genai.Client = Depends(get_gemini_client),
) -> EvaluateResponse:
    log_event(logger, "evaluate_request_received", conversation_id=body.conversation.id)
    return await _run_one(body.conversation, timeout_s=SINGLE_EVALUATE_TIMEOUT_S, client=client)


# ---------------------------------------------------------------------------
# POST /evaluate/batch
# ---------------------------------------------------------------------------

@app.post("/evaluate/batch", response_model=BatchEvaluateResponse)
async def evaluate_batch(
    body: BatchEvaluateRequest,
    request: Request,
    client: genai.Client = Depends(get_gemini_client),
) -> BatchEvaluateResponse:
    enforce_batch_rate_limit(request, n_conversations=len(body.conversations))

    ctx = get_request_context()
    log_event(
        logger, "batch_request_received",
        n_conversations=len(body.conversations), max_concurrency=body.max_concurrency,
    )

    start = time.perf_counter()
    semaphore = asyncio.Semaphore(body.max_concurrency)

    async def _guarded(conv: ConversationInput) -> EvaluateResponse:
        async with semaphore:
            try:
                return await _run_one(conv, timeout_s=BATCH_ITEM_TIMEOUT_S, client=client)
            except (RequestTimeoutError, UpstreamGeminiFatalError) as e:
                # Un ítem del batch no debe tumbar el resto: se registra como
                # fallback_manual_review con el error, en vez de propagar
                # el HTTP error a toda la respuesta del batch.
                return EvaluateResponse(
                    request_id=ctx.request_id, trace_id=ctx.trace_id,
                    conversation_id=conv.id, status="fallback_manual_review",
                    attempts=0, latency_ms=0.0, error=str(e),
                )

    results = await asyncio.gather(*(_guarded(c) for c in body.conversations))

    latency_ms_total = (time.perf_counter() - start) * 1000
    ok_count = sum(1 for r in results if r.status == "ok")

    log_event(
        logger, "batch_request_finished",
        total=len(results), ok_count=ok_count,
        fallback_count=len(results) - ok_count,
        latency_ms_total=round(latency_ms_total, 2),
    )

    return BatchEvaluateResponse(
        request_id=ctx.request_id,
        trace_id=ctx.trace_id,
        total=len(results),
        ok_count=ok_count,
        fallback_count=len(results) - ok_count,
        latency_ms_total=round(latency_ms_total, 2),
        results=results,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
