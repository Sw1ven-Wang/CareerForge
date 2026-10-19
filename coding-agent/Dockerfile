# Node belongs only to the model controller Worker, never the command sandbox.
# Reuse the Python base and verify official Node tarballs, avoiding a Docker Hub dependency.
FROM ghcr.io/astral-sh/uv:0.12.5-python3.12-trixie-slim@sha256:4677e08839853fe91c523b593f822ec1e87c7b91ba4c6b30929016b2e0933cd5 AS pi
COPY coding-agent/install_node.py /tmp/install_node.py
RUN python /tmp/install_node.py && /opt/node/bin/node --version
ENV PATH="/opt/node/bin:$PATH"
WORKDIR /opt/careerforge/pi
COPY coding-agent/package.json coding-agent/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY coding-agent/runner.mjs ./

FROM ghcr.io/astral-sh/uv:0.12.5-python3.12-trixie-slim@sha256:4677e08839853fe91c523b593f822ec1e87c7b91ba4c6b30929016b2e0933cd5
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/src ./src
COPY backend/alembic.ini ./
RUN uv sync --locked --no-dev
COPY --from=pi /opt/node/bin/node /usr/local/bin/node
COPY --from=pi /opt/careerforge/pi /opt/careerforge/pi
ENV PATH="/app/.venv/bin:$PATH" CAREERFORGE_PI_RUNTIME_DIR=/opt/careerforge/pi
RUN node --version && node --check /opt/careerforge/pi/runner.mjs
CMD ["celery", "-A", "careerforge.infrastructure.queue.celery_app:app", "worker", "-Q", "careerforge", "--loglevel=INFO", "--concurrency=2", "--uid=65534"]
