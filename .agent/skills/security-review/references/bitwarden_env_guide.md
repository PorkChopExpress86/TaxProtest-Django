# Bitwarden `.env` Backup & Secret Management Guide

This guide provides an end-to-end walkthrough for securely storing, backing up, restoring, and injecting `.env` configurations using the **Bitwarden CLI (`bw`)** and the **Bitwarden MCP server**.

---

## 1. Bitwarden CLI Setup & Authentication

### Installation
Install the official Bitwarden CLI:

```bash
# Via npm (global)
npm install -g @bitwarden/cli

# Or via bun
bun add -g @bitwarden/cli

# Verify installation
bw --version
```

### Authentication & Vault Unlock
```bash
# 1. Log in with your email
bw login

# 2. Unlock your vault and capture the session key
export BW_SESSION=$(bw unlock --raw)

# 3. Synchronize with the cloud vault
bw sync
```

> [!TIP]
> Add a helper function to your `~/.bashrc` or `~/.zshrc` to quickly unlock Bitwarden in any terminal session:
> ```bash
> bw-unlock() {
>   export BW_SESSION=$(bw unlock --raw)
>   echo "Bitwarden unlocked for this session."
> }
> ```

---

## 2. `.env` Backup Strategies in Bitwarden

### Strategy A: Full File Backup as a Secure Note (Fastest)

Ideal for backing up complete multi-line `.env` files with comments and formatting preserved.

#### Creating the Backup:
```bash
PROJECT_NAME="TaxProtest-Django"
ENV_FILE=".env"

if [ -f "$ENV_FILE" ]; then
  ITEM_NAME="$PROJECT_NAME - Environment Backup (.env)"
  CONTENT=$(cat "$ENV_FILE")

  # Build JSON payload
  PAYLOAD=$(bw get template item | jq \
    --arg name "$ITEM_NAME" \
    --arg notes "$CONTENT" \
    '.type = 2 | .name = $name | .notes = $notes')

  # Encode and create in vault
  echo "$PAYLOAD" | bw encode | bw create item
  echo "Successfully backed up $ENV_FILE to Bitwarden item: $ITEM_NAME"
fi
```

#### Restoring the `.env` from Bitwarden:
```bash
PROJECT_NAME="TaxProtest-Django"
ITEM_NAME="$PROJECT_NAME - Environment Backup (.env)"

bw get item "$ITEM_NAME" | jq -r '.notes' > .env
chmod 600 .env
echo "Restored .env from Bitwarden."
```

---

### Strategy B: Structured Item with Key-Value Custom Fields

Ideal when individual variables need to be accessed programmatically or injected into processes without creating physical `.env` files on disk.

#### Creating a Structured Item:
```bash
PROJECT_NAME="TaxProtest-Django"
ITEM_NAME="$PROJECT_NAME - Project Secrets"

# Read key=value lines from .env and convert to Bitwarden custom field objects
FIELDS_JSON=$(python3 -c '
import json, re

fields = []
with open(".env") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        # Strip outer quotes if present
        v = v.strip("\"'\''")
        fields.append({"name": k.strip(), "value": v, "type": 1}) # type 1 = hidden/password

print(json.dumps(fields))
')

PAYLOAD=$(bw get template item | jq \
  --arg name "$ITEM_NAME" \
  --argjson fields "$FIELDS_JSON" \
  '.type = 1 | .name = $name | .login.username = "app" | .fields = $fields')

echo "$PAYLOAD" | bw encode | bw create item
echo "Created structured secrets item: $ITEM_NAME"
```

#### Programmatic Secret Injection (Zero-Disk Plaintext):
Inject secrets directly into your application process environment without saving a `.env` file to disk:

```bash
# Run Django server with secrets loaded dynamically from Bitwarden
env $(bw get item "TaxProtest-Django - Project Secrets" | jq -r '.fields[] | "\(.name)=\(.value)"') python manage.py runserver
```

---

## 3. Automated Backup & Restore Scripts

You can save these scripts in your repository under `scripts/` (ensure `scripts/` is executable):

