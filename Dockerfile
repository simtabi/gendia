# syntax=docker/dockerfile:1.7
# Multi-stage build: small final image with only what gendia needs at runtime.

FROM python:3.12-slim AS builder
WORKDIR /src
RUN pip install --no-cache-dir hatchling
COPY . .
RUN python -m hatchling build --target wheel

FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/simtabi/gendia"
LABEL org.opencontainers.image.description="CI/CD-friendly Packagist (and npm + PyPI) dev-workflow handler for polyrepo ecosystems"
LABEL org.opencontainers.image.licenses="MIT"

# Runtime deps only: git for repo ops, openssh-client for git over SSH,
# ca-certs for TLS, and bash for the gendia-env helper script.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        openssh-client \
        ca-certificates \
        bash \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /src/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -rf /tmp/*.whl

# Ship the env-loader bridge inside the image for Docker / Compose / k8s use.
COPY bin/gendia-env /usr/local/bin/gendia-env
RUN chmod +x /usr/local/bin/gendia-env

# XDG-compliant config location; mount your config volume here.
ENV XDG_CONFIG_HOME=/config \
    GENDIA_ENV_FILE=/config/.env \
    GENDIA_LOG_FORMAT=json \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Non-root user for VPS deployment best practice.
RUN useradd --create-home --shell /bin/bash gendia
USER gendia
WORKDIR /work

# `gendia-env` sources the mounted .env (resolving any *_FILE Docker secrets
# into plain values) and execs gendia. Override CMD to invoke a specific verb.
ENTRYPOINT ["gendia-env", "gendia"]
CMD ["--help"]
