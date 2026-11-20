ARG NODE_IMAGE=node:24-bookworm-slim
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${NODE_IMAGE} AS node
FROM ${PYTHON_IMAGE} AS backend
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 libatomic1 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
WORKDIR /app
COPY pyproject.toml LICENSE constraints-python312-linux.txt constraints-langgraph-python312-linux.txt ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic
COPY coding-agent/package.json coding-agent/package-lock.json ./coding-agent/
RUN python -m pip install --no-cache-dir -c constraints-python312-linux.txt '.[production]' && \
    npm ci --prefix coding-agent --ignore-scripts --no-audit --no-fund
COPY coding-agent/runner.mjs ./coding-agent/runner.mjs
COPY deploy/backend_entry.py ./deploy/backend_entry.py
RUN node --check coding-agent/runner.mjs && mkdir -p /data && chown 65534:65534 /data
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/data CAREERFORGE_STATE_DIR=/data CAREERFORGE_PI_RUNTIME_DIR=/app/coding-agent
USER 65534:65534
EXPOSE 8000
ENTRYPOINT ["python", "/app/deploy/backend_entry.py"]
CMD ["careerforge", "serve", "--host", "0.0.0.0"]

FROM backend AS validation
USER 0:0
RUN python -m pip install --no-cache-dir -c constraints-langgraph-python312-linux.txt '.[dev,production,workflows]' pytest-cov
COPY tests ./tests
COPY scripts ./scripts
COPY deploy ./deploy
COPY compose.yaml compose.test.yaml .env.example ./
ENV PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
USER 65534:65534
CMD ["python", "-m", "pytest", "-p", "xdist.plugin", "-n", "auto", "--maxprocesses=8", "--basetemp=/data/pytest-offline", "-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider", "-m", "not production and not live and not sandbox", "-q"]

# A plain docker build yields production, not the test image.
FROM backend AS runtime
