---
name: create-tests
description: Describe when to use this prompt
---

<!-- Tip: Use /create-prompt in chat to generate content with agent assistance -->


```md
Run tests inside the dev container:

```bash
docker compose run --rm taxprotest-dev pytest -q
docker compose run --rm taxprotest-dev ruff check .
docker compose run --rm taxprotest-dev mypy src