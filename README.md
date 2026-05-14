# Bajaj DMS — SOA lookup (login-free wrapper)

A small Django server that logs into the Bajaj Finserv DMS portal on your
behalf and exposes a single page where anyone can enter an **AgreementNo**
and see the Statement of Account.

The server keeps a hot session against `dmsoneportal.bajajfinserv.in`:

- Stores your ADID / password in `.env`.
- RSA-encrypts a fresh `LoginKey` payload (PKCS#1 v1.5, public key extracted
  from Bajaj's frontend bundle).
- Maintains the `PortalSession` JWT cookie and refreshes it ~30 s before the
  15-minute expiry.
- Replays the multi-step Bajaj handshake (login → COC check → `/agency`
  priming → SOA report).

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env
# edit .env: set BAJAJ_USERNAME, BAJAJ_PASSWORD, DJANGO_SECRET_KEY

python manage.py runserver 0.0.0.0:8000
```

Open <http://127.0.0.1:8000/>, type an agreement number, hit **Fetch SOA**.

## Endpoints

| Method | Path           | Purpose                                       |
| ------ | -------------- | --------------------------------------------- |
| GET    | `/`            | HTML form                                     |
| POST   | `/api/soa`     | JSON `{ "agreement": "..." }` → SOA JSON      |
| GET    | `/api/soa?agreement=...` | Same, query-string variant         |
| GET    | `/healthz`     | Liveness probe                                |

## Security notes — please read

- `.env` holds the agency password. Never commit it. Treat the server as
  privileged.
- **As shipped the SOA endpoint is fully open.** Anyone who can reach the
  server can pull the SOA of any agreement number the configured account
  can see. If you expose this beyond `localhost`, put it behind one of:
  - basic auth in your reverse proxy (nginx/Caddy/Cloudflare Access), or
  - a shared-secret token check in `soa/views.py`, or
  - a VPN.
- This is a screen-scrape integration. Bajaj can break it any time by
  rotating the RSA key, changing the payload schema, or adding bot
  protection. If logins start failing, inspect the latest
  `login_main.<hash>.js` from `https://dmsoneportal.bajajfinserv.in/login/`
  and update `PUBLIC_KEY_PEM` / the payload shape in `soa/client.py`.

## Troubleshooting

- `BajajLoginError: missing PortalSession cookie` — usually wrong password
  or Bajaj changed the payload field names. Set `DJANGO_DEBUG=True` to see
  request/response details in the log.
- `403 / 401` on the SOA call but login succeeded — Bajaj likely now
  requires a non-empty `Estag`. The client captures whatever the server
  returns from `/agency/api/Auth/home`; check that response's headers.

## Out of scope

- PDF / printable export.
- Multiple Bajaj accounts on one server.
- MFA / captcha (none observed in the captured login flow).
