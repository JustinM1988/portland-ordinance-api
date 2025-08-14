from fastapi import FastAPI, HTTPException, Query, Request, Body, Depends
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Tuple
import os, re, time, json, pathlib, datetime
from collections import defaultdict, Counter

import httpx
from bs4 import BeautifulSoup
import yaml  # PyYAML

# -------------------------------
# App + CORS
# -------------------------------
app = FastAPI(title="Portland Ordinance API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------
# Config / constants
# -------------------------------
API_KEY = os.getenv("API_KEY", "")               # for normal protected endpoints
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")       # for admin endpoints (approval)
PRIVACY_EMAIL = os.getenv("PRIVACY_CONTACT_EMAIL", "gisteam@portlandtx.gov")

BASE_MUNICODE = os.getenv(
    "BASE_MUNICODE",
    "https://library.municode.com/tx/portland/codes/code_of_ordinances?nodeId=COOR_APXAUNDEOR",
)

DATA_DIR = os.getenv("DATA_DIR", "/opt/render/project/src/data")
pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

RULES_PATH = os.path.join(DATA_DIR, "rules.yaml")
FEEDBACK_PATH = os.path.join(DATA_DIR, "feedback.jsonl")
SUGGESTIONS_PATH = os.path.join(DATA_DIR, "suggestions.json")

DUCK = "https://html.duckduckgo.com/html/"  # tolerant endpoint


# -------------------------------
# Security / Rate limiting
# -------------------------------
RATE_LIMIT = 60  # req/min per IP
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

def _require_api_key(key: Optional[str]) -> bool:
    if not API_KEY:
        return True  # dev / if not set
    return key == API_KEY

def _require_admin_token(key: Optional[str]) -> bool:
    if not ADMIN_TOKEN:
        # no admin token set -> block admin
        return False
    return key == ADMIN_TOKEN

@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    # allow these without key/rate-limit
    if path in ("/health", "/healthz", "/privacy"):
        return await call_next(request)

    # rate-limit
    ip = request.client.host if request.client else "unknown"
    if not _rate_limit(ip):
        return JSONResponse({"error": "rate_limit_exceeded"}, status_code=429)

    # key for everything else
    api_key = request.headers.get("x-api-key")
    if not _require_api_key(api_key):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    return await call_next(request)


# -------------------------------
# Models
# -------------------------------
class OrdinanceSection(BaseModel):
    section_number: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    snippet: Optional[str] = None
    text: Optional[str] = None
    headings: Optional[List[str]] = None

class SearchResult(BaseModel):
    results: List[OrdinanceSection]
    query: str

class FeedbackIn(BaseModel):
    answer_id: str = Field(..., description="Client's answer/result id")
    query: str
    rating: int = Field(..., ge=1, le=5)
    comment: Optional[str] = None
    suggested_urls: Optional[List[str]] = None

# -------------------------------
# Privacy HTML
# -------------------------------
PRIVACY_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Privacy Policy — Portland Ordinance API</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;line-height:1.55}</style>
</head><body>
<h1>Privacy Policy — Portland Ordinance API</h1>
<p><strong>Effective:</strong> {date}</p>
<p>This API retrieves public ordinance pages from <code>library.municode.com</code> for the City of Portland, TX and returns the text to client applications for analysis and citation. It is read-only.</p>
<h2>Data processed</h2>
<ul>
<li>Request details (endpoint + query)</li>
<li>IP (for rate limiting)</li>
<li>Operational logs</li>
</ul>
<h2>Retention</h2>
<ul>
<li>Rate-limit buckets are in-memory.</li>
<li>Host may keep short-lived logs.</li>
</ul>
<h2>Sharing</h2>
<ul><li>Requests go to <code>library.municode.com</code>.</li></ul>
<h2>Security</h2>
<ul>
<li>HTTPS enforced.</li>
<li>Protected endpoints require <code>x-api-key</code>.</li>
</ul>
<h2>Contact</h2>
<p>Email: <a href="mailto:{email}">{email}</a></p>
</body></html>
"""

# -------------------------------
# Helpers: rules I/O
# -------------------------------
DEFAULT_RULES = {
    "term_expansions": {
        # seed a couple to show how it works
        "led": ["leisure and entertainment district", "sec. 515"],
        "bar": ["alcoholic beverages", "on-premises consumption"],
        "fence": ["fences", "screening"],
    },
    "mappings": {
        # human phrase -> sections to prefer
        "leisure and entertainment district": ["S515"],
    },
    "boosts": {
        # tokens we search for inside URL to boost it
        "S515": 0.6,
        "S406": 0.4
    }
}

def load_rules() -> Dict[str, Any]:
    if not os.path.exists(RULES_PATH):
        # write defaults once
        save_rules(DEFAULT_RULES)
        return DEFAULT_RULES
    try:
        with open(RULES_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # merge with defaults so keys always present
        out = {**DEFAULT_RULES}
        for k, v in (data or {}).items():
            out[k] = v or out.get(k, {})
        return out
    except Exception:
        return DEFAULT_RULES

def save_rules(rules: Dict[str, Any]) -> None:
    with open(RULES_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(rules, f, sort_keys=False, allow_unicode=True)

RULES = load_rules()

def _boost_score(url: str, base: float = 1.0) -> float:
    url_l = url.lower()
    score = base
    boosts: Dict[str, float] = RULES.get("boosts", {})
    for token, weight in boosts.items():
        if token.lower() in url_l:
            score += float(weight)
    return score

def _expand_terms(q: str) -> List[str]:
    out = [q]
    terms = RULES.get("term_expansions", {})
    q_l = q.lower()
    for term, expansions in terms.items():
        if term in q_l:
            out.extend(expansions)
    return list(dict.fromkeys(out))  # dedupe, keep order

def _implied_sections(q: str) -> List[str]:
    """From mappings dict find preferred sections."""
    prefs = []
    maps = RULES.get("mappings", {})
    q_l = q.lower()
    for k, vals in maps.items():
        if k in q_l:
            prefs.extend(vals)
    return list(dict.fromkeys(prefs))


# -------------------------------
# Municode scraping
# -------------------------------
def _clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for s in soup(["script", "style", "noscript", "iframe"]):
        s.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text

async def fetch_url(url: str) -> str:
    timeout = httpx.Timeout(25.0, connect=25.0)
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent":"PortlandOrdinanceBot/2.0"}) as client:
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

async def duckduck_search(query: str, max_links: int = 6) -> List[str]:
    # site-limited search to Municode: city of Portland TX
    q = f'site:library.municode.com tx portland code of ordinances {query}'
    params = {"q": q}
    timeout = httpx.Timeout(25.0, connect=25.0)
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent":"PortlandOrdinanceBot/2.0"}) as client:
        r = await client.get(DUCK, params=params)
        r.raise_for_status()
        html = r.text

    soup = BeautifulSoup(html, "lxml")
    links: List[str] = []
    for a in soup.select("a.result__a"):
        href = a.get("href")
        if href and "library.municode.com" in href:
            links.append(href)

    seen = set()
    uniq = []
    for u in links:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
        if len(uniq) >= max_links:
            break
    return uniq


# -------------------------------
# Public endpoints
# -------------------------------
@app.get("/health")
async def health():
    return {"ok": True, "source": "municode", "base": BASE_MUNICODE}

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    today = datetime.date.today().strftime("%B %d, %Y")
    return HTMLResponse(PRIVACY_HTML.format(date=today, email=PRIVACY_EMAIL))

@app.get("/fetchByUrl", response_model=OrdinanceSection)
async def fetch_by_url(url: str = Query(..., description="Direct Municode URL only")):
    if "library.municode.com" not in url:
        raise HTTPException(status_code=400, detail="Only Municode URLs are supported.")
    html = await fetch_url(url)
    return extract_section_fields(url, html)

@app.get("/searchOrdinance", response_model=SearchResult)
async def search_ordinance(
    q: str = Query(..., min_length=2, description="Keywords for Portland, TX Municode")
):
    # expand terms -> multiple passes
    queries = _expand_terms(q)
    implied = _implied_sections(q)  # e.g., ["S515"]

    scored: Dict[str, float] = {}
    for idx, term in enumerate(queries):
        try:
            urls = await duckduck_search(term)
        except Exception:
            continue
        base = 1.0 - (idx * 0.05)  # slight discount for later passes
        for u in urls:
            score = _boost_score(u, base=base)
            # if implied sections, give a small extra nudge if URL contains them
            for sec in implied:
                if sec.lower() in u.lower():
                    score += 0.5
            scored[u] = max(scored.get(u, 0), score)

    # pick top 5
    top_urls = [u for u, _ in sorted(scored.items(), key=lambda kv: kv[1], reverse=True)[:5]]
    results: List[OrdinanceSection] = []
    for u in top_urls:
        try:
            html = await fetch_url(u)
            results.append(extract_section_fields(u, html))
        except Exception:
            continue

    return SearchResult(query=q, results=results)


# -------------------------------
# Feedback capture
# -------------------------------
def _append_feedback_row(row: Dict[str, Any]) -> None:
    with open(FEEDBACK_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

@app.post("/feedback")
async def receive_feedback(payload: FeedbackIn):
    row = payload.dict()
    row["ts"] = datetime.datetime.utcnow().isoformat() + "Z"
    _append_feedback_row(row)
    return {"ok": True, "stored": True}

# -------------------------------
# Suggestions builder
# -------------------------------
SEC_RX = re.compile(r"(Sec\.?\s*[\dA-Za-z\.\-]+|§\s*[\dA-Za-z\.\-]+)")

def _extract_sections_from_text(text: str) -> List[str]:
    if not text:
        return []
    hits = SEC_RX.findall(text)
    # normalize e.g., "Sec. 515" -> S515
    norm = []
    for h in hits:
        s = re.sub(r"[^0-9A-Za-z\.]", "", h)  # drop spaces/symbols
        s = s.replace("Sec.", "").replace("sec.", "")
        s = s.strip(".")
        # keep digits + letter suffix if any -> "515" or "406"
        m = re.search(r"([0-9]{2,4}[A-Za-z\-\.]*)", s)
        if m:
            norm.append("S" + m.group(1))
    return list(dict.fromkeys(norm))

def build_suggestions(feedback_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    From low-rated feedback (<=3) produce proposed:
      - term_expansions (based on frequent tokens used in comments)
      - mappings (query -> sections)
      - boosts (sections seen frequently)
    """
    proposals = {
        "term_expansions": defaultdict(set),
        "mappings": defaultdict(set),
        "boosts": defaultdict(float),
        "stats": {}
    }

    for row in feedback_rows:
        rating = row.get("rating", 5)
        if rating > 3:
            continue  # we learn mostly from the misses
        query = (row.get("query") or "").lower().strip()
        comment = (row.get("comment") or "")
        sugg_urls = row.get("suggested_urls") or []

        # collect sections from comment and URLs
        secs = set(_extract_sections_from_text(comment))
        for u in sugg_urls:
            # try to find S### tokens in URL chunks
            m = re.findall(r"_S([0-9A-Za-z]+)", u)
            for s in m:
                secs.add("S" + s)

        # if comment includes phrases like "LED", propose expansions
        tokens = []
        if comment:
            tokens = [t.strip().lower() for t in re.split(r"[,;/\|\(\)\[\]\{\}]", comment) if t.strip()]
        # heuristics: short terms (<= 4) or words with capitals in original comment
        # but keep it simple—just offer token as expansion to the query
        for t in tokens:
            if len(t) <= 20 and t not in query and " " in t:
                proposals["term_expansions"][query].add(t)

        # map query to sections that were referenced
        for s in secs:
            proposals["mappings"][query].add(s)
            proposals["boosts"][s] += 0.2  # small confidence weight

    # convert sets -> lists
    out = {
        "term_expansions": {k: sorted(list(v)) for k, v in proposals["term_expansions"].items() if v},
        "mappings": {k: sorted(list(v)) for k, v in proposals["mappings"].items() if v},
        "boosts": {k: round(v, 2) for k, v in proposals["boosts"].items() if v > 0},
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "count_source_rows": len(feedback_rows),
    }
    return out

