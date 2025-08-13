from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict
import os
import httpx
from bs4 import BeautifulSoup
import re
import time
from datetime import date

app = FastAPI(title="Portland Ordinance API", version="1.0.0")

# ---------------------------------------------------------------------------
# CORS (tighten allow_origins later if you want)
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Security / rate limiting
# ---------------------------------------------------------------------------
API_KEY = os.getenv("API_KEY", "")

# Simple in-memory rate limiter (per IP)
RATE_LIMIT = 60  # requests per minute
_window = 60.0
_calls: Dict[str, List[float]] = {}


def _rate_limit(ip: str) -> bool:
    now = time.time()
    bucket = _calls.setdefault(ip, [])
    # drop old timestamps
    while bucket and now - bucket[0] > _window:
        bucket.pop(0)
    if len(bucket) >= RATE_LIMIT:
        return False
    bucket.append(now)
    return True


def _require_api_key(key: Optional[str]):
    expected = os.getenv("API_KEY", "")
    if not expected:
        # If API_KEY not set (dev mode), allow all
        return True
    return key == expected


# Allow unauthenticated health + privacy so probes/public policy work
@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path in ("/health", "/healthz", "/privacy"):
        return await call_next(request)

    # rate limit
    ip = request.client.host if request.client else "unknown"
    if not _rate_limit(ip):
        return JSONResponse({"error": "rate_limit_exceeded"}, status_code=429)

    # API key check
    api_key = request.headers.get("x-api-key")
    if not _require_api_key(api_key):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    return await call_next(request)


# ---------------------------------------------------------------------------
# App routes
# ---------------------------------------------------------------------------
MUNICODE_BASE = (
    "https://library.municode.com/tx/portland/codes/code_of_ordinances?nodeId=COOR_APXAUNDEOR"
)


class OrdinanceSection(BaseModel):
    section_number: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    snippet: Optional[str] = None
    text: Optional[str] = None
    headings: Optional[List[str]] = None


@app.get("/health")
async def health():
    return {"ok": True, "source": "municode", "base": MUNICODE_BASE}


