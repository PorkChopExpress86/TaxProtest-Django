# Subdomain & External Dependency Audit

Generated: October 20, 2025  
Purpose: Determine readiness for HSTS includeSubDomains and preload

---

## Summary

**Result:** ✅ **SAFE to enable includeSubDomains and preload** (with caveats below)

Your application has:
- No internal subdomains referenced in code or templates
- Only external third-party CDN dependencies (Tailwind CSS)
- No hardcoded domain references except localhost/127.0.0.1 for local dev

---

## Domains & Origins Found

### Internal / Configuration
| Source | Type | Value | Notes |
|--------|------|-------|-------|
| `.env` | ALLOWED_HOSTS | `localhost,127.0.0.1,0.0.0.0` | Local dev only |
| `.env` | CSRF_TRUSTED_ORIGINS | `http://localhost:8000,http://127.0.0.1:8000` | ⚠️ Must be updated to `https://` when behind proxy |
| `docker-compose.yml` | Port binding | `8000:8000` | Local only |

### External Dependencies
| Source | Type | URL | Security Impact |
|--------|------|-----|-----------------|
| `templates/base.html` | CDN script | `https://cdn.tailwindcss.com` | ✅ Already HTTPS, external domain (not affected by your HSTS) |

### Data Source References (documentation only)
| Source | Type | URL | Security Impact |
|--------|------|-----|-----------------|
| Documentation | HCAD data | `https://hcad.org/` | ✅ External site, no impact |
| Documentation | HCAD downloads | `https://download.hcad.org/data/` | ✅ External site, no impact |

---

## Template Analysis

### `templates/base.html`
- ✅ No subdomain references
- ✅ Only external CDN: `https://cdn.tailwindcss.com`
- ✅ Internal links use relative URLs (`href="/"`, `href="#"`)

### `templates/index.html`
- ✅ All links are relative or Django URL tags (`{% url 'similar_properties' %}`)
- ✅ No hardcoded domains or subdomains

### `templates/similar_properties.html`
- ✅ All links are relative or Django URL tags
- ✅ No hardcoded domains or subdomains

---

## Settings Analysis

### Django Settings (`taxprotest/settings.py`)
- ✅ `ALLOWED_HOSTS` read from env (currently localhost only)
- ✅ `CSRF_TRUSTED_ORIGINS` read from env (currently localhost only)
- ✅ No hardcoded domain/subdomain references
- ✅ `USE_X_FORWARDED_HOST=1` already enabled

### Docker Compose
- ✅ No external domain references
- ✅ Redis/Postgres on local Docker network only
- ✅ Ports exposed locally only

---

## Potential Issues & Action Items

### ⚠️ Action Required Before Going Live

1. **Update `.env` with your production domain:**
   ```bash
   ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com
   CSRF_TRUSTED_ORIGINS=https://yourdomain.com,https://www.yourdomain.com
   ```

2. **Decision: www subdomain?**
   - If you plan to use `www.yourdomain.com`, you must:
     - Have a valid HTTPS cert for it
     - Include it in `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`
   - If you only use the apex domain (`yourdomain.com`), no action needed
   - Recommendation: Redirect www → apex in nginx or support both

3. **Verify nginx configuration includes:**
   ```nginx
   proxy_set_header Host $host;
   proxy_set_header X-Forwarded-Proto $scheme;
   proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
   ```

### ✅ No Blockers Found

- No API subdomains
- No staging/dev subdomains referenced
- No static asset subdomains
- No third-party integrations that require HTTP
- No admin panels on separate subdomains

---

## Recommendations

### Short-term (Current Setup - 1 week HSTS)
✅ Already configured:
- SECURE_HSTS_SECONDS=604800 (1 week)
- includeSubDomains=False
- preload=False

Continue testing for 1 week and monitor logs for:
- Mixed content warnings
- Certificate errors
- Any unexpected HTTP requests

### Mid-term (After 1 week of testing)
Update `.env`:
```bash
# Increase HSTS to 1 year
SECURE_HSTS_SECONDS=31536000
```

Restart containers:
```powershell
docker compose up -d --no-deps --build web worker beat
```

### Long-term (Final Production - includeSubDomains + preload)

**Only enable if:**
- ✅ You've tested with 1-year HSTS for at least 1 week
- ✅ No plans for any HTTP-only subdomains
- ✅ Your SSL certificate covers all subdomains you'll use (or wildcard cert)
- ✅ www (if used) is HTTPS-ready

Update `.env`:
```bash
SECURE_HSTS_SECONDS=31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS=1
SECURE_HSTS_PRELOAD=1
```

Restart containers and verify:
```powershell
docker compose up -d --no-deps --build web worker beat
docker compose exec web python manage.py check --deploy
```

Then submit to HSTS preload list at: https://hstspreload.org/

---

## Nginx Configuration Example

```nginx
server {
    listen 443 ssl http2;
    server_name yourdomain.com www.yourdomain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    # HSTS header (start with 1 week, then increase to 1 year)
    add_header Strict-Transport-Security "max-age=604800; includeSubDomains" always;
    
    # Other security headers (Django also sets these, but can be set here too)
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Optional: serve static files directly from nginx
    location /static/ {
        alias /path/to/TaxProtest-Django/staticfiles/;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name yourdomain.com www.yourdomain.com;
    return 301 https://$server_name$request_uri;
}

# Optional: redirect www to apex (or vice versa)
server {
    listen 443 ssl http2;
    server_name www.yourdomain.com;
    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    return 301 https://yourdomain.com$request_uri;
}
```

---

## Final Checklist Before Enabling includeSubDomains/Preload

- [ ] Update `ALLOWED_HOSTS` in `.env` with production domain(s)
- [ ] Update `CSRF_TRUSTED_ORIGINS` in `.env` with `https://` scheme
- [ ] Test HTTPS works for all domains/subdomains you'll use
- [ ] Verify nginx forwards `X-Forwarded-Proto: https`
- [ ] Monitor for 1 week with short HSTS (604800)
- [ ] Increase to 1-year HSTS (31536000) and monitor for another week
- [ ] If all subdomains are HTTPS-ready, enable `SECURE_HSTS_INCLUDE_SUBDOMAINS=1`
- [ ] Only enable `SECURE_HSTS_PRELOAD=1` if you want browser preload (hard to reverse)
- [ ] Submit to https://hstspreload.org/ (optional, only if preload=1)

---

## Conclusion

Your application is **subdomain-safe** and ready for includeSubDomains/preload **once you:**
1. Update `.env` with your production domain
2. Complete the phased rollout (1 week → 1 year → includeSubDomains → preload)
3. Verify your nginx configuration

No code changes are required; only environment variables need updating when you're ready to finalize.
