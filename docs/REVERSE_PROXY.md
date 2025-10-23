Reverse proxy setup and testing

This project runs inside Docker Compose. If you front the app with an external reverse proxy
(e.g. nginx, Traefik, Caddy), use the following notes to verify the configuration.

Summary of key settings

- Compose exposes the web service on host port 8020 and binds the container to port 8000.
- The default `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` include `property.ohmygoshwhatever.com`.
- The compose default enables `USE_X_FORWARDED_HOST=1` and `ENABLE_SECURE_PROXY_SSL_HEADER=1`.
- `FORCE_SECURE_COOKIES` is set to `1` by default so `SESSION_COOKIE_SECURE` and `CSRF_COOKIE_SECURE` are enabled.

Start the app (from project root):

```powershell
# build and start (foreground)
docker compose up --build

# or start in detached mode
docker compose up -d --build
```

Test locally using the reverse proxy domain:

- Ensure your reverse proxy routes `property.ohmygoshwhatever.com` to the host running Docker and forwards to localhost:8020.
- If you're testing locally and don't have DNS for the domain, add a hosts file entry pointing the domain to the host's IP.

Verify Django sees the correct host and scheme:

- Visit https://property.ohmygoshwhatever.com in your browser (if TLS is terminated at the proxy).
- If TLS is on the proxy, verify the proxy sets `X-Forwarded-Proto: https` header so Django treats requests as secure.

Troubleshooting

- If you get "DisallowedHost" or CSRF errors, check the values of `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` in the environment used by Compose.
- To override defaults, set environment variables in a `.env` file or export them before `docker compose up`.

Example `.env` overrides (project root):

```
ALLOWED_HOSTS=property.ohmygoshwhatever.com
CSRF_TRUSTED_ORIGINS=https://property.ohmygoshwhatever.com
FORCE_SECURE_COOKIES=1
DJANGO_SECRET_KEY=your-secret-here
```

That's it — the compose defaults should make the app work behind a TLS-terminating reverse proxy. If you want, I can add an example nginx config next.
