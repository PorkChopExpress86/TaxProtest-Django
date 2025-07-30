# Copilot Instructions for TaxProtest-Django

## Project Overview
- This is a Django web application for property tax protest workflows, currently being adapted for a "homevalues" web app.
- The main Django project is in `taxprotest/`. Templates are in the top-level `templates/` directory.
- The app is structured for extensibility, with future plans for background scraping and scheduled tasks.

## Key Components
- `taxprotest/`: Django project settings, URLs, and WSGI/ASGI entrypoints.
- `templates/`: Global HTML templates, including `base.html` and `index.html` (the start page).
- (Planned) `home/`: App for user-facing features, forms, and background tasks (e.g., scraping HCAD data).

## Developer Workflows
- **Run the server:** `python manage.py runserver`
- **Migrate DB:** `python manage.py migrate`
- **Create superuser:** `python manage.py createsuperuser`
- **Install dependencies:** `pip install -r requirements.txt` (if present)
- **Add new apps:** `python manage.py startapp <appname>`

## Templates & Static Files
- Templates are loaded from the top-level `templates/` directory (see `settings.py:TEMPLATES['DIRS']`).
- Use `{% extends "base.html" %}` for consistent layout and navbars (Bootstrap via CDN is recommended).

## Background Tasks
- For scheduled or background jobs (e.g., scraping), use Celery with a `tasks.py` in the app directory (e.g., `home/tasks.py`).
- Schedule monthly jobs with Celery Beat using a crontab for the first Tuesday.

## Project Conventions
- Keep all user-facing templates in the global `templates/` directory.
- Place background scripts and periodic tasks in the relevant app's `tasks.py`.
- Use Bootstrap for UI consistency.
- Reference views in `urls.py` using `from <app>.views import ...` and map the root URL to the main page view.

## Integration Points
- External scraping (e.g., HCAD) should be isolated in background tasks and not block web requests.
- Use environment variables or Django settings for secrets and configuration.

## Example File Structure
```
TaxProtest-Django/
├── manage.py
├── taxprotest/
│   ├── settings.py
│   ├── urls.py
│   └── ...
├── home/
│   ├── views.py
│   ├── tasks.py
│   └── ...
├── templates/
│   ├── base.html
│   └── index.html
```

---
If you add new conventions or workflows, update this file to keep AI agents productive.
