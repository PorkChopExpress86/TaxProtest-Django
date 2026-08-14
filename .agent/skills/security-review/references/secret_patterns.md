# Secret Patterns & Detection Catalog

This catalog details regex patterns, token formats, and detection heuristics for auditing repositories and configuration files.

---

## 1. Cloud Provider Credentials

### Amazon Web Services (AWS)
- **AWS Access Key ID**: `(A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}`
- **AWS Secret Access Key**: `(?i)aws(.{0,20})?['\"][0-9a-zA-Z\/+]{40}['\"]`
- **AWS MWS Key**: `amzn\.mws\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`

### Google Cloud & Firebase
- **Google API Key**: `AIza[0-9A-Za-z\\-_]{35}`
- **GCP Service Account Private Key**: `\"type\":\\s*\"service_account\"`
- **OAuth Access Token**: `ya29\\.[0-9A-Za-z\\-_]+`
- **Firebase Auth / Realtime DB URL**: `https:\/\/[a-z0-9-]+\.firebaseio\.com`

### Microsoft Azure
- **Azure Storage Key**: `DefaultEndpointsProtocol=https;AccountName=[a-z0-9]+;AccountKey=[A-Za-z0-9+/=]{88}`
- **Azure Cosmos DB Key**: `AccountEndpoint=https:\/\/[a-z0-9-]+.documents.azure.com;AccountKey=[A-Za-z0-9+/=]{88}`
- **Azure Tenant/App Client Secret**: `[0-9a-zA-Z~_-]{34,40}`

---

## 2. AI & Machine Learning Services

- **OpenAI API Key**: `sk-(proj-)?[a-zA-Z0-9_-]{32,}`
- **Anthropic API Key**: `sk-ant-[a-zA-Z0-9_-]{40,}`
- **Google Gemini API Key**: `AIza[0-9A-Za-z\\-_]{35}`
- **HuggingFace User Access Token**: `hf_[a-zA-Z0-9]{34}`
- **Pinecone API Key**: `[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}`
- **Cohere API Key**: `[a-zA-Z0-9]{40}`
- **Mistral API Key**: `[a-zA-Z0-9]{32}`

---

## 3. Version Control & CI/CD

- **GitHub Personal Access Token (Classic)**: `ghp_[0-9a-zA-Z]{36}`
- **GitHub Fine-Grained Personal Access Token**: `github_pat_[0-9a-zA-Z_]{82}`
- **GitHub OAuth Access Token**: `gho_[0-9a-zA-Z]{36}`
- **GitHub App / Refresh Token**: `(ghu|ghs|ghr)_[0-9a-zA-Z]{36}`
- **GitLab Personal Access Token**: `glpat-[0-9a-zA-Z\\-_]{20,}`
- **Bitbucket App Password**: `(?i)bitbucket(.{0,20})?['\"][a-zA-Z0-9]{18,}['\"]`

---

## 4. Payment & SaaS Services

- **Stripe Secret Key**: `sk_(live|test)_[0-9a-zA-Z]{24,34}`
- **Stripe Restricted Key**: `rk_(live|test)_[0-9a-zA-Z]{24,34}`
- **Stripe Webhook Secret**: `whsec_[0-9a-zA-Z]{32}`
- **Square Access Token**: `sq0atp-[0-9A-Za-z\\-_]{22}`
- **PayPal Braintree Access Token**: `access_token\\$production\\$[0-9a-z]{16}\\$[0-9a-f]{32}`
- **Twilio Account SID / Auth Token**: `AC[a-z0-9]{32}` / `[a-z0-9]{32}`
- **SendGrid API Key**: `SG\.[a-zA-Z0-9_\-\.]{66}`
- **Mailgun API Key**: `key-[0-9a-zA-Z]{32}`
- **Slack Token**: `xox[baprs]-[0-9a-zA-Z]{10,48}`
- **Slack Webhook URL**: `https:\/\/hooks\.slack\.com\/services\/T[a-zA-Z0-9_]+\/B[a-zA-Z0-9_]+\/[a-zA-Z0-9_]+`
- **Discord Bot Token / Webhook**: `[MN][A-Za-z\d]{23}\.[\w-]{6}\.[\w-]{27}` / `https:\/\/discord(app)?\.com\/api\/webhooks\/[0-9]+\/[A-Za-z0-9_-]+`

---

## 5. Database & Cache URLs

- **PostgreSQL**: `postgres(ql)?:\/\/[a-zA-Z0-9_]+:[^@\s]+@[a-zA-Z0-9_\.\-]+(:[0-9]+)?\/[a-zA-Z0-9_]+`
- **MySQL**: `mysql:\/\/[a-zA-Z0-9_]+:[^@\s]+@[a-zA-Z0-9_\.\-]+(:[0-9]+)?\/[a-zA-Z0-9_]+`
- **Redis**: `redis(s)?:\/\/(:[^@\s]+@)?[a-zA-Z0-9_\.\-]+(:[0-9]+)?(\/[0-9]+)?`
- **MongoDB**: `mongodb(\+srv)?:\/\/[a-zA-Z0-9_]+:[^@\s]+@[a-zA-Z0-9_\.\-]+`

---

## 6. Cryptographic Keys & Certificates

- **RSA Private Key**: `-----BEGIN RSA PRIVATE KEY-----`
- **OpenSSH Private Key**: `-----BEGIN OPENSSH PRIVATE KEY-----`
- **DSA / EC Private Key**: `-----BEGIN (DSA|EC) PRIVATE KEY-----`
- **Generic Private Key**: `-----BEGIN PRIVATE KEY-----`
- **PGP Private Key**: `-----BEGIN PGP PRIVATE KEY BLOCK-----`
- **Encrypted Private Key**: `-----BEGIN ENCRYPTED PRIVATE KEY-----`
- **SSL / TLS Certificate**: `-----BEGIN CERTIFICATE-----`

---

## 7. Authentication Tokens & Framework Secrets

- **JSON Web Token (JWT)**: `eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+`
- **Django SECRET_KEY**: `django-insecure-[a-zA-Z0-9!@#$%^&*()_+\-=\[\]{}|;':",.<>?\/]{40,}` or `SECRET_KEY\s*=\s*['\"][^'\"]{30,}['\"]`
- **Basic Auth in Headers/URLs**: `Authorization:\s*Basic\s+[A-Za-z0-9+/=]{10,}`
