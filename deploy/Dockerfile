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
# The container runs as an unprivileged user by default. Installer Pods override
# that with a Pod-level securityContext, because kubelet's manifest directory is
# root-owned; the controller itself needs no privilege.
# -----------------------------------------------------------------------------

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# The project derives its version from version control, so it is installed from the
# full context with git available.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY . /app/
RUN pip install --no-cache-dir .

# The chart's securityContext runs the controller as this uid, and kopf resolves
# the account when it starts: a uid with no passwd entry aborts it, so the user has
# to exist in the image.
RUN useradd --uid 65532 --user-group --create-home --shell /usr/sbin/nologin nonroot

USER 65532

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]

CMD ["kopf", "run", "app/static_sets_operator.py", "--liveness=http://0.0.0.0:8080/healthz"]