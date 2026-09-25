"""
Estimación de costo de evaluar 1.000 conversaciones con el módulo
gemini_evaluator.py.

Supuestos de tokens (calibrados contra las 20 transcripciones de muestra y
el texto de la rúbrica, ~1.3 tokens por palabra en español):

  - Rúbrica completa embebida en el prompt (system + criterios R1-R10): ~1.800 tokens
  - Datos del cliente + instrucciones + transcripción promedio (~230 palabras): ~500 tokens
  - Salida estructurada (10 reglas x evidencia/comentario + agregados): ~900 tokens

  input_tokens_por_llamada  = 1.800 + 500 = 2.300
  output_tokens_por_llamada = 900

Factor de reintentos: se asume que ~90% de las conversaciones se resuelven
en el primer intento, ~8% requieren 1 reintento (schema inválido o
transitorio) y ~2% requieren 2 reintentos antes de validar o caer a
fallback. Cada reintento repite el prompt completo (no hay caching en esta
estimación base).

  factor_reintentos = 0.90*1 + 0.08*2 + 0.02*3 = 1.12

Precios (USD por millón de tokens, tarifa estándar <=200k contexto,
verificados en la página de precios de Google — sept. 2026):

  gemini-3.8-flash : input 0.75 | output 3.75   (precio introductorio,
                     vigente hasta el 31/12/2026; sube a 1.50/7.50 desde
                     el 1/1/2027 — reemplazó a gemini-2.5-flash, retirado
                     para API keys nuevas el 2/9/2026)
  gemini-3.1-pro   : input 2.00 | output 12.00

Nota: no se incluye aquí el precio de "context caching" (cachear la rúbrica,
que es idéntica en las 1.000 llamadas) porque esa tarifa cambia con
frecuencia; cachear el bloque de rúbrica (~1.800 tokens) puede reducir el
costo de input entre 50% y 75% adicional — se recomienda verificarlo en
ai.google.dev antes de producción y activarlo si el volumen crece.
"""

from dataclasses import dataclass

N_CONVERSATIONS = 1000
INPUT_TOKENS_PER_CALL = 2300
OUTPUT_TOKENS_PER_CALL = 900
RETRY_FACTOR = 0.90 * 1 + 0.08 * 2 + 0.02 * 3  # = 1.12


@dataclass
class ModelPricing:
    name: str
    input_per_million: float
    output_per_million: float


MODELS = [
    ModelPricing("gemini-3.8-flash", 0.75, 3.75),
    ModelPricing("gemini-3.1-pro", 2.00, 12.00),
    ModelPricing("gemini-3.1-flash-lite", 0.25, 1.50)
]


def estimate(model: ModelPricing, n: int = N_CONVERSATIONS) -> dict:
    total_input_tokens = n * INPUT_TOKENS_PER_CALL * RETRY_FACTOR
    total_output_tokens = n * OUTPUT_TOKENS_PER_CALL * RETRY_FACTOR

    input_cost = total_input_tokens / 1_000_000 * model.input_per_million
    output_cost = total_output_tokens / 1_000_000 * model.output_per_million
    total_cost = input_cost + output_cost

    return {
        "modelo": model.name,
        "llamadas_totales_estimadas": round(n * RETRY_FACTOR),
        "tokens_input_total": round(total_input_tokens),
        "tokens_output_total": round(total_output_tokens),
        "costo_input_usd": round(input_cost, 2),
        "costo_output_usd": round(output_cost, 2),
        "costo_total_usd": round(total_cost, 2),
        "costo_por_conversacion_usd": round(total_cost / n, 4),
    }


if __name__ == "__main__":
    print(f"Estimación para {N_CONVERSATIONS} conversaciones "
          f"(factor de reintentos = {RETRY_FACTOR:.2f}x llamadas)\n")
    for m in MODELS:
        r = estimate(m)
        print(f"--- {r['modelo']} ---")
        print(f"  Llamadas totales (con reintentos): {r['llamadas_totales_estimadas']}")
        print(f"  Tokens de entrada totales:          {r['tokens_input_total']:,}")
        print(f"  Tokens de salida totales:           {r['tokens_output_total']:,}")
        print(f"  Costo entrada:   ${r['costo_input_usd']:.2f}")
        print(f"  Costo salida:    ${r['costo_output_usd']:.2f}")
        print(f"  Costo TOTAL:     ${r['costo_total_usd']:.2f}")
        print(f"  Costo por conversación: ${r['costo_por_conversacion_usd']:.4f}\n")
