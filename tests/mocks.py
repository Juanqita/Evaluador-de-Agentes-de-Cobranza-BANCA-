"""
tests/mocks.py — Reemplazos falsos del cliente `google.genai.Client` para no
depender de la red ni de una API key real.

`ScriptedGeminiClient` recibe una lista de "pasos"; cada llamada a
`.models.generate_content(...)` consume el siguiente paso. Un paso puede ser:
  - un str  -> se devuelve como `response.text` (puede ser JSON inválido o
               JSON que no cumple el schema, a propósito).
  - una Exception -> se lanza (para simular 429/500/timeout de Gemini).

Si se agotan los pasos, lanza AssertionError (evita tests que "por accidente"
llaman a Gemini más veces de las que el escenario esperaba).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class _FakeResponse:
    text: str


class _FakeModels:
    def __init__(self, steps: list, latency_fn: Callable[[], float] | None = None):
        self._steps = list(steps)
        self.calls: list[dict] = []
        self._latency_fn = latency_fn

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self._latency_fn is not None:
            time.sleep(self._latency_fn())  # simula la latencia de red/inferencia real
        if not self._steps:
            raise AssertionError(
                f"ScriptedGeminiClient: se agotaron los pasos programados "
                f"(llamada #{len(self.calls)} sin script)."
            )
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return _FakeResponse(text=step)


class ScriptedGeminiClient:
    """Doble de prueba de `genai.Client`. Uso:
        client = ScriptedGeminiClient(["texto malformado", VALID_JSON_STR])

    `latency_fn`, si se pasa, se invoca en CADA llamada a generate_content y
    su retorno (segundos) se usa como time.sleep(...) para simular latencia
    de red/inferencia real dentro de la medición end-to-end.
    """

    def __init__(self, steps: list, latency_fn: Callable[[], float] | None = None):
        self.models = _FakeModels(steps, latency_fn=latency_fn)

    @property
    def call_count(self) -> int:
        return len(self.models.calls)


def valid_evaluation_json(
    conversation_id: str = "C_TEST",
    fecha_llamada: str = "2026-09-22",
    veredicto: str = "aprobado",
    puntaje_total: int = 100,
    evidencia_override: dict[str, str] | None = None,
) -> str:
    """
    Construye un JSON de evaluación 100% válido contra
    rubrica_evaluacion_lina.schema.json — las 10 reglas, cumple por defecto.
    `evidencia_override` permite forzar el texto de "evidencia" de reglas
    puntuales (ej. para inyectar una cita inventada en R1).
    """
    severidades = {
        "R1": "media", "R2": "critica", "R3": "critica", "R4": "alta",
        "R5": "alta", "R6": "critica", "R7": "alta", "R8": "alta",
        "R9": "media", "R10": "critica",
    }
    evaluaciones = []
    for regla, sev in severidades.items():
        evidencia = "El agente cumple el criterio según lo observado en la llamada."
        if evidencia_override and regla in evidencia_override:
            evidencia = evidencia_override[regla]
        evaluaciones.append({
            "regla": regla,
            "criterio_evaluado": f"Criterio {regla}",
            "resultado": "cumple",
            "severidad": sev,
            "evidencia": evidencia,
            "comentario": "Cumple sin observaciones.",
        })

    return json.dumps({
        "id_conversacion": conversation_id,
        "fecha_llamada": fecha_llamada,
        "evaluaciones": evaluaciones,
        "puntaje_total": puntaje_total,
        "veredicto": veredicto,
        "banderas_criticas": [],
    }, ensure_ascii=False)
