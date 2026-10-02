# Geo Assistant: API + map front-end.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# GeoPandas, Shapely, pyproj and pyogrio ship binary wheels bundling GEOS, PROJ
# and GDAL, so no system package is needed.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

# Real Luxembourg layers (with SOURCES.json for attribution) and the synthetic sample.
COPY data/lux ./data/lux
COPY data/sample ./data/sample

RUN useradd --create-home appuser
USER appuser

# The image is what runs as the public demo: real data, and /ask limited to
# 10 questions per hour per visitor, identified through the hosting proxy's
# X-Forwarded-For header. Set ASK_RATE_LIMIT=0 to lift the limit.
ENV DATA_DIR=data/lux \
    ASK_RATE_LIMIT=10 \
    ASK_RATE_WINDOW_S=3600 \
    TRUST_FORWARDED_FOR=true
EXPOSE 8000

# Hosting platforms such as Render provide the port to listen on in $PORT.
CMD ["sh", "-c", "uvicorn app.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
