from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict
import os
import httpx
from bs4 import BeautifulSoup
import re
import time

app = FastAPI(title="Portland Ordinance API", version="1.0.0")

# CORS (tighten allow_origins later if you want)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Security / rate limiting ------------------------------------------------
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

# Allow Render to probe health without API key
@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path in ("/health", "/healthz"):
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

# ---- App routes --------------------------------------------------------------
MUNICODE_BASE = "https://library.municode.com/tx/portland/codes/code_of_ordinances?nodeId=COOR_APXAUNDEOR"

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

def _clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for s in soup(["script", "style", "noscript", "iframe"]):
        s.decompose()
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)

async def fetch_url(url: str) -> str:
    timeout = httpx.Timeout(20.0, connect=20.0)
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "PortlandOrdinanceBot/1.0"}) as client:
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

DUCK = "https://duckduckgo.com/html/"

async def duckduck_search(query: str) -> List[str]:
    # Simple site-limited search to Municode (no API keys)
    q = f"site:library.municode.com tx portland code of ordinances {query}"
    params = {"q": q}
    timeout = httpx.Timeout(20.0, connect=20.0)
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "PortlandOrdinanceBot/1.0"}) as client:
        r = await client.get(DUCK, params=params)
        r.raise_for_status()
        html = r.text
    soup = BeautifulSoup(html, "lxml")
    links: List[str] = []
    for a in soup.select("a.result__a"):
        href = a.get("href")
        if href and "library.municode.com" in href:
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
async def search_ordinance(q: str = Query(..., min_length=2, description="Keywords to search within Municode for Portland, TX")):
    urls = await duckduck_search(q)
    if not urls:
        return SearchResult(query=q, results=[])
    results: List[OrdinanceSection] = []
    for url in urls:
        try:
            html = await fetch_url(url)
            results.append(extract_section_fields(url, html))
        except Exception:
            continue
    return SearchResult(query=q, results=results)
