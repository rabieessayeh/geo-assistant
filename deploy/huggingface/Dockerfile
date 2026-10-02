# Geo Assistant on a Hugging Face Space (Docker SDK).
#
# This file is copied to the root of the Space repository by
# scripts/build_space.py, next to app/, pyproject.toml and data/lux/.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Spaces run the container as user 1000.
RUN useradd --create-home --uid 1000 user
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY app ./app
RUN pip install .

# Real Luxembourg layers with their provenance file (SOURCES.json).
COPY data/lux ./data/lux

USER user

# Non-secret configuration; override any of these with Space variables.
# LLM_API_KEY is deliberately absent: it must be set as a Space secret.
ENV DATA_DIR=data/lux \
    LLM_BASE_URL=https://api.groq.com/openai/v1 \
    LLM_MODEL=openai/gpt-oss-20b \
    LLM_MAX_RETRIES=2 \
    ASK_RATE_LIMIT=10 \
    ASK_GLOBAL_LIMIT=60 \
    ASK_RATE_WINDOW_S=3600 \
    TRUST_FORWARDED_FOR=true

EXPOSE 7860
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "7860"]
