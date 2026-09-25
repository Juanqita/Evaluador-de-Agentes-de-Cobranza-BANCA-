"""
schemas.py — Modelos Pydantic para la API de evaluación de Lina.

Los modelos de entrada (ConversationInput) reflejan el formato del dataset
de conversaciones. Los modelos de salida (EvaluationResult, RuleEvaluation)
reflejan 1:1 rubrica_evaluacion_lina.schema.json entregado por
rubric-designer, para que la respuesta de la API sea siempre válida contra
ese schema cuando status == "ok".
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, conint, conlist


# ---------------------------------------------------------------------------
# Entrada: conversación
# ---------------------------------------------------------------------------

class TranscriptTurn(BaseModel):
    hablante: Literal["agente", "cliente", "sistema"]
    # 1000 chars cubre con margen el turno más largo observado en el
    # dataset real (~250 caracteres); ver DEPLOY_SECURITY.md sección
    # "Riesgo de tamaño de payload" para el razonamiento completo.
    texto: str = Field(..., min_length=1, max_length=1000)


class ClientData(BaseModel):
    nombre: str
    ultimos4_documento: str = Field(..., pattern=r"^\d{4}$")
    producto: str
    monto_vencido_cop: conint(gt=0)
    fecha_vencimiento: date


class ConversationInput(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    fecha_llamada: date
    datos_cliente: ClientData
    # 60 turnos es ~5x el máximo observado (11, en C07). Antes era 200,
    # lo que combinado con el límite de batch permitía payloads de >150 MB.
    transcripcion: conlist(TranscriptTurn, min_length=1, max_length=60)


class EvaluateRequest(BaseModel):
    conversation: ConversationInput


class BatchEvaluateRequest(BaseModel):
    # 50 conversaciones por request: suficiente para un lote de trabajo
    # razonable sin habilitar un solo request que dispare 200 llamadas a
    # Gemini (costo + latencia + riesgo de agotar la cuota) de una vez.
    # Volúmenes mayores deben partirse en varios requests desde el cliente.
    conversations: conlist(ConversationInput, min_length=1, max_length=50)
    max_concurrency: conint(ge=1, le=20) = 5


# ---------------------------------------------------------------------------
# Salida: evaluación (espejo de rubrica_evaluacion_lina.schema.json)
# ---------------------------------------------------------------------------

class ResultadoRegla(str, Enum):
    cumple = "cumple"
    no_cumple = "no_cumple"
    no_aplica = "no_aplica"


class Severidad(str, Enum):
    critica = "critica"
    alta = "alta"
    media = "media"


class RuleEvaluation(BaseModel):
    regla: Literal["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10"]
    criterio_evaluado: str | None = None
    resultado: ResultadoRegla
    severidad: Severidad
    evidencia: str
    comentario: str


class Veredicto(str, Enum):
    aprobado = "aprobado"
    aprobado_con_observaciones = "aprobado_con_observaciones"
    rechazado = "rechazado"


class EvaluationResult(BaseModel):
    id_conversacion: str
    fecha_llamada: date
    evaluaciones: conlist(RuleEvaluation, min_length=10, max_length=10)
    puntaje_total: conint(ge=0, le=100)
    veredicto: Veredicto
    banderas_criticas: list[Literal["R2", "R3", "R6", "R10"]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Salida: envoltorio de pipeline (incluye metadatos de infraestructura que
# NO forman parte del schema de la rúbrica: request_id, latencia, reintentos)
# ---------------------------------------------------------------------------

PipelineStatus = Literal["ok", "fallback_manual_review"]


class EvaluateResponse(BaseModel):
    request_id: str
    trace_id: str
    conversation_id: str
    status: PipelineStatus
    attempts: int
    latency_ms: float
    evaluation: EvaluationResult | None = None
    evidence_warnings: list[str] = Field(default_factory=list)
    injection_warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class BatchEvaluateResponse(BaseModel):
    request_id: str
    trace_id: str
    total: int
    ok_count: int
    fallback_count: int
    latency_ms_total: float
    results: list[EvaluateResponse]


class ErrorResponse(BaseModel):
    request_id: str
    trace_id: str
    error: str
    detail: str | None = None
    retry_after_seconds: float | None = None
