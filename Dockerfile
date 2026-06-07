# syntax=docker/dockerfile:1
#
# Multi-stage build:
#   builder  — installs Python deps into an isolated /install prefix
#   dev      — builder + lint/test tooling (used by the taxprotest-dev service)
#   runtime  — slim production image: deps + app code, NO compiler/toolchain
#
# `runtime` is the LAST stage, so it is the default build target for the
# production services. The dev service selects `target: dev` explicitly.
#
# The base image is pinned by digest so rebuilds are reproducible and an
# upstream change can't enter silently. Refresh the digest deliberately.
FROM python:3.14-slim@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97 AS python-base

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

# ---------------------------------------------------------------------------
# builder: resolve and install runtime dependencies into /install.
# All pinned wheels are pure-binary (manylinux), so --only-binary=:all:
# guarantees no source builds — no gcc/python3-dev/libpq-dev required.
# --require-hashes rejects any artifact whose sha256 is not in the lock.
# ---------------------------------------------------------------------------
FROM python-base AS builder

COPY requirements.txt /app/
RUN pip install --no-cache-dir --prefix=/install \
    --require-hashes --only-binary=:all: \
    -r requirements.txt

# ---------------------------------------------------------------------------
# dev target: runtime deps PLUS lint/test tooling from the dev lock. Used only
# by the `taxprotest-dev` compose service and CI — never the production image.
# Build with: docker build --target dev .  (compose sets target: dev)
# It bind-mounts the source at runtime, so no app code is copied here.
# ---------------------------------------------------------------------------
FROM builder AS dev
COPY requirements-dev.txt /app/
RUN pip install --no-cache-dir --prefix=/install \
    --require-hashes --only-binary=:all: \
    -r requirements-dev.txt
# Make the installed packages/scripts importable on the default paths.
RUN cp -a /install/. /usr/local/

# ---------------------------------------------------------------------------
# runtime (default target): the production image. Bare slim + the installed
# packages copied from the builder + the application code. No build toolchain,
# no dev/test tooling, no postgresql-client (compose healthchecks use the
# postgres image's own pg_isready; nothing in the web image shells out to psql).
# ---------------------------------------------------------------------------
FROM python-base AS runtime

# Copy the resolved site-packages and console scripts from the builder.
COPY --from=builder /install /usr/local

# Copy project
COPY . /app/

# Collect static files during build so reverse proxies can serve them directly.
RUN DJANGO_SECRET_KEY=dummy python manage.py collectstatic --noinput

# Download and extract data during build.
# Set SKIP_DATA_DOWNLOAD=1 (via --build-arg or docker-compose build.args) to skip
# for faster dev/CI builds. Production builds should leave this at 0.
ARG SKIP_DATA_DOWNLOAD=0
RUN if [ "$SKIP_DATA_DOWNLOAD" = "0" ]; then \
        python scripts/build_time_download.py && \
        cp -r /app/var/downloads /hcad_downloads_baked && \
        date -u +%Y%m%dT%H%M%SZ > /hcad_downloads_baked/.build_stamp; \
    fi

# Expose port
EXPOSE 8000

# Create a non-root user with an explicit, predictable UID/GID so it matches the
# compose `user:` override (default 1000:1000) and host-owned bind mounts.
RUN addgroup --system --gid 1000 django \
    && adduser --system --uid 1000 --ingroup django django

# Chown all the files to the app user (combined to avoid extra layers)
RUN chown -R django:django /app \
    && if [ -d /hcad_downloads_baked ]; then chown -R django:django /hcad_downloads_baked; fi

# Liveness/readiness for orchestrators that honor HEALTHCHECK. Generous
# start-period: the entrypoint's first-run import path can take minutes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz/',timeout=4).status==200 else 1)" || exit 1

# Switch to the non-root user
USER django

# Run the entrypoint script (launches gunicorn for the web service)
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
