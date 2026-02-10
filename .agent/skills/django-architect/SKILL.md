---
name: django-architect
description: Expert specialized in Django 5.0+ web development, ORM optimization, and HTMX integration. Triggers when creating web apps, views, or database models.
---

# GOAL
Architect scalable Django applications using the "Fat Model, Skinny View" philosophy and efficient database queries.

# INSTRUCTIONS
1.  **Models & ORM:**
    - Use `BigIntegerField` for IDs to prevent overflow.
    - define `__str__` methods for all models.
    - ALWAYS use `select_related` (FK) and `prefetch_related` (M2M) to prevent N+1 query problems.
2.  **Views:**
    - Prefer **Class-Based Views (CBVs)** for standard CRUD operations.
    - Use **Functional Views (FBVs)** only for complex logic or HTMX partials.
3.  **Frontend Strategy:**
    - Use standard Django Templates (DTL).
    - Use **HTMX** for dynamic interactivity (swapping HTML) instead of React/Vue.
4.  **Settings:**
    - Never hardcode secrets. Use `django-environ` to read `.env` files.

# CONSTRAINTS
- Do not put business logic in Views; move it to Model methods or Services.
- Do not use `float` for currency; use `DecimalField`.