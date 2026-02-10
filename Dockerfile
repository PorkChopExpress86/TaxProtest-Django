# Use Python 3.14 slim image
FROM python:3.14-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    postgresql-client \
    gcc \
    python3-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . /app/

# Collect static files during build so reverse proxies can serve them directly
RUN DJANGO_SECRET_KEY=dummy python manage.py collectstatic --noinput || true

# Download and extract data during build
# This increases image size but ensures data is ready on startup
RUN python scripts/build_time_download.py

# Expose port
EXPOSE 8000

# Create a non-root user and switch to it
RUN addgroup --system django && adduser --system --ingroup django django

# Chown all the files to the app user
RUN chown -R django:django /app

# Switch to the non-root user
# USER django

# Run the application with Gunicorn for production readiness
# Run the entrypoint script
ENTRYPOINT ["/app/scripts/entrypoint.sh"]