def _read_feedback_rows() -> List[Dict[str, Any]]:
    rows = []
    if not os.path.exists(FEEDBACK_PATH):
        return rows
    with open(FEEDBACK_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows

# -------------------------------
# Admin endpoints
# -------------------------------
def admin_guard(request: Request):
    token = request.headers.get("x-admin-token")
    if not _require_admin_token(token):
        raise HTTPException(status_code=401, detail="admin_unauthorized")

@app.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request):
    admin_guard(request)
    return HTMLResponse("""
    <h1>PORTLAND ORDINANCE — Admin</h1>
    <p>Use the JSON endpoints below (send header <code>x-admin-token: &lt;ADMIN_TOKEN&gt;</code>):</p>
    <ul>
      <li>GET <code>/rules</code> — current loaded rules</li>
      <li>GET <code>/admin/suggestions</code> — build suggestions from feedback</li>
      <li>POST <code>/admin/approve</code> — apply a suggestion
        <pre>{
  "kind": "mapping" | "term_expansions" | "boosts",
  "key": "your-key",
  "values": ["S515"]   // or strings for expansions; for boosts use {"S515": 0.6}
}</pre>
      </li>
    </ul>
    """.strip())

@app.get("/rules")
async def get_rules(request: Request):
    admin_guard(request)
    return RULES

