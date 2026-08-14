---
name: security-review
description: >-
  Audits projects and Git repositories for leaked secrets, API keys, credentials, PII, personal emails,
  sensitive information, and uncommitted or committed .env files. Plans and executes remediation,
  Git history purging (git-filter-repo), secret rotation, and guides complete .env backup and recovery
  using the Bitwarden CLI (bw) and Bitwarden MCP server.
---

# Security Review & Secret Mitigation Skill

A comprehensive methodology and runbook for auditing codebases, configuration files, and full Git revision histories for exposed secrets, personal identifiable information (PII), and sensitive metadata, followed by strict containment, rotation, history purging, and secure secret backup in Bitwarden via CLI and MCP.

---

## Core Principles

1. **Defense-in-Depth & Zero Trust**: Treat any secret that touches a Git commit or unencrypted repository as potentially compromised.
2. **Assume Breach on Public Exposure**: If a secret was committed to a repository that is or ever was public, assume it was scraped instantly. Purging Git history is necessary for repository cleanliness, but **rotation and revocation of the credential is the only true remediation**.
3. **Never Display Full Secret Values in Logs or Outputs**: When reporting findings, redact secret payloads (e.g. `sk_live_...48f9` or `ghp_...XXXXX`) to prevent re-leaking credentials during reviews.
4. **Preserve Operational Safety**: Always back up the repository (`git clone --mirror`) and current `.env` files to an encrypted vault (like Bitwarden) *before* performing any destructive Git history rewrites.

---

## 6-Phase Audit & Mitigation Workflow

```mermaid
graph TD
    A[Phase 1: Working Tree & Untracked Scan] --> B[Phase 2: Git History & Reflog Deep Scan]
    B --> C[Phase 3: PII & Information Leakage Audit]
    C --> D[Phase 4: Threat Assessment & Mitigation Plan]
    D --> E[Phase 5: History Purge & Secret Rotation]
    E --> F[Phase 6: .env Backup via Bitwarden CLI / MCP]
```

---

## Phase 1: Working Tree & Staged File Secret Scan

Inspect the current working directory, untracked files, staged commits, configuration files, and local build artifacts.

### 1. Identify High-Risk File Targets
Search for sensitive files that should never be tracked:
- `.env`, `.env.local`, `.env.production`, `.env.staging`, `.env.test`, `.env.backup`
- `*.pem`, `*.key`, `*.pkcs12`, `*.pfx`, `*.p12`, `id_rsa`, `id_ed25519`
- `*.kdbx`, `*.credentials`, `credentials.json`, `service-account*.json`
- `*.sqlite3`, `*.db`, `*.sql`, `*.dump`, `*.tar.gz`, `*.zip` (unintended database backups)
- `docker-compose.override.yml`, `local_settings.py`, `secrets.yaml`

### 2. Scan for High-Entropy and Pattern-Matched Secrets
Run targeted regex searches using grep/ripgrep across the codebase (see [Secret Patterns Reference](./references/secret_patterns.md)):
- **API Keys & Cloud Credentials**: AWS (`AKIA[0-9A-Z]{16}`), Google/Gemini (`AIza[0-9A-Za-z\\-_]{35}`), OpenAI (`sk-[a-zA-Z0-9_-]{32,}`), Anthropic (`sk-ant-[a-zA-Z0-9_-]+`), Stripe (`sk_live_[0-9a-zA-Z]{24}`), GitHub Tokens (`ghp_[a-zA-Z0-9]{36}`, `github_pat_[a-zA-Z0-9_]{82}`).
- **Private Keys**: `-----BEGIN (RSA|EC|DSA|OPENSSH|PGP) PRIVATE KEY-----`.
- **Database Connection Strings**: `postgres://user:password@host`, `mysql://`, `redis://:password@host`, `mongodb+srv://`.
- **Hardcoded Framework Secrets**: Django `SECRET_KEY`, JWT signing secrets, Flask `app.secret_key`, Express session secrets.

---

## Phase 2: Git History & Reflog Deep Scan

Secrets are frequently committed and later "deleted" in subsequent commits, remaining permanently accessible in the Git object database and commit history.

### 1. Scan Entire Commit History for Secret Patterns
Use `git log -p` and `git log -S` (pickaxe search) to detect when sensitive tokens entered the history:
```bash
# Search commit diffs for known secret keywords and regexes
git log -p -G"(API_KEY|SECRET_KEY|PASSWORD|PRIVATE KEY|BEGIN RSA|ACCESS_KEY)" --all

# Search for any commit that touched .env files
git log --all --full-history -- "**.env*" "**/*.key" "**/*.pem" "**/*credentials*"
```

