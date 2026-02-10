#!/bin/bash
set -e

echo "Starting TaxProtest-Django..."

# Run migrations
echo "Running migrations..."
python manage.py migrate --noinput

# Collect static files
echo "Collecting static files..."
python manage.py collectstatic --noinput

# Check and import data
echo "Checking data..."
python manage.py check_and_import_data

# Start Gunicorn
echo "Starting web server..."
exec gunicorn taxprotest.wsgi:application --bind 0.0.0.0:8000 --workers ${WEB_CONCURRENCY:-3}