@app.get("/admin/suggestions")
async def admin_suggestions(request: Request):
    admin_guard(request)
    rows = _read_feedback_rows()
    suggestions = build_suggestions(rows)
    with open(SUGGESTIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(suggestions, f, ensure_ascii=False, indent=2)
    return suggestions

@app.post("/admin/approve")
async def admin_approve(request: Request, body: Dict[str, Any] = Body(...)):
    admin_guard(request)
    kind = body.get("kind")
    key = body.get("key")
    values = body.get("values")

    if kind not in ("term_expansions", "mappings", "boosts"):
        raise HTTPException(status_code=400, detail="invalid kind")
    if not key:
        raise HTTPException(status_code=400, detail="missing key")

    rules = load_rules()

    if kind == "boosts":
        # allow dict of {token: weight} or a single token string
        if isinstance(values, dict):
            for k, v in values.items():
                rules.setdefault("boosts", {})[str(k)] = float(v)
        elif isinstance(values, list):
            for token in values:
                rules.setdefault("boosts", {})[str(token)] = float(0.3)
        else:
            raise HTTPException(status_code=400, detail="boosts expects dict or list")
    else:
        # term_expansions / mappings both {key: [values]}
        if not isinstance(values, list):
            raise HTTPException(status_code=400, detail="values must be list")
        bucket = rules.setdefault(kind, {})
        cur = set([*(bucket.get(key, []) or [])])
        for v in values:
            cur.add(str(v))
        bucket[key] = sorted(list(cur))

    save_rules(rules)
    # reload for next requests
    global RULES
    RULES = load_rules()

    return {"ok": True, "updated": kind, "key": key, "values": values}
