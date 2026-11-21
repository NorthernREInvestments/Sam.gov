# M3 Supply — Business Operating System (2.0)

Clean-sheet Build 001. Architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Run locally

```bash
pip install -r requirements.txt
uvicorn app:app --reload --port 8080
```

Requires `DATABASE_URL`, `SECRET_KEY`, and owner bootstrap env vars (see `.env.example`).

## Deploy

Railway uses the existing GitHub connection and PostgreSQL service. No deploy-time data wipe.
