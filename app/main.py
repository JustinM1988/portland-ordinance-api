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
from io import BytesIO
from urllib.parse import urlparse, parse_qs, unquote
from docx import Document

app = FastAPI(title="Portland Ordinance API", version="1.2.0")

# ---------------- CORS ----------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- Security / rate limit ----------------
API_KEY = os.getenv("API_KEY", "")
RATE_LIMIT = 60
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
        return True
    return key == expected

@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path in ("/health", "/healthz", "/privacy"):
        return await call_next(request)
    ip = request.client.host if request.client else "unknown"
    if not _rate_limit(ip):
        return JSONResponse({"error": "rate_limit_exceeded"}, status_code=429)
    api_key = request.headers.get("x-api-key")
    if not _require_api_key(api_key):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)

# ---------------- App routes ----------------
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
    return {"ok": True, "version": app.version, "source": "municode", "base": MUNICODE_BASE}

@app.get("/healthz")
async def healthz():
    return {"ok": True}

# ---------- Privacy ----------
PRIVACY_HTML = """
<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Privacy Policy — Portland Ordinance API</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;line-height:1.55}code{background:#f4f4f4;padding:2px 4px;border-radius:4px}</style>
</head><body>
<h1>Privacy Policy — Portland Ordinance API</h1>
<p><strong>Effective:</strong> {{date}}</p>
<p>This API retrieves public ordinance pages from <code>library.municode.com</code> (and official Municode DOCX downloads) for the City of Portland, TX. It is read-only.</p>
<p>No cookies. Rate-limiting data is ephemeral. Render may keep short-lived logs.</p>
<p>Contact: <a href="mailto:gisteam@portlandtx.gov">gisteam@portlandtx.gov</a></p>
</body></html>
"""

@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return HTMLResponse(PRIVACY_HTML.replace("{{date}}", date.today().strftime("%B %d, %Y")))

# ---------- Helpers ----------
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 PortlandOrdinanceBot/1.0"
    )
}

def _clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for s in soup(["script", "style", "noscript", "iframe"]):
        s.decompose()
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)

async def fetch_text(url: str) -> str:
    timeout = httpx.Timeout(20.0, connect=20.0)
    async with httpx.AsyncClient(timeout=timeout, headers=UA) as client:
        r = await client.get(url, follow_redirects=True)
        r.raise_for_status()
        return r.text

async def fetch_bytes(url: str) -> bytes:
    timeout = httpx.Timeout(30.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout, headers=UA) as client:
        r = await client.get(url, follow_redirects=True)
        r.raise_for_status()
        return r.content

def extract_section_fields(url: str, html: str) -> OrdinanceSection:
    soup = BeautifulSoup(html, "lxml")
    title = None
    section_number = None
    headings: List[str] = []
    for h in soup.find_all(["h1", "h2", "h3"]):
        t = h.get_text(" ", strip=True)
        if t: headings.append(t)
        if t and not title: title = t
    if title:
        m = re.search(r"(§+\s*\d[\w\.\-]*)", title)
        if m: section_number = m.group(1)
    text = _clean_text(html)
    snippet = (text[:300] + "…") if len(text) > 300 else text
    return OrdinanceSection(
        section_number=section_number, title=title, url=url,
        snippet=snippet, text=text, headings=[h for h in headings if h]
    )

def _normalize_office_viewer(url: str) -> str:
    try:
        pu = urlparse(url)
        if "view.officeapps.live.com" in pu.netloc and pu.path.startswith("/op/view.aspx"):
            src = parse_qs(pu.query).get("src", [])
            if src: return unquote(src[0])
    except Exception:
        pass
    return url

def _is_municode_html(url: str) -> bool:
    return "library.municode.com" in url

def _is_municode_docx(url: str) -> bool:
    return ("mcclibrary.blob.core.usgovcloudapi.net" in url) or url.lower().endswith(".docx")

def _title_from_filename(url: str) -> Optional[str]:
    try:
        name = os.path.basename(urlparse(url).path)
        name = unquote(name)
        name = re.sub(r"[_\-]+", " ", name)
        return name.strip()
    except Exception:
        return None

