"""
test_evidence_citations.py — El campo "evidencia" puede citar texto literal
entre comillas; si esa cita no existe en la transcripción original, debe
marcarse como advertencia (posible alucinación).
"""

from __future__ import annotations

import json

import gemini_evaluator as ge
from mocks import valid_evaluation_json


def test_genuine_quote_passes_without_warning(sample_conversation):
    real_line = sample_conversation["transcripcion"][0]["texto"]
    raw = valid_evaluation_json(
        conversation_id=sample_conversation["id"],
        evidencia_override={"R1": f'El agente dijo: "{real_line}"'},
    )
    evaluation = json.loads(raw)
    warnings = ge.validate_evidence_citations(evaluation, sample_conversation)
    assert warnings == []


def test_fabricated_quote_is_rejected(sample_conversation):
    fabricated = "le garantizo que si no paga hoy vamos a embargar su casa mañana mismo"
    raw = valid_evaluation_json(
        conversation_id=sample_conversation["id"],
        evidencia_override={"R10": f'El agente amenazó: "{fabricated}"'},
    )
    evaluation = json.loads(raw)
    warnings = ge.validate_evidence_citations(evaluation, sample_conversation)

    assert len(warnings) == 1
    assert warnings[0].startswith("R10:")
    assert fabricated in warnings[0]


def test_fabricated_quote_survives_accent_and_case_normalization(sample_conversation):
    """La normalización (minúsculas, sin acentos) no debe generar falsos
    negativos: una cita alterada en acentos/mayúsculas pero con las MISMAS
    palabras que sí están en la transcripción no debe marcarse como
    inventada; una con palabras distintas sí."""
    real_line = sample_conversation["transcripcion"][0]["texto"]
    altered_case = real_line.upper()
    raw = valid_evaluation_json(
        conversation_id=sample_conversation["id"],
        evidencia_override={"R1": f'Cita: "{altered_case}"'},
    )
    evaluation = json.loads(raw)
    assert ge.validate_evidence_citations(evaluation, sample_conversation) == []


def test_multiple_fabricated_quotes_all_flagged(sample_conversation):
    raw = valid_evaluation_json(
        conversation_id=sample_conversation["id"],
        evidencia_override={
            "R6": 'Ofreció: "un cincuenta por ciento de descuento inmediato"',
            "R8": 'Dijo: "no se preocupe, ya vi el pago reflejado ayer mismo"',
        },
    )
    evaluation = json.loads(raw)
    warnings = ge.validate_evidence_citations(evaluation, sample_conversation)
    flagged_rules = {w.split(":")[0] for w in warnings}
    assert flagged_rules == {"R6", "R8"}
