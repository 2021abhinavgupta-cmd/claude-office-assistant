# Self-hosted GlitchTip for Lumina

Error tracking for the Lumina backend (`app.py`'s `error_monitoring.py`) —
every unhandled exception gets captured automatically once this is running
and `SENTRY_DSN` is set on Railway. Sentry-SDK-protocol-compatible, so the
Python side needs zero GlitchTip-specific code (see
`backend/error_monitoring.py`).

## Important — where this actually needs to run

**Lumina's Flask backend runs on Railway (the cloud), not on your laptop.**
For Railway to send error reports here, this GlitchTip instance needs a
real **public HTTPS address** Railway's servers can reach over the
internet — a bare LAN address like `http://192.168.1.50:8000` on the
always-on laptop will NOT work, because Railway can't reach into your home
network's private IP space.

Two ways to actually make this work:

1. **Run it as a second service on Railway itself** (recommended —
   simplest, and it's already public). Railway supports deploying an
   arbitrary Docker image; point it at this same `docker-compose.yml`'s
   services (Railway's own docs cover multi-service / Docker Compose-style
   projects). Uses more of your Railway plan's resources, but nothing new
   to expose to the internet.
2. **Run it on the always-on laptop, behind a tunnel** — Cloudflare Tunnel
   or ngrok (both have free tiers) give the laptop a real public HTTPS
   URL without port-forwarding your home router. More moving parts, one
   more free-tier account to manage, but keeps everything off Railway's
   resource usage.

If neither of those sounds worth it, the simpler alternative is going back
to **Sentry's own free cloud tier** instead of self-hosting at all — same
`SENTRY_DSN` env var, same zero code changes, just point it at
`sentry.io` instead of your own instance. 5,000 errors/month free, and a
10-person internal tool is almost certainly nowhere near that.

## Setup (once you've picked where it runs)

```bash
cd deploy/glitchtip
cp .env.example .env
# edit .env: real GLITCHTIP_DB_PASSWORD, GLITCHTIP_SECRET_KEY (see the
# comment in .env.example for how to generate one), and GLITCHTIP_DOMAIN
# set to wherever this ends up being reachable (see above)
docker compose up -d
docker compose logs -f web    # watch it come up; confirm the
                               # run-migrate-and-runserver.sh command
                               # actually exists in the image you pulled
                               # (see the comment on that line in
                               # docker-compose.yml if it doesn't)
```

Then open `GLITCHTIP_DOMAIN` in a browser, create the first
organization/account, create a project, and copy its DSN.

## Wire it into Lumina

On Railway, set:
```
SENTRY_DSN=<the DSN GlitchTip gave your project>
```

Then, **one deliberate step, separately** (same convention this codebase
already uses for `semantic_kb.py`'s dependency): add the line from
`backend/requirements-monitoring.txt` to the root `requirements.txt` and
redeploy. This is the only step with real deploy risk — watch that
deploy. Until you do this, `SENTRY_DSN` being set does nothing (guarded
import — see `backend/error_monitoring.py`).

## What you get

Every unhandled exception in the Flask app — the exact "silently 500'd,
nobody knew until someone complained" pattern behind a large fraction of
this codebase's own documented bug history — now shows up in the
GlitchTip dashboard with a full traceback, request context, and
(optionally) email/webhook alerting, the moment it happens.

## Honest status

This compose file and README are written correctly against GlitchTip's
documented install requirements and official Docker Hub image
(`glitchtip/glitchtip`), but **have not been run** in this environment (no
Docker available here). The web service's startup command name was
confirmed via a community fork's compose file, not a hands-on run against
the current official image — if it's wrong, `docker compose logs web`
will say so plainly (a "no such file" error), and the fix is checking
`docker run --rm glitchtip/glitchtip:latest ls bin/` for the real script
name.
