@app.get("/fetchByUrl", response_model=OrdinanceSection)
async def fetch_by_url(url: str = Query(..., description="Direct Municode section URL (HTML or DOCX)")):
    # Accept Municode HTML or the official DOCX download
    if _is_municode_docx(url):
        try:
            data = await fetch_bytes(url)
            text = _extract_docx_text(data)
            title = _title_from_filename(url)
            snippet = (text[:300] + "…") if len(text) > 300 else text
            return OrdinanceSection(
                section_number=None,
                title=title,
                url=url,
                snippet=snippet,
                text=text,
                headings=[],
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to fetch or parse DOCX: {e}")

    if _is_municode_html(url):
        # First try the normal HTML path
        html = await fetch_url(url)
        sec = extract_section_fields(url, html)

        # If Municode returned only a placeholder or almost no text, try the DOCX fallback
        bad = (not sec.text) or (sec.text.strip().lower() == "municode library") or (len(sec.text.strip()) < 120)
        if bad:
            docx_url = _find_docx_link_in_html(html)
            if docx_url:
                try:
                    data = await fetch_bytes(docx_url)
                    text = _extract_docx_text(data)
                    title = sec.title or _title_from_filename(docx_url)
                    snippet = (text[:300] + "…") if len(text) > 300 else text
                    return OrdinanceSection(
                        section_number=sec.section_number,
                        title=title,
                        url=url,             # keep the original page URL
                        snippet=snippet,
                        text=text,
                        headings=sec.headings or [],
                    )
                except Exception:
                    # If DOCX fails, return the best we have
                    pass

        return sec

    # If it's neither Municode HTML nor the official DOCX host, reject it
    raise HTTPException(status_code=400, detail="Only Municode HTML pages or Municode DOCX download links are supported.")