### 2. Inspect Untracked Stashes and Dangling Objects
```bash
# Check git stashes for uncommitted sensitive changes
git stash list
git stash show -p

# Identify deleted large files or blobs that may contain dumps or archives
git rev-list --objects --all | sort -k 2 | tail -n 50
```

---

## Phase 3: PII & Information Leakage Audit

Review code, documentation, scripts, and commit metadata for personal identifying information that could be exposed publicly:

1. **Commit Author & Committer Names/Emails**:
   ```bash
   git log --format="%an <%ae>" | sort -u
   ```
   Check for personal email addresses (`@gmail.com`, personal domains) that should be replaced with a noreply email (e.g. `username@users.noreply.github.com`).

2. **Hardcoded User Paths & Hostnames**:
   Search for `/home/username`, `C:\Users\username`, internal hostnames (`*.internal`, `192.168.*`, `10.*`), staging servers, and internal developer notes.

3. **Sample Data & Test Fixtures**:
   Verify that test fixtures and mock datasets do not contain real user names, phone numbers, addresses, social security numbers, or live customer records.

---

## Phase 4: Threat Assessment & Mitigation Planning

Categorize every finding by severity and formulate an actionable mitigation plan:

| Severity | Definition | Examples | Immediate Action |
|---|---|---|---|
| 🚨 **CRITICAL** | Live production secret, private key, or cloud root credential currently active or in public history. | AWS root key, production DB password, Stripe live key, active SSH private key. | Revoke & rotate immediately; purge Git history; check cloud audit logs. |
| ⚠️ **HIGH** | Active development/staging secret, un-revoked third-party API key, or committed `.env` file in private repo. | Staging API key, OpenAI token, Django `SECRET_KEY`, SMTP credentials. | Invalidate credential; add to `.gitignore`; purge from history. |
| 🟡 **MEDIUM** | Personal email in public commit metadata, internal IP/host leakage, hardcoded local absolute paths. | Personal email in git history, internal staging domain, `/home/user/` paths. | Rewrite commit authors if needed; replace hardcoded paths with relative configs. |
| ℹ️ **LOW** | Placeholder secrets in `.env.example`, expired test keys, false positive test mocks. | `SECRET_KEY=change-me`, dummy test tokens. | Document as sanitized placeholder in `.env.example`. |

---

## Phase 5: History Purge & Secret Rotation Runbook

Follow this strict runbook when secrets must be eradicated from Git history (see [Git History Purge Guide](./references/git_history_purge.md) for full instructions).

### Step 1: Immediate Secret Revocation & Rotation
> [!CAUTION]
> Rewriting Git history does NOT invalidate a leaked key. If a key was ever pushed, it must be revoked and regenerated in the service provider's console first.

1. Generate a new credential in the external provider (AWS, Stripe, OpenAI, GitHub, Database).
2. Update the local `.env` and production vault with the new credential.
3. Test connectivity with the new credential.
4. Revoke and delete the old compromised credential.

### Step 2: Create a Complete Safety Backup
```bash
# Create a mirror clone as a backup before rewriting history
git clone --mirror . ../repo-safety-backup.git
```

### Step 3: Purge Sensitive Files / Text with `git-filter-repo`
Use the modern, Python-based `git-filter-repo` tool (recommended by Git maintainers over `git filter-branch` or BFG):
```bash
# To purge a specific file (e.g. .env or credentials.json) across all commits:
git-filter-repo --path .env --invert-paths --force

# To replace sensitive text strings in place across all commits:
# Create a replacement file `replace.txt` with format: sensitive_string==>REDACTED
git-filter-repo --replace-text replace.txt --force
```

### Step 4: Verify and Force-Push Safely
```bash
# Force-push the sanitized history to all branches and tags
git push origin --force --all
git push origin --force --tags
```

### Step 5: Harden `.gitignore` and Pre-commit Hooks
Ensure `.gitignore` contains all sensitive patterns and enforce pre-commit secret scanning:
```gitignore
# Environment and Secrets
.env
.env.*
!.env.example
*.pem
*.key
*.pfx
*.p12
*.credentials
```

---

## Phase 6: `.env` Backup & Secret Management via Bitwarden (CLI & MCP)

A secure `.env` file should never live in version control. Instead, store and manage development and production secrets in **Bitwarden** using the **Bitwarden CLI (`bw`)** and **Bitwarden MCP server** (see [Bitwarden .env Guide](./references/bitwarden_env_guide.md)).

