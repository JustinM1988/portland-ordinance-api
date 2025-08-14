from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Tuple
from pathlib import Path
import os, re, time, yaml
import httpx
from bs4 import BeautifulSoup
from datetime import date

app = FastAPI(title="Portland Ordinance API", version="1.1.0")

# CORS (tighten later if needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Security / rate limiting ------------------------------------------------
API_KEY = os.getenv("API_KEY", "")

RATE_LIMIT = 60  # requests per minute
_window = 60.0
_calls: Dict[str, List[float]] = {}

def _rate_limit(ip: str) -> bool:
    now = time.time()
    bucket = _calls.setdefault(ip, [])
    while bucket and now - bucket[0] > _window:
        bucket.pop(0)
    if len(bucket) >= RATE_LIMIT:
        return False
    bucket.append(now)
    return True

def _require_api_key(key: Optional[str]):
    expected = os.getenv("API_KEY", "")
    if not expected:
        return True  # dev mode
    return key == expected

# Allow unauthenticated health + privacy so probes/public policy work
@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path in ("/health", "/healthz", "/privacy"):
        return await call_next(request)

    ip = request.client.host if request.client else "unknown"
    if not _rate_limit(ip):
        return JSONResponse({"error": "rate_limit_exceeded"}, status_code=429)

    api_key = request.headers.get("x-api-key")
    if not _require_api_key(api_key):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    return await call_next(request)

# ---- Rules (synonyms/boosts) -------------------------------------------------
RULES_PATH = Path(__file__).resolve().parent.parent / "data" / "rules.yaml"
RULES: Dict = {}

def _load_rules() -> Dict:
    if RULES_PATH.exists():
        try:
            with open(RULES_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}
    return {}

@app.on_event("startup")
def _startup():
    global RULES
    RULES = _load_rules()

def _expand_queries(q: str) -> Tuple[List[str], List[str]]:
    """
    Returns (queries_to_try, favored_codes_for_boosting)
    - Expands the user's query using term_expansions in rules
    - Uses mappings to add favored section codes (e.g., S515)
    """
    queries = [q.strip()]
    favored_codes: List[str] = []

    q_low = q.lower()
    te = (RULES.get("term_expansions") or {})
    # if a key or any of its aliases appear, add those phrases as extra queries
    for key, phrases in te.items():
        if key in q_low or any(p in q_low for p in phrases):
            for p in phrases:
                if p not in queries:
                    queries.append(p)

    # mappings: if a phrase appears in q, add preferred codes to boost
    mappings = (RULES.get("mappings") or {})
    for phrase, codes in mappings.items():
        if phrase.lower() in q_low:
            for c in codes:
                if c not in favored_codes:
                    favored_codes.append(c)

    return queries, favored_codes

def _score_url(url: str, index_in_list: int, favored_codes: List[str]) -> float:
    """
    Higher score = ranked higher. Earlier URLs in the list get a small base,
    and URLs that include favored/boosted section codes get an extra boost.
    """
    score = 1.0 / (index_in_list + 1)  # earlier results get a bit more weight
    boosts = RULES.get("boosts") or {}
    # Municode URLs often contain "S###" in nodeId; reward matches
    for code, weight in boosts.items():
        if code in url.upper():
            score += float(weight)
    # If user’s query mapped to favored codes, give extra
    for code in favored_codes:
        if code in url.upper():
            score += 0.2
    return score

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
<p>This API retrieves public ordinance pages from <code>library.municode.com</code> for the City of Portland, TX and returns the text to client applications. It is read-only and does not modify external systems.</p>
<h2>Data processed</h2>
<ul>
  <li>Request details (endpoint and query parameters)</li>
  <li>Client IP (for rate limiting)</li>
  <li>Operational logs (timestamps and status codes)</li>
</ul>
<h2>Security</h2>
<ul>
  <li>HTTPS for all traffic</li>
  <li>Protected endpoints require an <code>x-api-key</code> header</li>
</ul>
<p>Contact: <a href="mailto:gisteam@portlandtx.gov">gisteam@portlandtx.gov</a></p>
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
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "PortlandOrdinanceBot/1.0"}) as client:
        r = await client.get(url, follow_redirects=True)
        r.raise_for_status()
        return r.text

def extract_section_fields(url: str, html: str) -> OrdinanceSection:
    soup = BeautifulSoup(html, "lxml")
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

# A more tolerant DuckDuckGo endpoint
DUCK = "https://html.duckduckgo.com/html/"

async def duckduck_search(query: str) -> List[str]:
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
    # de-duplicate
    seen = set()
    uniq: List[str] = []
    for u in links:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq

@app.get("/searchOrdinance", response_model=SearchResult)
async def search_ordinance(q: str = Query(..., min_length=2, description="Keywords to search within Municode for Portland, TX")):
    # 1) Expand the user query and gather favored codes
    queries, favored_codes = _expand_queries(q)

    # 2) Run searches for each query, collect URLs with basic scoring
    scored_urls: List[Tuple[float, str]] = []
    seen = set()
    for qi in queries:
        try:
            urls = await duckduck_search(qi)
            for idx, u in enumerate(urls):
                if u not in seen:
                    seen.add(u)
                    scored_urls.append((_score_url(u, idx, favored_codes), u))
        except Exception:
            continue

    if not scored_urls:
        return SearchResult(query=q, results=[])

    # 3) Sort by score (desc) and limit to top 5
    scored_urls.sort(key=lambda t: t[0], reverse=True)
    top_urls = [u for _, u in scored_urls[:5]]

    # 4) Fetch and parse each top URL
    results: List[OrdinanceSection] = []
    for url in top_urls:
        try:
            html = await fetch_url(url)
            results.append(extract_section_fields(url, html))
        except Exception:
            continue

    return SearchResult(query=q, results=results)