### Backup Script (`scripts/backup_env_to_bitwarden.sh`):
```bash
#!/usr/bin/env bash
set -euo pipefail

if [ -z "${BW_SESSION:-}" ]; then
  echo "Error: BW_SESSION is not set. Run 'export BW_SESSION=\$(bw unlock --raw)' first."
  exit 1
fi

PROJECT_NAME=$(basename "$PWD")
ITEM_NAME="$PROJECT_NAME - .env Backup"
FOLDER_NAME="Environment files"

if [ ! -f ".env" ]; then
  echo "Error: .env file not found in current directory."
  exit 1
fi

echo "Syncing vault..."
bw sync >/dev/null

# Lookup target folder ID
FOLDER_ID=$(bw list folders | jq -r --arg f "$FOLDER_NAME" '.[] | select(.name | ascii_downcase == ($f | ascii_downcase)) | .id' | head -n 1)

EXISTING_ID=$(bw list items --search "$ITEM_NAME" | jq -r '.[0].id // empty')

if [ -n "$EXISTING_ID" ]; then
  echo "Updating existing vault item ($EXISTING_ID)..."
  UPDATED_ITEM=$(bw get item "$EXISTING_ID" | jq \
    --arg notes "$(cat .env)" \
    --arg folder "${FOLDER_ID:-null}" \
    '.notes = $notes | if $folder != "null" and $folder != "" then .folderId = $folder else . end')
  echo "$UPDATED_ITEM" | bw encode | bw edit item "$EXISTING_ID" >/dev/null
else
  echo "Creating new vault item in folder '$FOLDER_NAME'..."
  NEW_ITEM=$(bw get template item | jq \
    --arg name "$ITEM_NAME" \
    --arg notes "$(cat .env)" \
    --arg folder "${FOLDER_ID:-null}" \
    '.type = 2 | .name = $name | .notes = $notes | if $folder != "null" and $folder != "" then .folderId = $folder else . end')
  echo "$NEW_ITEM" | bw encode | bw create item >/dev/null
fi

echo "✓ Successfully backed up .env to Bitwarden: $ITEM_NAME"
```

### Restore Script (`scripts/restore_env_from_bitwarden.sh`):
```bash
#!/usr/bin/env bash
set -euo pipefail

if [ -z "${BW_SESSION:-}" ]; then
  echo "Error: BW_SESSION is not set. Run 'export BW_SESSION=\$(bw unlock --raw)' first."
  exit 1
fi

PROJECT_NAME=$(basename "$PWD")
ITEM_NAME="$PROJECT_NAME - .env Backup"

echo "Retrieving .env from Bitwarden..."
NOTES=$(bw list items --search "$ITEM_NAME" | jq -r '.[0].notes // empty')

if [ -z "$NOTES" ]; then
  echo "Error: No backup item found with name '$ITEM_NAME'."
  exit 1
fi

echo "$NOTES" > .env
chmod 600 .env
echo "✓ .env successfully restored (permissions set to 600)."
```

---

## 4. Bitwarden Model Context Protocol (MCP) Integration

The Model Context Protocol (MCP) allows your AI pair programmer (Antigravity, Claude Code, etc.) to query secrets and vault items on-demand with fine-grained access control.

### MCP Configuration

Add the Bitwarden MCP server to your MCP configuration file (e.g. `~/.gemini/antigravity-cli/mcp_config.json` or project `.agents/mcp_config.json`):

```json
{
  "mcpServers": {
    "bitwarden": {
      "command": "npx",
      "args": ["-y", "@bitwarden/mcp-server"],
      "env": {
        "BW_SESSION": "${BW_SESSION}"
      }
    }
  }
}
```

### Using Bitwarden MCP in Workflows
When the Bitwarden MCP server is active:
1. **Secret Resolution**: The agent can ask the Bitwarden MCP tool to retrieve specific configuration parameters (e.g., API keys, database connection strings) during local testing without reading local plaintext files.
2. **Key Generation & Storage**: When generating new keys (e.g. `DJANGO_SECRET_KEY`), the agent can store the newly created key directly into your Bitwarden vault item.
3. **Audit Compliance**: Prevents API keys from entering conversation histories, tool inputs, or git diffs.

---

## 5. Bitwarden Secrets Manager (`bws`) CLI

If using **Bitwarden Secrets Manager** (designed for developer and machine-to-machine secrets):

```bash
# 1. Install bws
# Download release from https://github.com/bitwarden/sm/releases

# 2. Authenticate with an Access Token
export BWS_ACCESS_TOKEN="your-access-token"

# 3. Inject secrets directly into execution:
bws run -- python manage.py runserver
```
