FROM ghcr.io/astral-sh/uv:0.12.5-python3.12-trixie-slim@sha256:4677e08839853fe91c523b593f822ec1e87c7b91ba4c6b30929016b2e0933cd5
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY alembic.ini ./
RUN uv sync --locked --no-dev
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "careerforge.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
