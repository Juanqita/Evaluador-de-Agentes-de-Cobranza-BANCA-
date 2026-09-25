FROM python:3.12-slim AS base

# Usuario no-root: si el contenedor se ve comprometido, el proceso no corre
# como root dentro de él.
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py schemas.py exceptions.py observability.py rate_limiter.py gemini_evaluator.py ./
COPY rubrica_evaluacion_lina.md rubrica_evaluacion_lina.schema.json ./

USER appuser

# La API key de Gemini NUNCA se copia a la imagen: se inyecta como variable
# de entorno en tiempo de ejecución (ver .env.example / DEPLOY_SECURITY.md).
ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
