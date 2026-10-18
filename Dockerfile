# One image for local runs and a Hugging Face Docker Space: port $PORT (7860), uid 1000, writable var/.
# Dependencies come from pyproject.toml only, with the `graph` extra (GraphRAG-SDK, Graphiti, hnswlib).
# The GraphRAG-SDK git pin is installed as written; this file does not replace it with a PyPI release.
# GraphRAG's prose chunker needs tiktoken encodings on disk; `nwb cache-tokenizers` downloads the names in
# config/graph.yaml while the build still has network.
FROM python:3.13-slim-bookworm

ARG NWB_EXTRAS=graph

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860 \
    HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    XDG_CACHE_HOME=/home/user/app/var/cache \
    HF_HOME=/home/user/app/var/cache/huggingface \
    FASTEMBED_CACHE_PATH=/home/user/app/var/cache/fastembed \
    TIKTOKEN_CACHE_DIR=/home/user/app/var/tiktoken

# Build-only tools: git for the GraphRAG-SDK pin, a compiler for hnswlib and falkordblite.
# They are purged once the wheels exist so the image stays small.
RUN useradd --create-home --uid 1000 --shell /bin/bash user \
    && apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates build-essential git

WORKDIR /home/user/app

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY docker/install_deps.sh docker/install_deps.sh
RUN chmod 755 docker/install_deps.sh && ./docker/install_deps.sh "$NWB_EXTRAS" \
    && python -m nwb cache-tokenizers \
    && apt-get purge -y --auto-remove build-essential git \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

COPY --chown=user:user config ./config
COPY --chown=user:user prompts ./prompts
COPY --chown=user:user agent ./agent
COPY --chown=user:user migrations ./migrations
COPY --chown=user:user demo ./demo
COPY --chown=user:user data ./data
COPY --chown=user:user scripts/deploy_smoke.py scripts/deploy_smoke.py
COPY --chown=user:user docker/entrypoint.sh docker/entrypoint.sh
# Reinstall the package only, so a code change does not rebuild the dependency wheels.
COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
    && mkdir -p var/logs var/blobs var/extractions var/cache \
    && chown -R user:user /home/user/app \
    && chmod 755 docker/entrypoint.sh

USER user
EXPOSE 7860
CMD ["./docker/entrypoint.sh"]
