# Deployment Guide

This project deploys automatically to a Linux production server via GitHub
Actions on every push to `main`. The workflow SSHes into the server and
executes `scripts/deploy.sh`, which classifies the diff and applies the
lightest-weight rebuild that still picks up the changes.

## 1. Server prerequisites

The remote server must have:

- A recent Linux distribution (Ubuntu 22.04+ or equivalent)
- **Git** (`apt install git`)
- **Docker Engine** 24+ with the **Compose v2 plugin** (`docker compose`)
- An unprivileged user (`SERVER_USER`) that owns the project directory
  and can invoke `docker compose` without `sudo`

## 2. Grant non-root Docker access

The deploy user must be able to run `docker compose` without root. The
canonical way is to add the user to the `docker` group:

```bash
sudo usermod -aG docker "$SERVER_USER"
# log out and back in, or:
newgrp docker
```

Verify:

```bash
sudo -u "$SERVER_USER" docker compose version
```

> If the server uses **rootless Docker** (e.g. `dockerd-rootless`),
> grant access to the per-user socket (`DOCKER_HOST=unix:///run/user/<uid>/docker.sock`)
> instead. The deploy script does not require any specific mode; it
> just shells out to `docker compose`.

## 3. Generate a deploy SSH key

Generate an ed25519 key on the server (or anywhere; only the private key
matters):

```bash
ssh-keygen -t ed25519 -C "github-deploy" -f ~/.ssh/github_deploy -N ""
```

Authorize the public key for `SERVER_USER`:

```bash
ssh-copy-id -i ~/.ssh/github_deploy.pub "$SERVER_USER@$SERVER_HOST"
```

Test:

```bash
ssh -i ~/.ssh/github_deploy "$SERVER_USER@$SERVER_HOST" 'docker compose version && git --version'
```

The server's SSH host key will be picked up automatically by the workflow
via `ssh-keyscan`; no manual `known_hosts` editing required.

## 4. Register GitHub repository secrets

In the GitHub repository, navigate to
**Settings → Secrets and variables → Actions → New repository secret**
and create the following five secrets:

| Secret | Example value | Description |
|---|---|---|
| `SERVER_HOST` | `203.0.113.10` | Hostname or IP of the production server. |
| `SERVER_USER` | `deploy` | SSH user that owns `$PROJECT_PATH`. |
| `SERVER_PORT` | `22` | SSH port (use a non-default port if you changed it). |
| `SSH_PRIVATE_KEY` | *(full key including `-----BEGIN ...` and `-----END ...` lines)* | The private half of the key generated in step 3. |
| `PROJECT_PATH` | `/srv/taxprotest` | Absolute path to the project working tree on the server. The server's `.env` file must already exist at this path. |

### Optional: skip a specific push

Add `[skip deploy]` to the start of a commit message to bypass the
workflow without disabling the action.

## 5. First-time deployment

```bash
# On the server, as $SERVER_USER
git clone <repo-url> "$PROJECT_PATH"
cd "$PROJECT_PATH"
# Copy or create the .env file at $PROJECT_PATH/.env with the
# production secrets (POSTGRES_PASSWORD, DJANGO_SECRET_KEY, ALLOWED_HOSTS, etc.)

# Trigger a manual deploy from the GitHub Actions tab (workflow_dispatch)
# or push a commit to main.
```

## 6. What `scripts/deploy.sh` actually does

1. Verifies `git` and `docker compose` are available; exits hard if not.
2. `cd "$PROJECT_PATH"` and `git fetch origin main`.
3. Computes the changed-file set via `git diff --name-only HEAD origin/main`.
4. Classifies each commit as one of:
   - **FULL** — any change to `docker-compose*.yml`, `Dockerfile`,
     `requirements*.txt`, `taxprotest/settings.py`, Django migrations,
     `counties/harris/models.py`, `counties/brazos/models.py`, templates, or static
     files. Runs `docker compose up -d --build`.
   - **PARTIAL** — only BCAD/ETL-only code changed (`counties/brazos/management/`,
     `counties/brazos/parsers/`, `counties/harris/etl_pipeline/`, `counties/harris/etl.py`).
     Runs `docker compose up -d --build etl`. If the `etl` service is
     not defined, falls back to FULL.
   - **SKIP** — only docs, comments, `.github/`, tests, or repo
     housekeeping files changed. No container restart.
5. `git pull --ff-only origin main`.
6. Executes the chosen rebuild (or no-op for SKIP).
7. `docker image prune -f` to remove dangling build layers.

Migrations and `collectstatic` are intentionally **not** run here; they
are handled by `scripts/entrypoint.sh` on container start and at Docker
build time respectively.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Permission denied` running `docker compose` on the server | `$SERVER_USER` not in the `docker` group | `sudo usermod -aG docker $SERVER_USER` and re-login |
| Workflow fails at `Validate SSH connectivity` | Wrong host/port/key/secret | Re-check secrets; re-test `ssh -i ~/.ssh/github_deploy -p $PORT $USER@$HOST` from a workstation |
| `non-fast-forward` from `git pull --ff-only` | Server's local commits ahead of `origin/main` | `git fetch origin && git reset --hard origin/main` on the server (or resolve manually) |
| Rebuild succeeds but the new code is not active in `web` | A PARTIAL classification ran; `web`/`worker`/`beat` still hold the previous image | Re-run the deploy or `docker compose up -d --no-deps web worker beat` |
| First deploy fails with `set -euo pipefail` and a cryptic line | `docker compose` plugin missing on the server | Install Compose v2: `apt install docker-compose-plugin` |
