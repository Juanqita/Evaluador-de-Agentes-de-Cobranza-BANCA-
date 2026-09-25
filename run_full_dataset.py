"""
run_full_dataset.py — Ejecuta gemini_evaluator sobre las 20 conversaciones
reales de conversaciones_prueba_fde.json, genera results.json y mide
latencia p50/p95 end-to-end por conversación.

Cliente Gemini a usar (selección por variable de entorno GEMINI_CLIENT_MODE):
  - "real" (default si hay GEMINI_API_KEY): usa google.genai.Client real.
  - "mock": usa un evaluador heurístico local (tests/mocks.py) que NO llama
    a Gemini. Sirve para: (a) correr la suite sin costo/API key/red, y
    (b) dejar demostrado extremo a extremo el pipeline completo (schema +
    evidencia + latencia) con datos deterministas.

IMPORTANTE: los resultados generados en modo "mock" son evaluaciones
heurísticas de referencia para probar el pipeline — NO deben confundirse
con una evaluación real de Gemini. results.json incluye "modo_cliente" en
cada registro para que quede explícito cuál se usó.
"""

from __future__ import annotations

import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")  # main.py hace esto al importarse; este script no
                             # importa main.py, así que hay que cargarlo aquí
                             # también o GEMINI_API_KEY nunca se vería.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import gemini_evaluator as ge  # noqa: E402
from mocks import ScriptedGeminiClient, valid_evaluation_json  # noqa: E402

DATASET_PATH = ROOT / "conversaciones_prueba_fde.json"
RESULTS_PATH = ROOT / "results.json"
LATENCY_REPORT_PATH = ROOT / "latency_report.json"


def _heuristic_evaluate(conversation: dict) -> str:
    """
    Evaluador heurístico determinista (basado en reglas explícitas, no LLM)
    usado SOLO en modo mock para poder generar results.json sin red/API key.
    Aplica una porción de los criterios de la rúbrica con reglas simples de
    texto — suficiente para ejercitar el pipeline completo con datos reales
    y variados, no para sustituir la evaluación con Gemini en producción.
    """
    turnos = conversation["transcripcion"]
    texto_agente = " ".join(t["texto"] for t in turnos if t["hablante"] == "agente").lower()
    texto_cliente = " ".join(t["texto"] for t in turnos if t["hablante"] == "cliente").lower()

    r10_ok = not any(w in texto_agente for w in ("embargo", "cobro jurídico", "central de riesgo", "acción legal", "acciones legales"))
    r6_ok = not any(w in texto_agente for w in ("descuento", "condonaci", "cuotas", "refinanc")) or "un asesor" in texto_agente

    resultado_por_regla = {r: "cumple" for r in ge.REGLAS_ORDEN}
    resultado_por_regla["R10"] = "cumple" if r10_ok else "no_cumple"
    resultado_por_regla["R6"] = "cumple" if r6_ok else "no_cumple"

    severidades = {
        "R1": "media", "R2": "critica", "R3": "critica", "R4": "alta",
        "R5": "alta", "R6": "critica", "R7": "alta", "R8": "alta",
        "R9": "media", "R10": "critica",
    }
    evaluaciones = [{
        "regla": r,
        "criterio_evaluado": f"Criterio {r}",
        "resultado": resultado_por_regla[r],
        "severidad": severidades[r],
        "evidencia": "Evaluación heurística basada en presencia/ausencia de lenguaje clave en los turnos del agente.",
        "comentario": "Generado por el evaluador heurístico de referencia (modo mock), no por Gemini.",
    } for r in ge.REGLAS_ORDEN]

    penal = {"critica": 40, "alta": 20, "media": 10}
    puntaje = 100 - sum(penal[severidades[r]] for r in ge.REGLAS_ORDEN if resultado_por_regla[r] == "no_cumple")
    banderas = [r for r in ("R2", "R3", "R6", "R10") if resultado_por_regla[r] == "no_cumple"]
    veredicto = "rechazado" if banderas or puntaje < 70 else ("aprobado" if puntaje >= 90 else "aprobado_con_observaciones")

    return json.dumps({
        "id_conversacion": conversation["id"],
        "fecha_llamada": conversation["fecha_llamada"],
        "evaluaciones": evaluaciones,
        "puntaje_total": max(0, puntaje),
        "veredicto": veredicto,
        "banderas_criticas": banderas,
    }, ensure_ascii=False)


def _build_client_for(conversation: dict, mode: str):
    if mode == "real":
        from google import genai
        return genai.Client()
    # Mock: una sola respuesta válida en cola. La latencia se simula DENTRO
    # de generate_content (50-250ms con cola derecha, como una llamada de
    # red real) para que quede capturada en la medición end-to-end de
    # evaluate_conversation, no antes de empezar a medir.
    return ScriptedGeminiClient(
        [_heuristic_evaluate(conversation)],
        latency_fn=lambda: random.gammavariate(2.0, 0.05),  # media ~100ms, cola larga
    )


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(ordered) - 1)
    if f == c:
        return ordered[f]
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


def main() -> None:
    mode = os.environ.get("GEMINI_CLIENT_MODE") or ("real" if os.environ.get("GEMINI_API_KEY") else "mock")
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    conversations = dataset["conversaciones"]

    results = []
    latencies_ms = []

    for conv in conversations:
        client = _build_client_for(conv, mode)
        start = time.perf_counter()
        pipeline_result = ge.evaluate_conversation(client, conv)
        latency_ms = (time.perf_counter() - start) * 1000

        latencies_ms.append(latency_ms)
        results.append({
            "modo_cliente": mode,
            "conversation_id": pipeline_result.conversation_id,
            "status": pipeline_result.status,
            "attempts": pipeline_result.attempts,
            "latency_ms": round(latency_ms, 2),
            "evidence_warnings": pipeline_result.evidence_warnings,
            "evaluation": pipeline_result.evaluation,
            "error": pipeline_result.error,
        })

    RESULTS_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    latency_report = {
        "modo_cliente": mode,
        "n_conversaciones": len(conversations),
        "ok_count": sum(1 for r in results if r["status"] == "ok"),
        "fallback_count": sum(1 for r in results if r["status"] == "fallback_manual_review"),
        "latency_ms": {
            "min": round(min(latencies_ms), 2),
            "p50": round(percentile(latencies_ms, 50), 2),
            "p95": round(percentile(latencies_ms, 95), 2),
            "max": round(max(latencies_ms), 2),
            "mean": round(statistics.mean(latencies_ms), 2),
        },
    }
    LATENCY_REPORT_PATH.write_text(json.dumps(latency_report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(latency_report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
