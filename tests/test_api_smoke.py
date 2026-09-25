"""
test_api_smoke.py — Prueba de humo del endpoint /evaluate usando
TestClient de FastAPI, con el cliente Gemini del módulo `main` reemplazado
por ScriptedGeminiClient (sin red, sin API key).
"""

from __future__ import annotations

import main as api_main
from fastapi.testclient import TestClient
from mocks import ScriptedGeminiClient, valid_evaluation_json


def test_evaluate_endpoint_returns_valid_shape(sample_conversation):
    scripted = ScriptedGeminiClient(
        [valid_evaluation_json(conversation_id=sample_conversation["id"])]
    )
    api_main.app.dependency_overrides[api_main.get_gemini_client] = lambda: scripted
    try:
        client = TestClient(api_main.app)
        payload = {"conversation": sample_conversation}
        resp = client.post("/evaluate", json=payload)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["conversation_id"] == sample_conversation["id"]
        assert body["evaluation"]["puntaje_total"] == 100
        assert "X-Request-ID" in resp.headers
        assert "X-Latency-Ms" in resp.headers
    finally:
        api_main.app.dependency_overrides.clear()


def test_health_endpoint():
    client = TestClient(api_main.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_request_id_is_real_uuid_even_on_internal_error():
    """Regresión: antes, RequestContextMiddleware heredaba de
    BaseHTTPMiddleware, cuyo aislamiento de tareas hacía que los
    contextvars no se vieran dentro de los exception handlers — el
    cliente recibía "request_id":"-" en vez de un id real, inútil para
    correlacionar logs. Se fuerza un error 500 (dependencia que revienta)
    y se confirma que el request_id en la respuesta de error es un UUID
    real, no el valor por defecto."""
    def _boom():
        raise RuntimeError("fallo simulado de dependencia")

    api_main.app.dependency_overrides[api_main.get_gemini_client] = _boom
    try:
        client = TestClient(api_main.app, raise_server_exceptions=False)
        resp = client.get("/health")  # /health no depende de get_gemini_client, control
        assert resp.headers["X-Request-ID"] != "-"

        # /evaluate sí depende de get_gemini_client -> dispara el 500
        payload = {"conversation": {
            "id": "C_ERR", "fecha_llamada": "2026-09-22",
            "datos_cliente": {
                "nombre": "Test", "ultimos4_documento": "1234",
                "producto": "Tarjeta de crédito", "monto_vencido_cop": 1000,
                "fecha_vencimiento": "2026-09-01",
            },
            "transcripcion": [{"hablante": "agente", "texto": "hola"}],
        }}
        resp = client.post("/evaluate", json=payload)
        assert resp.status_code == 500
        body = resp.json()
        assert body["request_id"] != "-"
        assert body["trace_id"] != "-"
        # UUID válido (4 guiones, 36 caracteres) salvo que el cliente haya
        # mandado X-Request-ID propio, que no es el caso aquí.
        assert len(body["request_id"]) == 36
    finally:
        api_main.app.dependency_overrides.clear()
