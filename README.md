# Portland Ordinance API (Free-tier Render + GitHub)

A small FastAPI service that wraps the City of Portland, TX Municode site so a GPT can retrieve **live** ordinance sections for reasoning/citation.

## Endpoints

- `GET /health` — quick status check
- `GET /fetchByUrl?url=...` — fetch & parse a specific Municode section by URL
- `GET /searchOrdinance?q=keywords` — site-limited search (DuckDuckGo HTML) to find Portland Municode pages and return parsed results (best effort)

All requests require an **`x-api-key`** header. Set the `API_KEY` environment variable in hosting.

## Local Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export API_KEY=devkey123
uvicorn app.main:app --reload
# open http://127.0.0.1:8000/docs
```

## Deploy to Render (free)

1. Push this folder to a GitHub repo.
2. In Render: **New +** → **Web Service** → connect your repo.
3. Runtime: **Python**. Build: `pip install -r requirements.txt`.
4. Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Set **Environment Variable** `API_KEY` to a long random string.
6. Choose the **Free** plan and deploy.

## Security

- API key is required via `x-api-key` header.
- Simple per-IP rate limit (60 req/min).
- CORS currently allows all origins — tighten for production.
- Do not store logs containing sensitive info.

## Actions (Custom GPT) OpenAPI schema

Use the provided `actions_openapi.json`, replace `YOUR-RENDER-URL` with your deployed URL, and paste into the **Actions → Schema** field.
