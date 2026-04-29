# Production Security Deployment (Pi)

## 1) Install and enable Nginx reverse proxy

1. Install Nginx and certbot (LetsEncrypt).
2. Copy `deploy/nginx/vinyl-inventory.conf` to `/etc/nginx/sites-available/vinyl-inventory`.
3. Update `ssl_certificate` and `ssl_certificate_key` to your real certificate paths.
4. Enable site and reload:
   - `sudo ln -s /etc/nginx/sites-available/vinyl-inventory /etc/nginx/sites-enabled/vinyl-inventory`
   - `sudo nginx -t && sudo systemctl reload nginx`

## 2) Run app internally only

1. Copy `deploy/systemd/vinyl-inventory.service` to `/etc/systemd/system/vinyl-inventory.service`.
2. Set the correct service user/group and app path.
3. Reload and restart:
   - `sudo systemctl daemon-reload`
   - `sudo systemctl enable --now vinyl-inventory`

The app should only listen on `127.0.0.1:5003`.

## 3) Firewall lockdown

Allow only SSH + HTTPS (and HTTP only for redirect/cert issuance):

- `sudo ufw allow 22/tcp`
- `sudo ufw allow 80/tcp`
- `sudo ufw allow 443/tcp`
- `sudo ufw deny 5001/tcp`
- `sudo ufw deny 5002/tcp`
- `sudo ufw deny 5003/tcp`
- `sudo ufw enable`

## 4) Owner credential hardening

Generate a hash:

- `python tools/generate_owner_hash.py`

Set `owner_password_hash` in `config.json` (or `VINYL_OWNER_PASSWORD_HASH` env var) and remove any plaintext owner password/PIN from deployment secrets.

## 5) Validation checks

- `curl -I http://your-domain` should 301 to HTTPS.
- `curl -I https://your-domain` should include security headers (`HSTS`, `X-Content-Type-Options`, `X-Frame-Options`).
- Direct backend ports should not be reachable externally.
- Owner login required for all write endpoints.
