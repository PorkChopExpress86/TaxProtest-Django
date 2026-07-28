# Deployment

Continuous deployment for TaxProtest-Django. A push to `main` triggers
[`.github/workflows/deploy.yml`](.github/workflows/deploy.yml), which SSHes into the
production server and runs [`scripts/deploy.sh`](scripts/deploy.sh) from the project
directory.

---

## How the pipeline decides what to rebuild

`scripts/deploy.sh` runs `git fetch origin main`, inspects
`git diff --name-only HEAD origin/main`, and picks the smallest safe rebuild.

| Mode | Trigger | Action |
|---|---|---|
| **full** | Core infrastructure: `docker-compose*.yml`, `Dockerfile*`, `requirements*.txt`, `pyproject.toml`, `Makefile`, `setup.sh`, `scripts/entrypoint.sh`, `data/migrations/`, `taxprotest/{settings,celery,wsgi,asgi}.py`, any `*.sql`, `.github/workflows/` | `docker compose up -d --build` |
| **full** | Any runtime file that isn't ETL or docs (views, templates, static, models) | `docker compose up -d --build` |
| **partial** | ETL surface only: `data/etl.py`, `data/etl_pipeline/`, `data/management/`, `data/tasks_new.py`, `data/residential.py`, `data/brazos_layouts.py`, `etl/`, `ingest.py`, `parsers/` | `docker compose up -d --build worker beat` |
| **skip** | Docs / non-runtime only: `*.md`, `*.rst`, `docs/`, `LICENSE`, images, `.gitignore`, `.pre-commit-config.yaml`, and the host-side scripts `scripts/deploy.sh` / `scripts/cleanup_data.sh` | `git pull` only, no restart |

Rules that keep this safe:

- **Highest tier wins.** A commit touching both `README.md` and `docker-compose.yml` gets a
  full rebuild.
- **Unrecognised paths get a full rebuild.** A new file matching no pattern is assumed to be
  runtime code rather than silently skipped.
- **`requirements*.txt` is infrastructure, not docs**, despite the `.txt` extension.
- **`scripts/entrypoint.sh` is infrastructure**, unlike the other scripts: it is baked into
  the image and runs in the container, so it does require a rebuild.
- After any rebuild, `docker image prune -f` removes dangling layers.

`docker compose up -d --build` only recreates containers whose image or config actually
changed, so a full rebuild is not a full outage of every service.

### Overrides

`scripts/deploy.sh` reads these environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `BRANCH` | `main` | Branch to deploy |
| `REMOTE` | `origin` | Git remote |
| `ETL_SERVICES` | `worker beat` | Services rebuilt in partial mode |
| `FORCE_FULL` | `0` | Set to `1` to force a full rebuild |
| `DRY_RUN` | `0` | Set to `1` to print the plan and exit without changing anything |

Preview a deploy without touching containers:

```bash
cd /path/to/TaxProtest-Django
DRY_RUN=1 ./scripts/deploy.sh
```

---

## Server setup

### 1. Generate a deploy SSH key