# Extra unauthenticated health endpoint for Render
@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ---------- Privacy Policy (HTML) ----------
PRIVACY_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Privacy Policy — Portland Ordinance API</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;line-height:1.55}code{background:#f4f4f4;padding:2px 4px;border-radius:4px}</style>
</head>
<body>
<h1>Privacy Policy — Portland Ordinance API</h1>
<p><strong>Effective:</strong> {{date}}</p>

<h2>What this service does</h2>
<p>This API retrieves public ordinance pages from <code>library.municode.com</code> for the City of Portland, TX and returns the text to client applications (e.g., a Custom GPT) for analysis and citation. It is read-only and does not modify external systems.</p>

<h2>Data processed</h2>
<ul>
  <li><strong>Request details</strong>: endpoint and query parameters you send (e.g., search terms, Municode URLs).</li>
  <li><strong>Network metadata</strong>: client IP address for rate limiting and abuse prevention.</li>
  <li><strong>Operational logs</strong>: timestamps and status codes for troubleshooting.</li>
</ul>
<p>No cookies are set. No names, emails, or account identifiers are intentionally collected.</p>

<h2>Retention</h2>
<ul>
  <li>Rate-limit buckets are in-memory and ephemeral.</li>
  <li>Host (Render) may retain short-lived logs for operations.</li>
</ul>

<h2>Sharing</h2>
<ul>
  <li>Requests are sent to <code>library.municode.com</code> to fetch public ordinance content.</li>
  <li>No data is sold or shared for advertising.</li>
</ul>

<h2>Security</h2>
<ul>
  <li>All traffic uses HTTPS.</li>
  <li>Access to protected endpoints requires an <code>x-api-key</code> header. Keys can be rotated if compromised.</li>
</ul>

<h2>Your choices</h2>
<ul>
  <li>Do not send sensitive or non-public information to this API.</li>
  <li>For concerns or deletion requests (where applicable), contact us.</li>
</ul>

<h2>Contact</h2>
<p>Email: <a href="mailto:gisteam@portlandtx.gov">gisteam@portlandtx.gov</a></p>
</body>
</html>
"""


@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    today = date.today().strftime("%B %d, %Y")
    return HTMLResponse(PRIVACY_HTML.replace("{{date}}", today))


def _clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for s in soup(["script", "style", "noscript", "iframe"]):
        s.decompose()
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)


async def fetch_url(url: str) -> str:
    timeout = httpx.Timeout(20.0, connect=20.0)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36 PortlandOrdinanceBot/1.0"
        )
    }
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        r = await client.get(url, follow_redirects=True)
        r.raise_for_status()
        return r.text


def extract_section_fields(url: str, html: str) -> OrdinanceSection:
    soup = BeautifulSoup(html, "lxml")

    # Try to find title and section number
    title = None
    section_number = None
    headings: List[str] = []

    for h in soup.find_all(["h1", "h2", "h3"]):
        t = h.get_text(" ", strip=True)
        if t:
            headings.append(t)
            if not title:
                title = t

    if title:
        m = re.search(r"(§+\s*\d[\w\.\-]*)", title)
        if m:
            section_number = m.group(1)

    text = _clean_text(html)
    snippet = (text[:300] + "…") if len(text) > 300 else text

    return OrdinanceSection(
        section_number=section_number,
        title=title,
        url=url,
        snippet=snippet,
        text=text,
        headings=[h for h in headings if h],
    )


@app.get("/fetchByUrl", response_model=OrdinanceSection)
async def fetch_by_url(url: str = Query(..., description="Direct Municode section URL")):
    if "library.municode.com" not in url:
        raise HTTPException(status_code=400, detail="Only Municode URLs are supported.")
    html = await fetch_url(url)
    return extract_section_fields(url, html)


class SearchResult(BaseModel):
    results: List[OrdinanceSection]
    query: str


# Tolerant DuckDuckGo HTML endpoint
DUCK = "https://html.duckduckgo.com/html/"


async def duckduck_search(query: str) -> List[str]:
    """
    Use DuckDuckGo HTML to find Municode pages for Portland, TX.
    Be resilient to non-200 responses/timeouts and just return [] on failure.
    """
    # keep it specific to Portland, TX municode
    q = f"site:library.municode.com/tx/portland {query}"
    params = {"q": q}
    timeout = httpx.Timeout(20.0, connect=20.0)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36 PortlandOrdinanceBot/1.0"
        )
    }

    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            r = await client.get(DUCK, params=params, follow_redirects=True)
            # If DDG returns a non-200 or empty body, return [] instead of raising
            if r.status_code != 200 or not r.text:
                return []
            html = r.text
    except Exception as e:
        # Log to Render logs and return no results rather than erroring out
        print("duckduck_search error:", repr(e))
        return []

    soup = BeautifulSoup(html, "lxml")
    links: List[str] = []

    # Primary selector
    for a in soup.select("a.result__a"):
        href = a.get("href")
        if href and "library.municode.com" in href:
            links.append(href)

    # Fallback selector, in case layout changes
    if not links:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "library.municode.com" in href:
                links.append(href)

    # De-duplicate, keep top 5
    seen = set()
    uniq: List[str] = []
    for u in links:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
        if len(uniq) >= 5:
            break

    return uniq


@app.get("/searchOrdinance", response_model=SearchResult)
async def search_ordinance(
    q: str = Query(..., min_length=2, description="Keywords to search within Municode for Portland, TX")
):
    urls = await duckduck_search(q)
    if not urls:
        return SearchResult(query=q, results=[])
    results: List[OrdinanceSection] = []
    for url in urls:
        try:
            html = await fetch_url(url)
            results.append(extract_section_fields(url, html))
        except Exception as e:
            # log and keep going
            print("fetch_or_parse error:", repr(e))
            continue
    return SearchResult(query=q, results=results)