### 1. Bitwarden CLI Setup & Authentication

Install the Bitwarden CLI:
```bash
# Via bun
bun add -g @bitwarden/cli

# Or via npm
npm install -g @bitwarden/cli
```

**Interactive Agent Flow (GUI Password Prompt)**:
When backing up secrets, the agent checks `bw status`. If unauthenticated, the user is prompted to run `bw login` once in their terminal. If locked, the agent prompts for the master password via `zenity`:

```bash
# Prompt master password via secure GUI dialog
BW_PASSWORD=$(zenity --password --title="Bitwarden Master Password" --text="Enter master password to unlock vault:" 2>/dev/null)

# Unlock vault and obtain session key
BW_SESSION=$(bw unlock "$BW_PASSWORD" --raw)
```

### 2. Backing Up `.env` to Bitwarden

#### Option A: Store as a Secure Note (Targeted to "Environment files" folder)
```bash
PROJECT_NAME="TaxProtest-Django"
FOLDER_NAME="Environment files"
ITEM_NAME="$PROJECT_NAME - .env (Production/Dev)"
ENV_CONTENT=$(cat .env)

# Sync and resolve target folder ID
bw sync --session "$BW_SESSION" >/dev/null
FOLDER_ID=$(bw list folders --session "$BW_SESSION" | jq -r --arg f "$FOLDER_NAME" '.[] | select(.name | ascii_downcase == ($f | ascii_downcase)) | .id' | head -n 1)

# Encode and create/edit secure note item in Bitwarden
PAYLOAD=$(jq -n \
  --arg name "$ITEM_NAME" \
  --arg notes "$ENV_CONTENT" \
  --arg folder "${FOLDER_ID:-null}" \
  '{"type": 2, "name": $name, "notes": $notes, "secureNote": {"type": 0}} | if $folder != "null" and $folder != "" then .folderId = $folder else . end')

echo "$PAYLOAD" | bw encode | bw create item --session "$BW_SESSION"
```

#### Option B: Store as a Structured Item with Custom Fields
Store individual key-value pairs (e.g., `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD`, `DATABASE_URL`) as individual custom fields on a single Bitwarden item for granular programmatic retrieval:
```bash
# Using the automated helper script or bw create with custom fields
bw get template item | jq \
  --arg name "$PROJECT_NAME - Secrets" \
  '.type = 1 | .name = $name | .fields = [
    {"name": "DJANGO_SECRET_KEY", "value": "secret-value", "type": 1},
    {"name": "POSTGRES_PASSWORD", "value": "db-password", "type": 1}
  ]' | bw encode | bw create item
```

### 3. Restoring or Injecting `.env` from Bitwarden

#### Pulling `.env` on a Fresh Machine:
```bash
# Retrieve notes from the secure note and write to .env
bw get item "TaxProtest-Django - .env (Production/Dev)" | jq -r '.notes' > .env
chmod 600 .env
```

#### Running Application with Dynamic Secret Injection:
Avoid writing plaintext `.env` files to disk entirely by streaming variables directly into the process environment:
```bash
# Run command with secrets injected into environment
env $(bw get item "TaxProtest-Django - Secrets" | jq -r '.fields[] | "\(.name)=\(.value)"') python manage.py runserver
```

### 4. Bitwarden MCP Integration for AI Pair Programming

When working with Antigravity or AI assistants, integrate the Bitwarden MCP server (`bw-mcp` or Bitwarden Secrets Manager MCP) into `mcp_config.json`:

```json
{
  "mcpServers": {
    "bitwarden": {
      "command": "npx",
      "args": ["-y", "@bitwarden/mcp-server"],
      "env": {
        "BW_SESSION": "YOUR_SESSION_KEY_OR_TOKEN"
      }
    }
  }
}
```

With Bitwarden MCP configured:
- The agent can securely look up configuration keys without requiring local `.env` files in git.
- The user can instruct the agent to save newly generated keys directly into Bitwarden vault items.
- Secrets stay in the encrypted vault and never leak into git diffs or public transcripts.

---

## Detailed References

For in-depth procedures, consult the bundled reference guides:
- 📖 [Secret Patterns Reference](./references/secret_patterns.md) — Comprehensive regexes for cloud, SaaS, AI, and crypto keys.
- 🧹 [Git History Purge Guide](./references/git_history_purge.md) — Step-by-step `git-filter-repo` and BFG runbook.
- 🔐 [Bitwarden .env Guide](./references/bitwarden_env_guide.md) — Complete Bitwarden CLI and MCP integration manual.
