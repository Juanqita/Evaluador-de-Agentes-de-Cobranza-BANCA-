"""
listar_modelos.py — Diagnóstico rápido: qué modelos puede usar tu API key
AHORA MISMO, en vez de seguir adivinando nombres uno por uno.

Uso: python listar_modelos.py
(lee GEMINI_API_KEY de tu .env automáticamente, igual que main.py)
"""
from dotenv import load_dotenv
load_dotenv()

from google import genai

client = genai.Client()
print("Modelos disponibles para tu API key que soportan generateContent:\n")
for m in client.models.list():
    actions = getattr(m, "supported_actions", None) or []
    if "generateContent" in actions:
        print(" -", m.name)