def _extract_docx_text(bin_data: bytes) -> str:
    doc = Document(BytesIO(bin_data))
    parts = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    text = "\n".join(parts)
    return re.sub(r"\n{3,}", "\n\n", text)

def _find_docx_link_in_html_page(html: str) -> Optional[str]:
    """Find the 'blue W' download link on a Municode page."""
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "codesgenerateddownloaddocx" in href or href.lower().endswith(".docx"):
            return href
    return None

async def fetch_any(url: str) -> OrdinanceSection:
    """
    Try HTML first; if content is placeholder/too short, fall back:
    find the DOCX link on the page and parse the DOCX instead.
    Also accepts direct DOCX or Office viewer URLs.
    """
    url = _normalize_office_viewer(url)

    # Direct DOCX
    if _is_municode_docx(url):
        data = await fetch_bytes(url)
        text = _extract_docx_text(data)
        title = _title_from_filename(url) or "Municode DOCX"
        snippet = (text[:300] + "…") if len(text) > 300 else text
        return OrdinanceSection(
            section_number=None, title=title, url=url,
            snippet=snippet, text=text, headings=[]
        )

    # HTML (with fallback)
    if _is_municode_html(url):
        html = await fetch_text(url)
        section = extract_section_fields(url, html)

        # Heuristic: fallback if page looks empty/placeholder
        if (not section.text) or (section.text.strip() == "Municode Library") or (len(section.text) < 400):
            maybe_docx = _find_docx_link_in_html_page(html)
            if maybe_docx:
                if maybe_docx.startswith("/"):
                    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
                    maybe_docx = base + maybe_docx
                data = await fetch_bytes(maybe_docx)
                text = _extract_docx_text(data)
                title = section.title or _title_from_filename(maybe_docx) or "Municode DOCX"
                snippet = (text[:300] + "…") if len(text) > 300 else text
                return OrdinanceSection(
                    section_number=section.section_number, title=title,
                    url=maybe_docx, snippet=snippet, text=text, headings=section.headings or []
                )
        return section

    raise HTTPException(
        status_code=400,
        detail="Only Municode HTML pages or official Municode DOCX download links are supported."
    )

# ---------- Endpoints ----------
@app.get("/fetchByUrl", response_model=OrdinanceSection)
async def fetch_by_url(url: str = Query(..., description="Municode HTML section URL or official DOCX link")):
    try:
        return await fetch_any(url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch content: {e}")

class SearchResult(BaseModel):
    results: List[OrdinanceSection]
    query: str

DUCK = "https://html.duckduckgo.com/html/"

async def ddg_search(query: str) -> List[str]:
    q = f"site:library.municode.com/tx/portland {query}"
    params = {"q": q}
    timeout = httpx.Timeout(20.0, connect=20.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=UA) as client:
            r = await client.get(DUCK, params=params, follow_redirects=True)
            if r.status_code != 200 or not r.text:
                return []
            html = r.text
    except Exception as e:
        print("duckduck_search error:", repr(e))
        return []
    soup = BeautifulSoup(html, "lxml")
    links = [a.get("href") for a in soup.select("a.result__a") if a.get("href")]
    if not links:
        links = [a["href"] for a in soup.find_all("a", href=True) if "library.municode.com" in a["href"]]
    seen, uniq = set(), []
    for u in links:
        if "library.municode.com" in u and u not in seen:
            seen.add(u); uniq.append(u)
        if len(uniq) >= 5: break
    return uniq

@app.get("/searchOrdinance", response_model=SearchResult)
async def search_ordinance(q: str = Query(..., min_length=2)):
    urls = await ddg_search(q)
    if not urls:
        return SearchResult(query=q, results=[])
    results: List[OrdinanceSection] = []
    for url in urls:
        try:
            results.append(await fetch_any(url))
        except Exception as e:
            print("fetch_any error:", repr(e))
            continue
    return SearchResult(query=q, results=results)
