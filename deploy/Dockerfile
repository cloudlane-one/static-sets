# syntax=docker/dockerfile:1

# -----------------------------------------------------------------------------
# The static-sets image.
#
# One image carries both halves of the operator: the controller that decides what
# should exist, and the installer command that writes a static Pod on the node it
# runs on. They share the library and the installer's dependencies, so shipping
# them together removes any chance of the two disagreeing about the revision
# hashing or the ownership protocol.
#
#   controller: kopf run app/static_sets_operator.py   (the image's CMD)
#   installer:  static-set-installer reconcile|cleanup  (the chart passes this)
#
# Build it from the repository root, so that the build context is the project:
#
#     docker build -f deploy/Dockerfile -t ghcr.io/cloudlane-one/static-sets:0.1.0 .
#
# The container runs as an unprivileged user. Installer Pods override that with a
# Pod-level securityContext, because kubelet's manifest directory is root-owned; the
# controller itself needs no privilege.
#
# It is a two-stage build because installing the project needs tooling that must not
# end up in the image: `git`, to fetch based-operators from its repository until it
# has a PyPI release. An installer Pod runs as root on a node and mounts the manifest
# directory, so a VCS client and a compiler have no business being in its filesystem.
# -----------------------------------------------------------------------------

FROM python:3.13-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# The project derives its version from version control, and the scaffolder's
# Dockerfile copies `.git` into the context so the backend can read it. This build
# states the version instead, so the context does not have to carry the repository's
# history merely to be installed:
#
#     docker build -f deploy/Dockerfile --build-arg VERSION=$(git describe --tags) -t ...
ARG VERSION=0.0.0
ENV UV_DYNAMIC_VERSIONING_BYPASS=$VERSION

# git is required by the build, not by the image: based-operators is a direct
# dependency referenced by its Git URL until it is released on PyPI.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Install into a virtual environment: that directory is what the runtime stage gets,
# leaving the interpreter's own site-packages untouched.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY . /app/
RUN pip install --no-cache-dir .


FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# The library and the installer command, exactly as the builder installed them, with
# no compiler, no VCS client and no build cache.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# The controller is loaded by path, not imported as a package — kopf executes the file
# directly — so it has to be on disk at the path the image's CMD names.
COPY --from=builder /app/app /app/app

# The chart's securityContext runs the container as this uid, and kopf resolves the
# account when it starts: a uid with no passwd entry aborts it, so the user has to
# exist in the image.
RUN useradd --uid 65532 --user-group --create-home --shell /usr/sbin/nologin nonroot

USER 65532

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]

CMD ["kopf", "run", "app/static_sets_operator.py", "--liveness=http://0.0.0.0:8080/healthz"]
