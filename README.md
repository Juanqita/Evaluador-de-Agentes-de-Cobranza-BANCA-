# Lina Evaluation Pipeline — Banco Andino

Servicio de evaluación automática de llamadas de cobranza del agente de voz
**Lina** (Banco Andino, entidad ficticia) contra una rúbrica de 10 reglas de
cumplimiento (R1-R10), usando Gemini como motor de evaluación estructurada,
expuesto como API FastAPI y probado sobre las 20 conversaciones reales del
dataset de prueba.

## Índice
1. [Arquitectura](#arquitectura)
2. [Estructura del repo](#estructura-del-repo)
3. [Instalación](#instalación)
4. [Uso local](#uso-local)
5. [Ejecutar el dataset completo](#ejecutar-el-dataset-completo)
6. [Testing](#testing)
7. [Seguridad](#seguridad)
8. [Despliegue](#despliegue)
9. [Metodología de evaluación (LLM-as-a-judge)](#metodología-de-evaluación-llm-as-a-judge)
10. [Costos](#costos)
11. [Limitaciones conocidas y notas metodológicas](#limitaciones-conocidas-y-notas-metodológicas)

## Arquitectura

```
rúbrica (R1-R10 + JSON Schema)
        │
        ▼
gemini_evaluator.py ── prompt con fencing + Gemini structured output
        │               ├─ reintentos: transporte (429/5xx/timeout) y de
        │               │  contenido (schema inválido)
        │               ├─ validación de citas vs. transcripción original
        │               ├─ recompute_score(): puntaje/veredicto NUNCA
        │               │  se toman del modelo, se recalculan en Python
        │               └─ fallback_manual_review si todo lo anterior falla
        ▼
main.py (FastAPI) ── POST /evaluate, POST /evaluate/batch, GET /health
        ├─ rate limiting propio (token bucket)
        ├─ límite de tamaño de body (413 en streaming)
        ├─ timeouts HTTP en cada capa
        └─ logging estructurado (request_id/trace_id, JSON)
        ▼
Render (free tier) ── despliegue Docker, auto-deploy desde GitHub
```

Cuatro módulos de soporte: `schemas.py` (Pydantic), `exceptions.py`
(clasificación de errores + handlers), `observability.py` (contexto de
request + logging), `rate_limiter.py` (token bucket).

## Estructura del repo

```
rubrica_evaluacion_lina.md              # criterios por regla, severidad, cálculo de puntaje
rubrica_evaluacion_lina.schema.json     # JSON Schema de la evaluación
gemini_evaluator.py                     # prompt, llamada a Gemini, retries, recompute_score
schemas.py                              # modelos Pydantic (request/response)
main.py                                 # API FastAPI
exceptions.py                           # errores tipados + handlers
observability.py                        # request_id/trace_id + logging JSON
rate_limiter.py                         # rate limiting propio
requirements.txt
Dockerfile
render.yaml
.env.example / .gitignore
run_full_dataset.py                     # corre las 20 conversaciones reales, genera results.json
ver_resultados.py                       # resumen legible de results.json
cost_estimation.py                      # estimador de costo por volumen
listar_modelos.py                       # diagnóstico: modelos disponibles para tu API key
conversaciones_prueba_fde.json          # dataset de las 20 llamadas
tests/                                  # 38 tests (pytest)
```

## Instalación

```bash
pip install -r requirements.txt
cp .env.example .env
# editar .env y poner GEMINI_API_KEY=<tu key de Google AI Studio>
```

`main.py` y `run_full_dataset.py` cargan `.env` automáticamente
(`python-dotenv`) — nunca hay que exportar la variable a mano en desarrollo.
`GEMINI_API_KEY` nunca se lee de ningún archivo de código; el SDK
`google-genai` la toma directo del entorno.

## Uso local

```bash
uvicorn main:app --reload --port 8080
```

- `GET /health` → `{"status": "ok"}`
- `POST /evaluate` con `{"conversation": {...}}` → evalúa una llamada
- `POST /evaluate/batch` con `{"conversations": [...], "max_concurrency": 5}` → hasta 50 llamadas por request

**Nota sobre streaming**: el pipeline no usa streaming de la respuesta del
LLM ni de la API. Es una decisión deliberada, no una omisión: el caso de
uso es evaluación por lotes (el cliente necesita el JSON completo y
validado de una conversación, no tokens parciales que no sirven hasta que
el objeto esté completo y haya pasado la validación de schema/citas). Sí
se maneja streaming a nivel de *entrada* — el body de `/evaluate/batch` se
lee en streaming para poder cortar con `MaxBodySizeMiddleware` antes de
cargarlo completo en memoria.

```bash
curl.exe -X POST http://localhost:8080/evaluate -H "Content-Type: application/json" --data-binary "@conversacion_ejemplo.json"
```

Respuesta (`status: "ok"`) trae `evaluation` con las 10 reglas, `puntaje_total`,
`veredicto`, `banderas_criticas`, más `evidence_warnings` e
`injection_warnings` si algo quedó marcado para revisión. `status:
"fallback_manual_review"` significa que ni Gemini ni los reintintos lograron
una respuesta válida — **no** es un error del pipeline, es el diseño
funcionando: nunca se inventa una evaluación.

## Ejecutar el dataset completo

```bash
python run_full_dataset.py    # genera results.json y latency_report.json
python ver_resultados.py      # tabla legible: ID, status, veredicto, puntaje, banderas
```

Corrida real (modelo `gemini-3.1-flash-lite`, 20 conversaciones): 19/20
`ok`, 1 `fallback_manual_review` (503 transitorio de Gemini). Latencia real
p50 ≈ 11.4s, p95 ≈ 21.5s — sensiblemente mayor que los ~93ms/239ms de las
pruebas con cliente simulado, por ser un modelo con razonamiento ("thinking")
incluso en nivel `low`. De las 19 con evaluación válida: 8 `aprobado`,
4 `aprobado_con_observaciones`, 7 `rechazado` — detalle completo de los
hallazgos en `hallazgos_cliente.md`.

## Testing

```bash
python -m pytest tests/ -v   # 38 tests
```

Cobertura: validación de schema al 100% (9 tipos de violación distintos),
rechazo de citas de evidencia inventadas, reintentos y fallback ante JSON
inválido y errores de transporte, recálculo determinístico de puntaje ante
un veredicto "envenenado", detección de patrones de inyección de prompt,
límites de tamaño de payload (Pydantic + middleware ASGI a nivel de
streaming), smoke tests de la API con `TestClient` y un test de regresión
específico para la propagación de `request_id` en exception handlers.

## Seguridad

- **Credenciales**: escaneo completo del repo sin coincidencias reales
  (AWS, Google API key, Slack, JWT, PEM). `GEMINI_API_KEY` solo vive en
  variables de entorno, nunca en código ni en el repo.
- **Tamaño de payload**: límites de Pydantic ajustados a la realidad del
  dataset (60 turnos/llamada, 1.000 caracteres/turno, 50 conversaciones/batch)
  + `MaxBodySizeMiddleware` que corta a los 3 MB en streaming, sin confiar
  solo en `Content-Length`.
- **Inyección de prompt vía transcripción**: la transcripción es contenido
  no confiable (lo escribe quien llama a la API, simulando lo que dijo un
  cliente real) y se interpola directo en el prompt. Mitigación principal:
  `puntaje_total`/`veredicto`/`banderas_criticas` se recalculan siempre en
  Python desde el array `evaluaciones`, nunca se toman de lo que el modelo
  devuelve — así una inyección exitosa, en el peor caso, ensucia el texto
  de una `evidencia` puntual, pero no puede forzar un veredicto favorable
  que no corresponda a los resultados reales por regla. Defensa en
  profundidad: fencing del prompt con marcadores únicos + detección
  heurística de patrones típicos de inyección sobre los turnos de cliente.
- **request_id/trace_id confiables**: bug real encontrado y corregido —
  `BaseHTTPMiddleware` no propaga contextvars de forma confiable hacia los
  exception handlers de FastAPI/Starlette; se resolvió usando
  `request.state` (que vive en el `scope` ASGI, no en un contextvar) como
  fuente para los handlers de error.

## Despliegue

**Render (free tier)**, elegido por ser la única de las tres plataformas
comparadas (Render/Railway/Fly.io) con un free tier recurrente real —
Railway lo eliminó en 2023. Trade-off aceptado: cold start de 30-50s tras
15 min de inactividad.

1. Subir el repo a GitHub (todo excepto `.env`, ya cubierto por `.gitignore`).
2. render.com → "New +" → "Blueprint" → conectar el repo (detecta
   `render.yaml` solo).
3. Pegar `GEMINI_API_KEY` en el dashboard del servicio (nunca en el repo).
4. Primer build (~2-3 min) → URL pública. De ahí en adelante, cada
   `git push` auto-despliega.

**Nota sobre cold start**: tras ~15 min de inactividad el servicio se
duerme; el primer request tras eso puede tardar 30-50s en responder antes
de empezar a evaluar. No es una falla ni una respuesta colgada — un
`GET /health` de calentamiento antes de la evaluación real lo resuelve.

## Metodología de evaluación (LLM-as-a-judge)

Usar un LLM para evaluar el cumplimiento de otro sistema (aquí, un agente
de voz) frente a una rúbrica es una instancia del paradigma conocido en la
literatura como **LLM-as-a-judge**. El estudio fundacional (Zheng et al.,
2023, *"Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena"*, NeurIPS)
encontró que un LLM fuerte actuando como juez concuerda con evaluadores
humanos en más u alrededor del 80% de los casos — similar al nivel de
acuerdo entre dos humanos — pero identificó fallas sistemáticas:
**sesgo de posición** (favorecer una respuesta por su orden, en
comparaciones por pares), **sesgo de verbosidad** (preferir respuestas más
largas sin que eso implique mejor calidad) y **sesgo de auto-preferencia**
(un juez tiende a puntuar mejor las salidas de modelos de su propia
familia). Trabajos posteriores (Shankar et al., 2024, entre otros) plantean
que ningún framework de LLM-as-a-judge debería confiarse sin calibración
empírica contra revisión humana en la tarea específica.

Varias decisiones de diseño de este pipeline responden directamente a esos
hallazgos, aunque nuestro caso es de **calificación absoluta contra una
rúbrica explícita** (cada regla se evalúa de forma independiente como
cumple/no_cumple/no_aplica), no de comparación por pares — un setup menos
expuesto al sesgo de posición que el estudiado originalmente, pero
igualmente vulnerable a que el modelo "redondee para arriba" su propio
veredicto agregado:

- **`recompute_score` nunca confía en el puntaje/veredicto que el modelo
  reporta** — es, en esencia, una forma de blindarse contra el equivalente
  de un sesgo de auto-preferencia a nivel de agregación: el modelo puede
  evaluar cada regla, pero no decide el resultado final.
- **La validación de citas contra la transcripción original** ataca
  directamente el riesgo de confabulación (evidencia que suena plausible
  pero no ocurrió), un problema afín al que motiva gran parte de esta
  literatura.
- **La revisión manual de casos límite** (ver el caso C18 más abajo) es
  exactamente el tipo de calibración humana que Shankar et al. argumentan
  como necesaria antes de confiar en un juez automático a escala.

## Costos

Ver `cost_estimation.py` (1.120 llamadas totales para 1.000 conversaciones,
incluyendo reintentos; 2.576.000 tokens de entrada, 1.008.000 de salida).

| Modelo | Costo total /1.000 conv. | Costo /conversación | Estado |
|---|---|---|---|
| **`gemini-3.1-flash-lite`** | **$2.16** | **$0.0022** | **usado en producción** (modelo real de `results.json`; tarifa oficial $0.25/$1.50 por millón de tokens, sept. 2026) |
| `gemini-3.8-flash` | $5.71 | $0.0057 | evaluado; retirado/inestable durante la prueba (ver Limitaciones) |
| `gemini-3.1-pro` | $17.25 | $0.0172 | evaluado y descartado — ~8x más costoso que el modelo de producción, sin ganancia de calidad justificable para este caso de uso |

El costo real de este pipeline en producción es **≈ $2.16 por cada 1.000
conversaciones** ($0.0022/conversación) con `gemini-3.1-flash-lite`. Nota
de corrección: una versión anterior de `cost_estimation.py` tenía la
tarifa de `flash-lite` copiada por error de `gemini-3.8-flash` ($0.75/$3.75
en vez de su precio real, $0.25/$1.50/millón de tokens) — corregido antes
de esta entrega.

## Limitaciones conocidas y notas metodológicas

- **Disponibilidad de modelo variable**: durante esta prueba,
  `gemini-2.5-flash` (el modelo original elegido) fue retirado para API
  keys nuevas; su reemplazo recomendado, `gemini-3.8-flash` (lanzado
  2/9/2026), devolvió `503` por alta demanda y, con otra key, `403` de
  permisos — probablemente por ser un lanzamiento muy reciente con acceso
  escalonado. Se resolvió fijando `gemini-3.1-flash-lite` (GA desde mayo
  2026, nivel de pensamiento mínimo por defecto) como modelo estable.
  `listar_modelos.py` queda en el repo como herramienta de diagnóstico
  para la próxima vez que esto ocurra.
- **Caso C18 — ambigüedad de R2 (verificación de identidad)**: en esta
  llamada, el agente nunca solicita los últimos 4 dígitos del documento,
  pero tampoco llega a informar monto/producto/fecha de la deuda — deriva
  directo a registrar una solicitud de asesor apenas el cliente dice que no
  tiene dinero. La rúbrica, tal como está redactada, marca esto
  `no_cumple` en R2 porque la única condición de `no_aplica` que definimos
  es "quien contesta no es el titular" (ahí rige R3). Pero R2 existe para
  evitar revelar información financiera a alguien no verificado — riesgo
  que en C18 nunca se materializó. R4 sí tiene una condición de `no_aplica`
  para "la llamada no llegó a esa etapa"; R2 no la tiene, por inconsistencia
  de diseño, no a propósito. Queda documentado como mejora pendiente de la
  rúbrica (ver hallazgos para el cliente) en vez de corregido
  unilateralmente, porque es una decisión de política de riesgo — no solo
  técnica — que le corresponde definir al cliente.
- **Fallback ≠ falla del sistema**: cuando una conversación cae en
  `fallback_manual_review`, es intencional (Gemini no respondió algo válido
  tras los reintintos) y queda en cola para revisión humana en vez de
  forzar un resultado — es importante no interpretar ese estado como un bug.
