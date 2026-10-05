# Upsido.ai backend

Manages workshops, workshop registrations and the course brochure PDF for the landing page.
Python 3.9+, standard library only, SQLite database. No packages needed to run or test.

## Run it
1. `cp .env.example .env` and set `ADMIN_PASSWORD` (login is disabled until you do).
2. `python server.py` then open `http://localhost:8000/admin`.
3. Tests: `python -m unittest tests.test_api -v` (needs `pip install requests`).

## Deploy
Run it under a production server and put HTTPS in front (Caddy, nginx or your host's TLS):

    pip install gunicorn
    gunicorn -w 2 -b 0.0.0.0:8000 server:app

- Keep the `data/` folder on persistent disk and back it up. It holds `upsido.db` (workshops, registrations),
  `brochure.pdf` and `secret.key` (signs admin sessions). Use `DATA_DIR` to move it.
- Set `CORS_ORIGINS` to your landing page's address and `TRUST_PROXY=true` if behind a proxy.
- SQLite suits a few thousand registrations and a couple of workers. If you outgrow it, tell me and I will move it to MongoDB or Postgres.

## Connect the landing page
In `upsido-landing.html`, find `var API_BASE="";` and set it to your backend address, for example
`var API_BASE="https://api.upsido.ai";`. Then:
- Published workshops from the admin replace the template card, each with its own Register now button.
- Registrations are saved here and show up in Admin > Registrations (search, filter by workshop, CSV export).
- Download brochure serves the PDF you upload in Admin > Brochure (falls back to the built-in copy if none).
If the backend is unreachable the popup shows an error instead of pretending it worked.

## Admin panel (/admin)
- **Workshops:** add, edit, publish/hide, feature, delete. Past dates drop off the landing page automatically.
- **Registrations:** search, filter, delete, export CSV. Cells that start with = + - @ are prefixed with ' so spreadsheets cannot run them as formulas.
- **Brochure:** upload or replace a PDF (checked as a real PDF, size-limited), download, remove.

## API
Public: `GET /api/workshops`, `POST /api/registrations`, `GET /api/brochure`, `GET /api/brochure/info`, `GET /api/health`
Admin (Bearer token from `POST /api/admin/login`): `GET/POST /api/admin/workshops`, `PATCH/DELETE /api/admin/workshops/<id>`,
`GET /api/admin/registrations(.csv)`, `DELETE /api/admin/registrations/<id>`, `POST/DELETE /api/admin/brochure`, `GET /api/admin/stats`

## Security notes
Password comes from `.env` only. Login is rate limited (8/min per IP), registration too (10 per 10 min per IP).
Admin sessions last 12 hours. Seats are enforced and duplicate email registrations are ignored per workshop.
There is one admin account and no password reset: change `ADMIN_PASSWORD` and restart. Personal data (names, emails, phones)
is stored unencrypted in SQLite, so protect the server and the `data/` folder, and add a privacy notice before launch.
