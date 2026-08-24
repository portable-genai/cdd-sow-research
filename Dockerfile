# B1 CDD + Source-of-Wealth Agent — API service image.
#
# Builds the FastAPI service with the managed-stack extra ([gcp]) installed, so the
# deployed container talks to Document AI / Gemini / Model Armor / DLP / Cloud Logging in
# the region selected at deploy time. The image is region-agnostic at build time; residency is enforced at
# runtime via config/settings.yaml (region pinned) and the deploy environment.

# --------------------------------------------------------------------------- #
# Builder — install dependencies into a venv we can copy into a slim runtime.
# --------------------------------------------------------------------------- #
# Digest-pinned (reproducible, immune to tag re-pushes); dependabot's docker
# ecosystem proposes digest bumps. Resolved from library/python tag 3.14-slim.
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# git: needed while pip resolves the commons git+https pins (builder
# stage only; the runtime stage copies the venv and never carries git).
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md requirements-gcp.lock constraints-security.txt ./
COPY src ./src
COPY config ./config

# Locked, reproducible install: deps come from the committed lockfile (uv pip compile
# --extra gcp), then the project itself with --no-deps so the lock stays authoritative.
#
# constraints-security.txt raises packages the lock never listed — transitives and the base
# image's own ensurepip — to versions without known HIGH advisories. The lock stays
# authoritative for everything it names; the constraints file only floors what it does not.
# See that file for why a floor is not a pin and when each line should be deleted.
RUN pip install --upgrade pip \
 && pip install -c constraints-security.txt -r requirements-gcp.lock \
 && pip install -c constraints-security.txt --no-deps . \
 && pip install -c constraints-security.txt --upgrade setuptools

# --------------------------------------------------------------------------- #
# Runtime — slim, non-root, venv copied from builder.
# --------------------------------------------------------------------------- #
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    CDD_PROFILE=gcp \
    CDD_SETTINGS=/app/config/settings.yaml \
    PORT=8090

WORKDIR /app

# A digest pin freezes the base image, which means it also freezes its unpatched packages.
# Reproducible and vulnerable are not opposites, and the pin quietly guarantees the second
# while being cited as evidence of the first. Debian security updates are applied on top so
# the image is both. (org-metadata's CI runner learned the same lesson at a larger scale:
# 182 CRITICAL / 476 HIGH, every one with a fix available.)
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv
COPY src ./src
COPY config ./config

# Remove pip from the RUNTIME image, in both the system prefix and the venv.
#
# Two reasons, and the second is the one that showed up in the scan. First, a serving
# container installs nothing: shipping a package manager in it adds an install capability an
# attacker can use and the application never can. Second, pip VENDORS its dependencies —
# msgpack and setuptools live inside pip/_vendor — so a scanner reports pip's bundled copies
# as installed packages. That is where BOTH Python findings in the 2026-08-24 promotion scan
# came from (msgpack 1.1.2, setuptools 70.3.0); neither was a dependency of this application,
# neither appeared in any lockfile, and no constraint could reach them, because they were
# never resolved — they arrived inside pip itself.
#
# The venv keeps its own setuptools (raised in the builder), which some libraries still import
# as pkg_resources at runtime. Only pip goes.
RUN rm -rf /usr/local/lib/python3.14/site-packages/pip \
           /usr/local/lib/python3.14/site-packages/pip-*.dist-info \
           /opt/venv/lib/python3.14/site-packages/pip \
           /opt/venv/lib/python3.14/site-packages/pip-*.dist-info \
           /usr/local/bin/pip /usr/local/bin/pip3 /opt/venv/bin/pip /opt/venv/bin/pip3

USER appuser
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8090')+'/healthz')" || exit 1

CMD exec uvicorn cdd_sow_research.api.app:app --host 0.0.0.0 --port ${PORT}
