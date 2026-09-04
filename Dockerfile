FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl zstd \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 lean \
    && mkdir /app && chown lean:lean /app

WORKDIR /app
USER lean
COPY --chown=lean:lean lean-toolchain install_lean.sh ./
RUN sh ./install_lean.sh

COPY --chown=lean:lean *.py ./
COPY --chown=lean:lean tests/ ./tests/

# Only non-secret defaults belong in the image. Set credentials at runtime.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ELAN_HOME=/app/.elan \
    PATH=/app/.elan/bin:$PATH \
    HOST=0.0.0.0 \
    PORT=10000 \
    APP_REQUIRE_AUTH=0 \
    BENCHMARK_WORKERS=1

EXPOSE 10000
CMD ["python3", "app.py"]
