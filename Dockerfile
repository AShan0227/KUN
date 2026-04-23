# KUN API Docker image — multi-stage, Python 3.13 + uv.
# Build: docker build -t kun:dev .
# Run:   docker run --env-file .env -p 8000:8000 kun:dev

FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

WORKDIR /build
COPY pyproject.toml uv.lock* README.md ./
COPY kun ./kun
RUN uv sync --frozen --no-dev --no-editable || uv sync --no-dev --no-editable

# --- runtime image ---
FROM python:3.13-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 1001 -s /bin/bash kun

WORKDIR /app
COPY --from=builder /build/.venv /app/.venv
COPY --chown=kun:kun kun /app/kun
COPY --chown=kun:kun rules /app/rules
COPY --chown=kun:kun skills /app/skills
COPY --chown=kun:kun alembic /app/alembic
COPY --chown=kun:kun alembic.ini pyproject.toml README.md /app/

USER kun
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    KUN_ENV=production \
    KUN_API_HOST=0.0.0.0 \
    KUN_API_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://localhost:8000/health/').raise_for_status()" || exit 1

CMD ["uvicorn", "kun.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
