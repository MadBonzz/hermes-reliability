# Build with the pinned agent checkout as context, never the benchmark repository.
FROM python:3.12-slim
ARG AGENT
ARG SOURCE_REVISION
LABEL org.agent-evaluation.runtime="${AGENT}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash git tmux curl ca-certificates ripgrep build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY . /opt/agent-source
RUN if [ "$AGENT" = "openhands" ]; then \
      pip install /opt/agent-source/openhands-sdk /opt/agent-source/openhands-tools; \
    elif [ "$AGENT" = "deerflow" ]; then \
      pip install /opt/agent-source/backend/packages/extension-api \
                  /opt/agent-source/backend/packages/harness; \
    else echo "Unsupported agent runtime" >&2; exit 2; fi
RUN useradd --create-home --uid 10001 worker \
    && mkdir -p /workspace && chown worker:worker /workspace
USER worker
WORKDIR /workspace
# The controller supplies the bridge at container creation. No host project,
# provider credentials or Docker socket belong in this image or task workspace.
CMD ["python", "-u", "/opt/agent_worker.py"]
