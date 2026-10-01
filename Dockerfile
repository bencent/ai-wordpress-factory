# Phase 8.6-1 single-host runtime image.
#
# ONE image reused by api, worker and publication-worker (Compose selects the
# command). Official Playwright Python base guarantees the Chromium build and
# OS libraries match the pinned `playwright==` package in requirements.txt.
# Browsers ship inside the base image; no separate browser install step.
#
# Tag rule: mcr.microsoft.com/playwright/python:v<playwright>-noble, where
# <playwright> MUST equal the pinned playwright version in requirements.txt.
# A wrong tag fails fast at `docker build` (manifest unknown) -- it cannot
# silently produce a mismatched runtime.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first for layer caching. Pinned exact in requirements.txt.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Application source (see .dockerignore for what stays out).
COPY . /app

# Writable runtime locations, owned by the non-root runtime user.
RUN groupadd -r app && useradd -r -g app -d /app -s /usr/sbin/nologin appuser \
    && mkdir -p /app/data /app/artifacts /app/config \
    && chown -R appuser:app /app/data /app/artifacts /app/config

USER appuser

# No CMD here: Compose supplies the per-service command
# (api / worker / publication-worker / migrate).
