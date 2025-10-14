
# Copilot Instructions for TaxProtest-Django

## Project Overview
- Django web application for property tax protest workflows, currently being adapted for a "homevalues" web app.
- Main Django project is in `taxprotest/`. All templates are in the top-level `templates/` directory.
- The codebase is intentionally simple and extensible, with future plans for background scraping and scheduled tasks (not yet implemented).

## Architecture & Key Components
- `taxprotest/`: Contains Django settings, URL routing, and the main view (`views.py`).
- `templates/`: All user-facing HTML templates. `base.html` provides the Bootstrap-based layout; `index.html` is the main entry page and extends `base.html`.
- No custom Django apps or background jobs are present yet. If adding new features, follow Django's app structure and keep templates global.

## Developer Workflows
- **Run the server:** `python manage.py runserver`
- **Migrate DB:** `python manage.py migrate`
- **Create superuser:** `python manage.py createsuperuser`
- **Install dependencies:** `pip install -r requirements.txt` (if present)
- **Add new apps:** `python manage.py startapp <appname>` (none exist yet)

## Templates & UI Patterns
- Templates are loaded from the top-level `templates/` directory (see `settings.py:TEMPLATES['DIRS']`).
- Always use `{% extends "base.html" %}` for new templates to ensure consistent Bootstrap layout and navigation.
- UI uses Bootstrap 5 via CDN (see `base.html`).

## Views & Routing
- Main view is `taxprotest/views.py:index`, mapped to the root URL in `taxprotest/urls.py`.
- Add new views to `taxprotest/views.py` or a new app's `views.py` as needed, and update `urls.py` accordingly.

## Project Conventions
- All user-facing templates go in the global `templates/` directory.
- Use Bootstrap for all UI components.
- Reference views in `urls.py` using `from <app>.views import ...` and map URLs explicitly.
- Use environment variables or Django settings for secrets/configuration (see `settings.py`).

## Background Tasks (Planned)
- No Celery or background jobs are implemented yet. If adding, use a `tasks.py` in the relevant app and keep jobs non-blocking.

## Example File Structure
```
TaxProtest-Django/
├── manage.py
├── taxprotest/
│   ├── settings.py
│   ├── urls.py
│   ├── views.py
│   └── ...
├── templates/
│   ├── base.html
│   └── index.html
```

---
If you add new conventions, workflows, or apps, update this file to keep AI agents productive.
