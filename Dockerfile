# Use Python 3.14 slim image
FROM python:3.14-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set work directory
WORKDIR /app

# Install system dependencies
# p7zip-full: some past-year BCAD certified archives use DEFLATE64 compression
# (confirmed: the 2023 export), which Python's stdlib zipfile cannot decompress
# (NotImplementedError) -- import_brazos_assessment_history falls back to `7z`
# for exactly those archives, trying the stdlib path first everywhere else.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    postgresql-client \
    gcc \
    python3-dev \
    libpq-dev \
    p7zip-full \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Create the app user before copying so files land correctly owned. A later
# `chown -R /app` would rewrite every file into a second copy-on-write layer,
# doubling the image; --chown on the COPY sets ownership as the layer is built.
RUN addgroup --system django && adduser --system --ingroup django django

# Copy project
COPY --chown=django:django . /app/

# Collect static files during build so reverse proxies can serve them directly.
# This runs as root, so hand the output back to django -- staticfiles/ is a
# couple of MB, unlike a recursive chown over all of /app.
RUN DJANGO_SECRET_KEY=dummy python manage.py collectstatic --noinput || true \
    && chown -R django:django /app/staticfiles

# Download and extract data during build.
# Set SKIP_DATA_DOWNLOAD=1 (via --build-arg or docker-compose build.args) to skip
# for faster dev/CI builds. Production builds should leave this at 0.
ARG SKIP_DATA_DOWNLOAD=0
RUN if [ "$SKIP_DATA_DOWNLOAD" = "0" ]; then \
        python scripts/build_time_download.py && \
        cp -r /app/counties/harris/var/downloads /hcad_downloads_baked && \
        date -u +%Y%m%dT%H%M%SZ > /hcad_downloads_baked/.build_stamp; \
    fi

# Expose port
EXPOSE 8000

# /app is already owned by django via COPY --chown above; only the build-baked
# archives (written by root in the RUN above) still need their ownership fixed.
RUN if [ -d /hcad_downloads_baked ]; then chown -R django:django /hcad_downloads_baked; fi

# Switch to the non-root user
USER django

# Run the application with Gunicorn for production readiness
# Run the entrypoint script
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
