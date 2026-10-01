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

# The synthetic sample layers make the image usable out of the box;
# mount real data on /app/data and set DATA_DIR to use it instead.
COPY data/sample ./data/sample

RUN useradd --create-home appuser
USER appuser

ENV DATA_DIR=data/sample
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)"

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
