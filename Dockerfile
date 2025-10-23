# Use Python 3.11 slim image
FROM python:3.11-slim

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

# Expose port
EXPOSE 8000

# Run the application with Gunicorn for production readiness
CMD ["gunicorn", "taxprotest.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]