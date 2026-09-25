"""
test_retry_and_fallback.py — Ejercita evaluate_conversation end-to-end contra
un ScriptedGeminiClient: JSON inválido deliberado que fuerza reintentos,
fallback cuando nunca llega a validar, y errores de transporte (429/500).
"""

from __future__ import annotations

import gemini_evaluator as ge
from mocks import ScriptedGeminiClient, valid_evaluation_json


def test_recovers_after_invalid_json_then_valid(sample_conversation, monkeypatch):
    monkeypatch.setattr(ge, "MAX_RETRIES", 4)
    client = ScriptedGeminiClient([
        "esto no es JSON en absoluto",                          # intento 1: malformado
        '{"id_conversacion": "C01"}',                            # intento 2: JSON válido, schema inválido
        valid_evaluation_json(conversation_id=sample_conversation["id"]),  # intento 3: válido
    ])

    result = ge.evaluate_conversation(client, sample_conversation)

    assert result.status == "ok"
    assert result.attempts == 3
    assert client.call_count == 3
    assert result.evaluation["id_conversacion"] == sample_conversation["id"]


def test_fallback_after_exhausting_retries(sample_conversation, monkeypatch):
    monkeypatch.setattr(ge, "MAX_RETRIES", 3)
    client = ScriptedGeminiClient([
        "malformado 1",
        "malformado 2",
        "malformado 3",
    ])

    result = ge.evaluate_conversation(client, sample_conversation)

    assert result.status == "fallback_manual_review"
    assert result.evaluation is None
    assert result.attempts == 3
    assert result.error is not None
    assert result.raw_last_response == "malformado 3"


def test_fallback_reports_actual_attempts_not_always_max(sample_conversation, monkeypatch):
    """Regresión: build_fallback_result se llamaba con MAX_RETRIES fijo, así
    que un error NO transitorio (ej. 404 por nombre de modelo inválido, que
    no se reintenta) reportaba igual 'attempts: 4' aunque solo hubo 1
    intento real — dato engañoso para diagnosticar. Debe reportar el
    número real de intentos hechos."""
    monkeypatch.setattr(ge, "MAX_RETRIES", 4)
    client = ScriptedGeminiClient([
        Exception("404 NOT_FOUND. Model not found."),  # no transitorio -> no se reintenta
    ])

    result = ge.evaluate_conversation(client, sample_conversation)

    assert result.status == "fallback_manual_review"
    assert result.attempts == 1          # no 4
    assert "404" in result.error
    assert client.call_count == 1        # una sola llamada real a Gemini


def test_first_attempt_success_needs_only_one_call(sample_conversation, monkeypatch):
    monkeypatch.setattr(ge, "MAX_RETRIES", 4)
    client = ScriptedGeminiClient([
        valid_evaluation_json(conversation_id=sample_conversation["id"]),
    ])

    result = ge.evaluate_conversation(client, sample_conversation)

    assert result.status == "ok"
    assert result.attempts == 1
    assert client.call_count == 1


def test_transport_error_retried_then_succeeds(sample_conversation):
    valid_json = valid_evaluation_json(conversation_id=sample_conversation["id"])
    client = ScriptedGeminiClient([
        Exception("429 RESOURCE_EXHAUSTED: rate limit"),
        Exception("503 UNAVAILABLE"),
        valid_json,
    ])

    result = ge.evaluate_conversation(client, sample_conversation)

    assert result.status == "ok"
    assert result.attempts == 1  # los 2 fallos fueron reintentos de TRANSPORTE
    assert client.call_count == 3  # dentro de la misma "attempt" de contenido


def test_transient_error_classified_correctly():
    assert ge._is_transient_error(Exception("429 Too Many Requests")) is True
    assert ge._is_transient_error(Exception("503 Service Unavailable")) is True
    assert ge._is_transient_error(Exception("Deadline exceeded (timeout)")) is True
    assert ge._is_transient_error(Exception("401 UNAUTHENTICATED: bad API key")) is False