Generate the key **on your workstation** (not on the server) so the private half never has
to be copied off the box it was created on. Use no passphrase — GitHub Actions runs
unattended and cannot answer a prompt.

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/taxprotest_deploy -N ""
```

This produces:

- `~/.ssh/taxprotest_deploy` — private key, goes into the `SSH_PRIVATE_KEY` GitHub secret
- `~/.ssh/taxprotest_deploy.pub` — public key, goes onto the server

Install the public key for the deploy user:

```bash
ssh-copy-id -i ~/.ssh/taxprotest_deploy.pub -p 22 deployuser@your.server.host
```

Or manually, on the server:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo "ssh-ed25519 AAAA... github-actions-deploy" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Verify it works before wiring up CI:

```bash
ssh -i ~/.ssh/taxprotest_deploy -p 22 deployuser@your.server.host 'echo connection ok'
```

> **Restricting the key (optional but recommended).** Prefix the `authorized_keys` entry to
> limit what the key can do:
> ```
> restrict,pty,command="cd /srv/TaxProtest-Django && ./scripts/deploy.sh" ssh-ed25519 AAAA...
> ```
> With a forced command the key can *only* run the deploy script. Note this overrides the
> command sent by the workflow, so the path must be correct here.

### 2. Grant non-root Docker access

`scripts/deploy.sh` calls `docker` without `sudo` and aborts early with a clear error if it
cannot reach the daemon. Add the deploy user to the `docker` group:

```bash
sudo groupadd -f docker
sudo usermod -aG docker deployuser
```

Group membership is applied at login, so **the existing SSH session must be closed**. Log
out and back in, then confirm — over a fresh SSH connection, since that is how Actions will
connect:

```bash
ssh -i ~/.ssh/taxprotest_deploy deployuser@your.server.host 'docker info > /dev/null && echo docker ok'
```

> **Security note.** Membership in the `docker` group is equivalent to root on the host: any
> member can start a container that mounts `/`. Only grant it to an account you are willing
> to treat as privileged. If that is unacceptable, use
> [rootless Docker](https://docs.docker.com/engine/security/rootless/) instead.

### 3. Prepare the project directory

Clone the repository to the path you will use for `PROJECT_PATH`, as the deploy user:

```bash
sudo mkdir -p /srv && sudo chown deployuser:deployuser /srv
cd /srv
git clone https://github.com/PorkChopExpress86/TaxProtest-Django.git
cd TaxProtest-Django
git checkout main
chmod +x scripts/deploy.sh
```

Create the `.env` file the compose services read. **It is not in git** and must be created
by hand once — see `.env.example` for the full list. At minimum:

```bash
cat > .env <<'EOF'
POSTGRES_PASSWORD=<strong-random-password>
DJANGO_SECRET_KEY=<strong-random-secret>
EOF
chmod 600 .env
```

`scripts/deploy.sh` uses `git pull --ff-only`, so the checkout must stay a clean
fast-forward of `origin/main`. Local edits on the server will stop the deploy rather than be
silently overwritten. To recover:

```bash
git reset --hard origin/main
```

The BCAD download directory is a bind mount and survives container restarts. Create it with
the right ownership before the first deploy:

```bash
mkdir -p /srv/TaxProtest-Django/data/cad_downloads
```

### 4. Register GitHub repository secrets

In the repository: **Settings → Secrets and variables → Actions → New repository secret**.

| Secret | Example | Notes |
|---|---|---|
| `SERVER_HOST` | `192.0.2.10` or `deploy.example.com` | Hostname or IP, no scheme or port |
| `SERVER_USER` | `deployuser` | The account holding the public key and `docker` group membership |
| `SSH_PRIVATE_KEY` | contents of `~/.ssh/taxprotest_deploy` | **Entire file**, including the BEGIN/END lines and the trailing newline |
| `SERVER_PORT` | `22` | SSH port; the workflow defaults to `22` if unset |
| `PROJECT_PATH` | `/srv/TaxProtest-Django` | Absolute path to the checkout |

Or with the `gh` CLI:

```bash
gh secret set SERVER_HOST    --body "192.0.2.10"
gh secret set SERVER_USER    --body "deployuser"
gh secret set SERVER_PORT    --body "22"
gh secret set PROJECT_PATH   --body "/srv/TaxProtest-Django"
gh secret set SSH_PRIVATE_KEY < ~/.ssh/taxprotest_deploy
```

> `SSH_PRIVATE_KEY` is the most common source of failures. Paste the whole file — a missing
> `-----END OPENSSH PRIVATE KEY-----` line or a stripped trailing newline produces
> `error in libcrypto` or `Load key: invalid format`. Piping the file with
> `gh secret set ... < file` avoids the problem entirely.

---

## Running a deployment

Automatic on every push to `main`. To run one by hand, use **Actions → Deploy → Run
workflow** (the workflow declares `workflow_dispatch`), or on the server:

```bash
cd /srv/TaxProtest-Django && ./scripts/deploy.sh
```

A `concurrency` group serialises deploys, so two quick pushes will not rebuild on top of
each other.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Permission denied (publickey)` | Public key not in the server's `authorized_keys`, or wrong `SERVER_USER` | Re-run step 1; confirm `~/.ssh` is `700` and `authorized_keys` is `600` |
| `Load key: invalid format` / `error in libcrypto` | `SSH_PRIVATE_KEY` truncated | Re-set it with `gh secret set SSH_PRIVATE_KEY < keyfile` |
| `Host key verification failed` | `ssh-keyscan` could not reach the host | Check `SERVER_HOST`/`SERVER_PORT` and that the firewall allows GitHub runners |
| `cannot talk to the Docker daemon` | Deploy user not in the `docker` group, or session predates the change | Re-run step 2 and reconnect |
| `not a git repository` | `PROJECT_PATH` wrong or not a clone | Correct the secret; verify with `ls $PROJECT_PATH/.git` |
| `fatal: Not possible to fast-forward` | Local commits or edits on the server | `git reset --hard origin/main` |
| Deploy succeeds but changes are absent | Change classified as `skip` | Confirm with `DRY_RUN=1 ./scripts/deploy.sh`; use `FORCE_FULL=1` to override |

Inspect what happened on the server:

```bash
cd /srv/TaxProtest-Django
docker compose ps
docker compose logs -f web
docker compose logs -f worker
git log --oneline -5
```
