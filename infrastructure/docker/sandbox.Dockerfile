# Sandbox image for agent-generated code. Build from the repository root:
#   docker build -f infrastructure/docker/sandbox.Dockerfile -t aieos-sandbox:local .
# No secrets, no package-manager caches; runs as nobody with no network at runtime.
# REPLACE_WITH_DIGEST_PIN: pin the base image by digest before publishing.
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install --yes --no-install-recommends git coreutils make \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "pytest>=8,<9" "ruff>=0.8,<1" "mypy>=1.13,<2"
# Mounted as tmpfs at runtime; created here so the path exists with the right mode.
RUN mkdir -p /workspace && chmod 1777 /workspace
USER 65534:65534
WORKDIR /workspace
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1
CMD ["sleep", "infinity"]
