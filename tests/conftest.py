from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gemini_evaluator as ge  # noqa: E402

DATASET_PATH = ROOT / "conversaciones_prueba_fde.json"


@pytest.fixture(scope="session")
def dataset() -> dict:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def conversations(dataset) -> list[dict]:
    return dataset["conversaciones"]


@pytest.fixture
def sample_conversation(conversations) -> dict:
    return conversations[0]  # C01


@pytest.fixture(scope="session")
def rubric_schema() -> dict:
    return ge._load_rubric_schema()


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Los tests de reintentos ejercitan el backoff real; se elimina la
    espera para que la suite corra en milisegundos sin cambiar la lógica."""
    monkeypatch.setattr(ge.time, "sleep", lambda _seconds: None)
