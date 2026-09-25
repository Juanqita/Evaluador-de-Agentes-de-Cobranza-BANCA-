"""
test_schema_validation.py — Validación de schema al 100%: todo JSON válido
debe pasar, y CADA tipo de violación relevante del schema debe rechazarse
(no solo "algún" caso inválido).
"""

from __future__ import annotations

import json

import pytest

import gemini_evaluator as ge
from mocks import valid_evaluation_json


def test_valid_json_passes_schema(rubric_schema, sample_conversation):
    raw = valid_evaluation_json(conversation_id=sample_conversation["id"])
    data = ge.parse_and_validate_schema(raw, rubric_schema)
    assert data["id_conversacion"] == sample_conversation["id"]
    assert len(data["evaluaciones"]) == 10


def test_malformed_json_rejected(rubric_schema):
    with pytest.raises(ValueError, match="JSON malformado"):
        ge.parse_and_validate_schema("{esto no es json", rubric_schema)


@pytest.mark.parametrize("mutation", [
    "missing_required_field",
    "wrong_resultado_enum",
    "wrong_severidad_enum",
    "wrong_veredicto_enum",
    "puntaje_out_of_range",
    "extra_unexpected_field",
    "only_9_reglas",
    "duplicate_regla",
    "wrong_regla_order",
])
def test_each_violation_type_rejected(rubric_schema, mutation):
    data = json.loads(valid_evaluation_json())

    if mutation == "missing_required_field":
        del data["evaluaciones"][0]["comentario"]
    elif mutation == "wrong_resultado_enum":
        data["evaluaciones"][0]["resultado"] = "parcialmente_cumple"
    elif mutation == "wrong_severidad_enum":
        data["evaluaciones"][0]["severidad"] = "baja"  # no existe en el schema
    elif mutation == "wrong_veredicto_enum":
        data["veredicto"] = "pendiente"
    elif mutation == "puntaje_out_of_range":
        data["puntaje_total"] = 150
    elif mutation == "extra_unexpected_field":
        data["campo_no_declarado"] = "x"
    elif mutation == "only_9_reglas":
        data["evaluaciones"].pop()
    elif mutation == "duplicate_regla":
        data["evaluaciones"][1]["regla"] = data["evaluaciones"][0]["regla"]
    elif mutation == "wrong_regla_order":
        data["evaluaciones"][0], data["evaluaciones"][1] = (
            data["evaluaciones"][1], data["evaluaciones"][0]
        )

    raw = json.dumps(data, ensure_ascii=False)
    with pytest.raises(ValueError):
        ge.parse_and_validate_schema(raw, rubric_schema)


def test_gemini_schema_conversion_strips_unsupported_keys(rubric_schema):
    gemini_schema = ge.to_gemini_schema(rubric_schema)
    assert "$schema" not in gemini_schema
    assert "$id" not in gemini_schema
    assert "additionalProperties" not in gemini_schema
    assert "additionalProperties" not in gemini_schema["properties"]["evaluaciones"]["items"]
