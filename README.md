# jedLoft v2

This repository now includes a Dockerized web host with user authentication and server-side data storage.

## What Is Included

1. FastAPI web application with server-rendered pages
2. User registration and login at `/register` and `/login`
3. Session-protected dashboard at `/dashboard`
4. MySQL for persistent server storage
5. Docker + Docker Compose for easy local hosting and management
6. Local bind mounts:
   - `./` mapped to `/app`
   - `./data` mapped to `/app/data`
7. Role-based access (`admin`, `read_only`) with admin approval flow for self-registered users
8. Password policy and history enforcement:
   - Minimum 6 characters
   - At least 2 uppercase letters
   - Cannot reuse current or last 2 passwords
9. Bird records with long-term SQL storage for:
   - Type of bird
   - Sex of bird
   - Band number (if banded)
   - Birth date and birthplace
   - Foreign loft owner name
   - Pedigree and bloodline
   - Special colors and features/markings
   - Family tree notes
   - Paired mate band number
10. Racing homers notes and flight logs tracked in SQL
11. Settings page for password reset, email change, colorblind theme, and text size
12. Admin page for user approval, user enable/disable, role management, and logs export
13. Mobile-friendly navigation menu and simplified GUI cards/tables
14. Ownership groups for birds and flight logs with per-user `view` or `edit` access by group
15. Bird-level sharing with `read only` (`view`) or `edit` permissions
16. Android-ready web-to-app client page at `/android-client` with local offline cache and server credential validation

## Stack

1. FastAPI
2. SQLAlchemy
3. MySQL
4. Jinja2 templates
5. Docker / Docker Compose

## Android Web-to-App Client

Use the Android-capable client page:

```text
http://localhost:8000/android-client
```

What it does:

1. Validates the configured server URL by calling `/api/mobile/health`.
2. Validates username/password against `/api/mobile/login`.
3. Pulls data via `/api/mobile/sync`.
4. Stores the latest sync locally in browser storage for offline viewing.
5. Can be installed to Android home screen (Chrome menu: "Add to Home screen").
6. Creates birds and flights while online through `/api/mobile/birds` and `/api/mobile/flights`.

## Quick Start

1. Start the stack:

```bash
docker compose up --build
```

1. Open the site:

```text
http://localhost:8000
```

1. Create an account on the register page and log in.

## Environment Variables

The default development `.env` is already included.

Use `.env.example` as the template for production:

```env
IMAGE_NAME=ghcr.io/wickedyoda/jedloft:latest
WEB_PORT=8000
MYSQL_PORT=3306
MYSQL_DATABASE=jedloft
MYSQL_USER=jedloft
MYSQL_PASSWORD=jedloft
MYSQL_ROOT_PASSWORD=rootpass
DATABASE_URL=mysql+pymysql://jedloft:jedloft@db:3306/jedloft
SESSION_SECRET=change-this-to-a-long-random-string
LOG_DIR=/logs
MOBILE_ALLOWED_ORIGINS=*
DEFAULT_ADMIN_NAME=System Admin
DEFAULT_ADMIN_EMAIL=admin@example.com
DEFAULT_ADMIN_PASSWORD=AdminAA1
```

Environment variables used by the app and compose stack:

1. `IMAGE_NAME` - Docker image name published to GitHub Container Registry.
2. `WEB_PORT` - Host port mapped to the web container.
3. `MYSQL_PORT` - Host port mapped to MySQL.
4. `MYSQL_DATABASE` - MySQL database name.
5. `MYSQL_USER` - MySQL user name.
6. `MYSQL_PASSWORD` - MySQL user password.
7. `MYSQL_ROOT_PASSWORD` - MySQL root account password.
8. `DATABASE_URL` - SQLAlchemy connection string used by the app.
9. `SESSION_SECRET` - Session signing secret.
10. `LOG_DIR` - Log directory mounted from the host.
11. `DEFAULT_ADMIN_NAME` - Bootstrap admin display name.
12. `DEFAULT_ADMIN_EMAIL` - Bootstrap admin login email.
13. `DEFAULT_ADMIN_PASSWORD` - Bootstrap admin password.
14. `MOBILE_ALLOWED_ORIGINS` - Comma-separated CORS origins for mobile API clients (`*` allows all).

`docker-compose.yml` reads `example.env` first, then `.env` for local overrides.

## Container Management

Start:

```bash
docker compose up -d --build
```

Build and run using the published image on a host that supports Docker:

```bash
docker compose pull
docker compose up -d
```

To force a local rebuild instead of using the published image:

```bash
docker compose up -d --build
```

Stop:

```bash
docker compose down
```

Stop and remove database volume:

```bash
docker compose down -v
```

View logs:

```bash
docker compose logs -f web
```

## Published Image

The CI workflow publishes a multi-arch image to GitHub Container Registry:

```text
ghcr.io/wickedyoda/jedloft:latest
```

You can also pull a SHA-tagged image from the same registry after a main branch push.

```bash
docker pull ghcr.io/wickedyoda/jedloft:latest
```

## Push Safety Checks

Pushes to `main` and pull requests targeting `main` run GitHub Actions checks that:

1. Verify Python syntax and import integrity.
2. Check installed dependency consistency with `pip check`.
3. Run a Bandit security scan on the application code.
4. Audit pinned dependencies with `pip-audit`.

## Docker Image Publish

After `CI` passes on `main`, a separate workflow publishes a multi-arch image to GitHub Container Registry for:

1. `linux/amd64`
2. `linux/arm64`

The published image uses the `IMAGE_NAME` value, which defaults to `ghcr.io/wickedyoda/jedloft`.

The workflows are separated as follows:

1. `Code Safety` - syntax, dependency, and security checks only.
2. `Container Release` - builds and publishes `linux/amd64` and `linux/arm64` images after successful `Code Safety` on `main`.

If GHCR push fails with `permission_denied: The requested installation does not exist`, add a repository secret named `GHCR_TOKEN` (PAT with `write:packages` and `read:packages`).
The workflow uses `GHCR_TOKEN` when present and falls back to `GITHUB_TOKEN` otherwise.

## Bind Mounts

1. The web container runs from `/app` and binds to your local project directory (`./:/app`).
2. The app data directory is `/app/data` and binds to `./data` (`./data:/app/data`).
3. MySQL data is persisted in `./data/mysql`.
4. Application logs are written to `/logs` and bind mounted to `./logs`.

## Security Notes

1. Change `SESSION_SECRET` before deploying publicly.
2. Set HTTPS at your reverse proxy when hosting on the internet.
3. Replace default database credentials for production.

## Project Structure

1. `app/main.py`: Routes, auth flow, session handling
2. `app/models.py`: Database models
3. `app/database.py`: SQLAlchemy engine/session setup
4. `templates/`: Login/register/dashboard HTML pages
5. `docker-compose.yml`: Web + MySQL services
6. `data/`: Local persistent bind-mounted storage
7. `logs/`: Exportable application logs and generated zip archives
