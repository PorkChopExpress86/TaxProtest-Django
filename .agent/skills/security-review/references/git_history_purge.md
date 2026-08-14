# Git History Purge & Remediation Guide

This guide provides step-by-step instructions for safely purging exposed secrets, files, and PII from a Git repository's entire history.

---

## The Immutable Rule of Secret Exposure

> [!CAUTION]
> **Rewriting Git history does NOT revoke or invalidate compromised credentials.**
> 
> If a commit containing a secret was ever pushed to a remote server (GitHub, GitLab, public server), assume that scrapers and automated bots indexed the secret within seconds.
> 
> **You MUST rotate, regenerate, and revoke the secret at the provider console BEFORE or IMMEDIATELY AFTER rewriting history.**

---

## Recommended Tool: `git-filter-repo`

`git-filter-repo` is the official Git-recommended tool for rewriting history. It is orders of magnitude faster and less error-prone than legacy `git filter-branch` or third-party wrappers.

### Installation

```bash
# Via pip/uv
pip install git-filter-repo

# Or via package manager (Ubuntu/Debian)
sudo apt-get install git-filter-repo
```

---

## Complete Step-by-Step Purge Procedure

### Step 1: Create a Safety Mirror Backup
Before running any history-rewriting command, create a full mirror clone in an external directory:

```bash
# From outside the repository directory:
git clone --mirror /path/to/my-repo /path/to/my-repo-backup.git
```

### Step 2: Purge an Entire File Across All Commits
If a `.env` file, private key (`id_rsa`), or configuration dump was committed:

```bash
cd /path/to/my-repo

# Completely remove .env from all branches, tags, and commits
git-filter-repo --path .env --invert-paths --force

# To remove multiple file patterns:
git-filter-repo \
  --path .env \
  --path .env.local \
  --path-glob "*.key" \
  --path-glob "*.pem" \
  --invert-paths --force
```

### Step 3: Replace Specific Secret Text in Place Across History
If a secret string was embedded directly inside source code or commit messages:

1. Create a `replacements.txt` file (do NOT commit this file):
   ```text
   # Format: exact_secret_string==>[REDACTED]
   AKIAIOSFODNN7EXAMPLE==>[REDACTED_AWS_KEY]
   sk_live_51Abcdef123456789==>[REDACTED_STRIPE_KEY]
   postgres://supersecretpassword@db.internal:5432==>postgres://REDACTED@localhost:5432
   ```

2. Run `git-filter-repo`:
   ```bash
   git-filter-repo --replace-text replacements.txt --force
   rm replacements.txt
   ```

### Step 4: Rewrite Author / Committer PII (Email & Name)
If personal email addresses or names need to be scrubbed from commit metadata:

1. Create a `mailmap` file:
   ```text
   # Format: Correct Name <correct@email.com> <old@email.com>
   Developer <user@users.noreply.github.com> <personal.email@gmail.com>
   ```

2. Run `git-filter-repo`:
   ```bash
   git-filter-repo --mailmap mailmap --force
   ```

### Step 5: Clean Reflogs and Garbage Collect
`git-filter-repo` automatically cleans references, but you can ensure no loose objects remain:

```bash
git reflog expire --expire=now --all
git gc --prune=now --aggressive
```

### Step 6: Verify History
Verify that the sensitive files or strings are gone from all branches:

```bash
# Check if any commit in history still references the file
git log --all --full-history -- "**.env*"

# Search for the secret string
git log -p -S "REDACTED_AWS_KEY" --all
```

### Step 7: Push the Rewritten History to Remote
Once verified:

```bash
# Re-add your remote origin if git-filter-repo unset it (git-filter-repo removes remotes as a safety measure)
git remote add origin <remote-url>

# Force-push all branches and tags
git push origin --force --all
git push origin --force --tags
```

---

## Collaborator Coordination

Any collaborator with an existing clone of the repository before the rewrite MUST NOT merge or pull their old branches, or they will re-introduce the purged commits.

Instruct team members to:
```bash
# 1. Stash or export uncommitted local changes
# 2. Fetch the newly rewritten history and reset local branches:
git fetch origin
git reset --hard origin/main
```
