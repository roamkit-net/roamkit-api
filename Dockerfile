# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements/base.txt requirements/
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements/base.txt

FROM python:3.12-slim AS runtime

WORKDIR /app

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 roamkit \
    && useradd --uid 1000 --gid roamkit --create-home roamkit

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* \
    && rm -rf /wheels

COPY --chown=roamkit:roamkit manage.py .
COPY --chown=roamkit:roamkit src/ src/

ARG ROAMKIT_GIT_SHA=
ARG ROAMKIT_BUILD_DATE=
ARG ROAMKIT_IMAGE_TAG=
ARG ROAMKIT_ENVIRONMENT=

ENV PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ROAMKIT_GIT_SHA=${ROAMKIT_GIT_SHA} \
    ROAMKIT_BUILD_DATE=${ROAMKIT_BUILD_DATE} \
    ROAMKIT_IMAGE_TAG=${ROAMKIT_IMAGE_TAG} \
    ROAMKIT_ENVIRONMENT=${ROAMKIT_ENVIRONMENT}

USER roamkit

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health/live || exit 1

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60"]
