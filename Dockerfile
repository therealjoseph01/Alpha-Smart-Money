# One image, five entrypoints. The services differ only in the command they run, so
# building once and varying the command keeps them provably identical.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# sqlite3 for the backup/restore helpers; curl for the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        sqlite3 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first so a code change does not reinstall the world.
COPY pyproject.toml README.md ./
RUN uv venv /app/.venv && uv pip install --python /app/.venv -r pyproject.toml

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src

COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src

# The database lives on a volume shared by every service.
RUN mkdir -p /app/data /app/logs

# Never run as root: the signer holds a key, and none of these need privileges.
RUN useradd --create-home --uid 10001 asm && chown -R asm:asm /app
USER asm

CMD ["uvicorn", "asm.services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
