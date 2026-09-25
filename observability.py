"""
observability.py — request_id / trace_id propagados por contextvars +
logging estructurado en JSON. Diseño:

  - request_id: identifica UNA llamada HTTP a esta API (se genera si el
    cliente no manda X-Request-ID; se devuelve siempre en la respuesta).
  - trace_id: identifica una operación de negocio que puede abarcar varias
    llamadas HTTP (ej. un batch de evaluaciones disparado por un job
    externo). Si el cliente manda X-Trace-ID se respeta (permite
    correlacionar con un sistema de tracing externo); si no, trace_id =
    request_id.

Cada log es una línea JSON con: timestamp, level, event, request_id,
trace_id, y campos adicionales (**extra). latency_ms se mide desde que
entra el middleware hasta que sale la respuesta, cubriendo el tiempo
completo end-to-end (incluye reintentos a Gemini, validación de schema,
validación de evidencia).
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass

_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")


@dataclass
class RequestContext:
    request_id: str
    trace_id: str


def get_request_context() -> RequestContext:
    return RequestContext(request_id=_request_id_var.get(), trace_id=_trace_id_var.get())


def get_request_context_from(request) -> RequestContext:
    """
    Variante para usar DENTRO de exception handlers de FastAPI/Starlette.

    Los exception handlers registrados vía @app.exception_handler NO ven
    de forma confiable los contextvars seteados por el middleware (se
    confirmó empíricamente: mismo asyncio.Task, pero el valor se pierde
    igual en ese punto específico de la maquinaria de Starlette). `request.state`
    en cambio vive directamente en el `scope` del ASGI, el mismo objeto que
    se pasa sin copiar por todo el ciclo de vida del request — por eso es
    la fuente confiable aquí. Con fallback a los contextvars por si
    `request.state` no tiene los campos (no debería pasar en este servicio,
    pero evita un AttributeError si algún día se llama fuera de contexto).
    """
    request_id = getattr(request.state, "request_id", None) or _request_id_var.get()
    trace_id = getattr(request.state, "trace_id", None) or _trace_id_var.get()
    return RequestContext(request_id=request_id, trace_id=trace_id)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "event": record.getMessage(),
            "request_id": getattr(record, "request_id", _request_id_var.get()),
            "trace_id": getattr(record, "trace_id", _trace_id_var.get()),
        }
        for k, v in getattr(record, "extra_fields", {}).items():
            payload[k] = v
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields) -> None:
    logger.log(
        level,
        event,
        extra={
            "request_id": _request_id_var.get(),
            "trace_id": _trace_id_var.get(),
            "extra_fields": fields,
        },
    )


class RequestContextMiddleware:
    """
    ASGI puro (no BaseHTTPMiddleware) A PROPÓSITO: BaseHTTPMiddleware corre
    la app interior en una tarea (task) separada por dentro, y eso puede
    hacer que los contextvars seteados aquí (request_id/trace_id) no se
    vean dentro del handler real ni de los exception handlers — exactamente
    el síntoma de "request_id":"-" en las respuestas de error. Con ASGI
    puro, `self.app(...)` se ejecuta en la MISMA corrutina/tarea que este
    middleware, así que el contextvar seteado antes queda visible en TODO
    lo que pase después, incluidos los exception handlers de FastAPI.

    Mide la latencia end-to-end de la request y emite dos logs
    estructurados por request: 'request_started' y 'request_finished' (con
    latency_ms, status_code y path). Devuelve X-Request-ID / X-Trace-ID /
    X-Latency-Ms en la respuesta inyectando las cabeceras en el mensaje
    'http.response.start' antes de reenviarlo.
    """

    def __init__(self, app):
        self.app = app
        self._logger = get_logger("api.request")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        request_id = headers.get(b"x-request-id", b"").decode() or str(uuid.uuid4())
        trace_id = headers.get(b"x-trace-id", b"").decode() or request_id

        req_token = _request_id_var.set(request_id)
        trace_token = _trace_id_var.set(trace_id)
        # También en scope["state"]: es lo que Request.state expone, y es lo
        # único que los exception handlers de FastAPI pueden leer de forma
        # confiable (ver get_request_context_from en este mismo archivo).
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id
        scope["state"]["trace_id"] = trace_id
        start = time.perf_counter()
        status_holder = {"code": None}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
                latency_ms = (time.perf_counter() - start) * 1000
                extra_headers = [
                    (b"x-request-id", request_id.encode()),
                    (b"x-trace-id", trace_id.encode()),
                    (b"x-latency-ms", f"{latency_ms:.2f}".encode()),
                ]
                message["headers"] = list(message.get("headers") or []) + extra_headers
            await send(message)

        log_event(self._logger, "request_started", method=scope["method"], path=scope["path"])
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            latency_ms = (time.perf_counter() - start) * 1000
            log_event(
                self._logger, "request_finished",
                method=scope["method"], path=scope["path"],
                status_code=status_holder["code"], latency_ms=round(latency_ms, 2),
            )
            _request_id_var.reset(req_token)
            _trace_id_var.reset(trace_token)
