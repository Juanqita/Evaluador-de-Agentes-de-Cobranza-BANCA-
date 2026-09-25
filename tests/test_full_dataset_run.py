"""
test_full_dataset_run.py — Integra todo lo anterior: corre evaluate_conversation
sobre las 20 conversaciones REALES del dataset (con un cliente scripted, sin
red), valida el 100% del resultado contra el schema, y mide p50/p95.

Ejecuta la misma lógica que run_full_dataset.py pero como test (assertions
en vez de solo imprimir), para que quede en la suite de CI.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parent.parent


def test_full_dataset_produces_valid_results_and_latency_report(rubric_schema, conversations, tmp_path):
    env = {"GEMINI_CLIENT_MODE": "mock"}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "run_full_dataset.py")],
        cwd=ROOT, env={**__import__("os").environ, **env},
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr

    import json
    results = json.loads((ROOT / "results.json").read_text(encoding="utf-8"))
    latency_report = json.loads((ROOT / "latency_report.json").read_text(encoding="utf-8"))

    assert len(results) == len(conversations) == 20

    seen_ids = set()
    for r in results:
        assert r["conversation_id"] not in seen_ids, "conversation_id duplicado en results.json"
        seen_ids.add(r["conversation_id"])
        assert r["status"] in ("ok", "fallback_manual_review")
        if r["status"] == "ok":
            jsonschema.validate(instance=r["evaluation"], schema=rubric_schema)

    assert seen_ids == {c["id"] for c in conversations}

    assert latency_report["n_conversaciones"] == 20
    p = latency_report["latency_ms"]
    assert p["min"] <= p["p50"] <= p["p95"] <= p["max"]
    assert p["p50"] > 0
