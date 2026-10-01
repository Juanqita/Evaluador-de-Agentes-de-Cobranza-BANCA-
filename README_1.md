# Lina Evaluation Pipeline — Banco Andino

Servicio de evaluación automática de llamadas de cobranza del agente de voz
**Lina** (Banco Andino, entidad ficticia) contra una rúbrica de 10 reglas de
cumplimiento (R1-R10), usando Gemini como motor de evaluación estructurada,
expuesto como API FastAPI y probado sobre las 20 conversaciones reales del
dataset de prueba.

> **Versión entregada vs. esta rama.** La entrega original (25/09/2026) está
> marcada con el tag `entrega-2026-09-25` y su `results.json` no se modificó.
> Esta rama (`post-entrega`) solo agrega correcciones y anotaciones fechadas,
> resumidas en [Actualizaciones post-entrega](#actualizaciones-post-entrega-29092026).

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
12. [Actualizaciones post-entrega (29/09/2026)](#actualizaciones-post-entrega-29092026)

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
.env.example / .gitignore               # (post-entrega: el archivo en el repo se llama env.example)
run_full_dataset.py                     # corre las 20 conversaciones reales, genera results.json
ver_resultados.py                       # resumen legible de results.json
cost_estimation.py                      # estimador de costo por volumen
listar_modelos.py                       # diagnóstico: modelos disponibles para tu API key
conversaciones_prueba_fde.json          # dataset de las 20 llamadas
tests/                                  # 38 tests (pytest) — post-entrega: pytest recolecta 26 (18 funciones de test, algunas parametrizadas)
post-entrega/C03_evaluacion_manual.json # post-entrega: revisión manual de la conversación que quedó en fallback
post-entrega/C03_reevaluacion_api.json  # post-entrega: respuesta de la API al reintentar C03
evaluar_todas.py                        # post-entrega: evalúa las 20 conversaciones contra la API pública
hallazgos_cliente.md                    # documento de hallazgos enviado al cliente (sin cambios; correcciones en este README)
```

## Instalación

```bash
pip install -r requirements.txt
cp .env.example .env
# editar .env y poner GEMINI_API_KEY=<tu key de Google AI Studio>
```

> **Nota post-entrega (29/09/2026):** en el repo la plantilla se llama `env.example` (sin punto inicial); el comando correcto es `cp env.example .env`.

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

> **Nota post-entrega (29/09/2026):** en la versión entregada, `rate_limiter.py` tenía `BATCH_BURST = 5` y cada conversación del lote consume una unidad. Un lote de más de 5 conversaciones respondía **429** sin evaluar nada, así que el límite efectivo era 5 y no 50. Verificado contra el servicio público el 29/09/2026 con las 20 conversaciones: `429 service_rate_limit_exceeded`, "reintentar en 75.0s". Desde el 30/09/2026 el servicio desplegado ya acepta lotes de más de 5 (ver [Despliegue](#despliegue)).

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

> **Nota post-entrega (29/09/2026):** la conversación en `fallback_manual_review` es **C03** (503 UNAVAILABLE de Gemini, un intento). Es la única llamada del dataset donde atiende un tercero y el agente le revela la deuda, por lo que su ausencia afectaba el conteo de fallas críticas. Se evaluó manualmente con la misma rúbrica y la misma fórmula de puntaje (`recompute_score`): **rechazado, 50 puntos, bandera crítica R3**. Detalle en `post-entrega/C03_evaluacion_manual.json`. Con C03 incluida, las 20 quedan en 8 `aprobado`, 4 `aprobado_con_observaciones` y **8** `rechazado`. El `results.json` entregado no se modificó.

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

> **Nota post-entrega (29/09/2026):** dos precisiones. (1) `pytest` recolecta 26 tests, no 38. (2) Una cita de evidencia que no existe en la transcripción **no se rechaza**: queda registrada como advertencia en `evidence_warnings` y la evaluación se devuelve igual (`status: ok`). Además, el prompt permitía parafrasear sin comillas, y 3 de los 22 `no_cumple` del `results.json` entregado no traen cita literal: C02 R2, C09 R7 y C18 R2.

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

> **Nota post-entrega (29/09/2026):** el push del 29/09/2026 a `main` (commit `a0bd384`, `BATCH_BURST` de 5 a 50) no se reflejó en el servicio: al verificarlo ese día, la URL pública seguía aplicando el límite de 5. El 30/09/2026 se verificó que el cambio ya está en producción: un lote de 6 conversaciones a `POST /evaluate/batch` respondió 200, con 6/6 `ok` en 47 s (`request_id` 5f1445aa-35c8-4fba-90fc-2a8f6aa15e87). Con el límite anterior, cualquier lote de más de 5 respondía 429.

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

> **Nota post-entrega (29/09/2026):** después de la entrega se hizo esa calibración a pequeña escala: revisión manual de las 20 transcripciones contra `results.json`. Resultados en [Actualizaciones post-entrega](#actualizaciones-post-entrega-29092026).

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

> **Nota post-entrega (29/09/2026):** el caso concreto fue C03 y su revisión humana ya está hecha (ver arriba). Lo que faltó en la entrega fue identificar la conversación por su ID y cerrar esa revisión antes de enviar el reporte al cliente.

## Actualizaciones post-entrega (29/09/2026)

Nada de lo que sigue cambia los archivos entregados; documenta lo que se
encontró al revisar la entrega después de enviarla.

### 1. C03: evaluación manual del caso que quedó en fallback

| Regla | Resultado | Evidencia (cita literal) |
|---|---|---|
| R1 | cumple | "Buenos días, le habla Lina, asistente virtual de Banco Andino. Le informo que esta llamada está siendo grabada." |
| R2 | no aplica | "No, habla la esposa. Él no está." (no es el titular; rige R3) |
| **R3** | **no cumple (crítica)** | "Por favor dígale que tiene un saldo vencido de dos millones cuatrocientos mil pesos en su tarjeta de crédito y que debe comunicarse con el banco lo antes posible." |
| R4 | no aplica | La rúbrica condiciona R4 a que R3 no aplique. El monto dicho coincide con el registro |
| R5–R8 | no aplica | Sin compromiso, solicitud de descuento, disputa ni pago reportado |
| R9 | no cumple (media) | "Gracias, que tenga buen día." (sin resumen y sin pedir horario de contacto; criterio discutible) |
| R10 | cumple | Sin menciones de cobro jurídico, embargo ni centrales de riesgo |

Puntaje 50, veredicto **rechazado**, bandera crítica R3. Evaluación hecha a
mano por el autor; el puntaje se calculó con `recompute_score()`.

**Reintento por la API (29/09/2026).** C03 se volvió a enviar al servicio
desplegado con `POST /evaluate` (`request_id` 50911194-0a79-4191-8058-100c4702d2a0):
`status: ok`, 1 intento, 9,0 s, sin `evidence_warnings`. Resultado: rechazado,
50 puntos, R3 y R9 en `no_cumple`. **Coincide con la revisión manual en las 10
reglas y en el puntaje.** Respuesta completa en `post-entrega/C03_reevaluacion_api.json`.

### 2. Servicio desplegado: lote limitado a 5 conversaciones (corregido)

- Causa: `BATCH_BURST = 5` en `rate_limiter.py` y un costo de una unidad por conversación.
- Efecto: `POST /evaluate/batch` con las 20 conversaciones respondía 429 sin evaluar. `POST /evaluate` sí funcionaba: C14 se evaluó el 29/09 en 13,8 s, rechazada por R10 con cita literal.
- Corrección: commit `a0bd384` en `main` (`BATCH_BURST` de 5 a 50). Verificado en producción el 30/09/2026 con un lote de 6 conversaciones: 200, 6/6 `ok`.
- Además, `/evaluate/batch` espera `{"conversations": [...]}`. El archivo de la prueba (`{"descripcion", "especificacion_agente", "conversaciones"}`) debe reenvolverse antes de enviarlo.
- El servicio no genera el reporte agregado que pide el enunciado (tasa de cumplimiento por criterio y fallas más frecuentes). La respuesta del lote trae los totales `ok_count` y `fallback_count` y la evaluación de cada conversación, no las tasas por regla.

### 3. Revisión manual de `results.json` (calibración del juez)

Comparación de las 22 decisiones `no_cumple` de las 19 conversaciones evaluadas contra la lectura manual de las transcripciones:

| Resultado | Casos |
|---|---|
| Fallas correctas | 18 de 22 |
| Falsos positivos | 4: R9 en C01 y C08 (el resumen está en el penúltimo turno del agente), R9 en C07 (el cliente colgó; la rúbrica indica `no_aplica`), R2 en C18 (ya documentado en Limitaciones) |
| Falla no detectada | R3 en C03 (no evaluada por el 503; cubierta en el punto 1) |
| Caso discutible | R10 en C07: el agente insiste dos veces en la fecha de pago después de que el cliente pide un asesor |

La verdad de referencia es la revisión del propio autor, no un etiquetado independiente.

### 4. Correcciones al documento de hallazgos para el cliente

- "8/20 aprobadas sin observaciones": 4 de esas 8 tienen una falla de severidad media (R9 en C01, C08 y C13; R1 en C16), aunque su puntaje (90) las deja en `aprobado`. Dos de esas fallas (C01, C08) son falsos positivos del punto 3.
- "Solicitud de asesor humano atendida tarde tras varias insistencias" (C07): según la transcripción la solicitud **nunca se atendió**; el agente insistió en la fecha de pago y el cliente colgó.
- Hallazgo 1 (4/20 en verificación de identidad): en 3 de esas 4 llamadas se reveló la deuda (C02, C19, C20); la cuarta (C18) es el caso límite de la nota metodológica. A eso se suma **C03**, donde la deuda se reveló a un tercero (R3). En total, 4 de 20 llamadas expusieron información de la deuda sin una verificación válida.
- Resultado agregado con C03: 8 aprobadas, 4 aprobadas con observaciones, 8 rechazadas.

### 5. Cómo evaluar con el servicio desplegado

Antes de cualquier prueba, `GET /health` despierta el servicio (30–50 s si estaba dormido).

**Una conversación.** `POST /evaluate` con `{"conversation": {...}}`. Desde Swagger
(`/docs` → `POST /evaluate` → *Try it out*) o con el ejemplo del repo:

```bash
curl.exe -X POST https://lina-evaluation-api.onrender.com/evaluate -H "Content-Type: application/json" --data-binary "@conversacion_ejemplo.json"
```

**Todas las conversaciones.** `POST /evaluate/batch` espera `{"conversations": [...]}`,
no el archivo de la prueba tal cual (`conversaciones`). `evaluar_todas.py` hace el
reenvolvido, envía lotes de 5 con espera entre ellos, reintenta ante un 429 y
guarda `resultados_api.json`:

```bash
python evaluar_todas.py
```

### 6. Otras precisiones de este README

Ver las notas fechadas en Estructura del repo, Instalación, Uso local,
Testing y Despliegue: nombre de `env.example`, número real de tests,
comportamiento de las citas no encontradas y estado del auto-deploy.
