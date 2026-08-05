#!/usr/bin/env python3
"""
Auto-fetch the latest EU Weekly Oil Bulletin 'Prices with taxes' spreadsheet
and save it to data/manual/eu_wob_prices.xlsx.

The EU page builds its download links with JavaScript, so a plain HTTP fetch
can't see them - we render the page with a headless browser (Playwright) to
read the links, then download the newest 'prices with taxes' xlsx.

SAFE BY DESIGN: on ANY problem it prints why and exits 0 without changing the
existing file, so a bad week can never break the map. It only overwrites the
file when it has a valid, parseable, newer workbook.
"""
import io
import os
import re
import sys
import zipfile
import urllib.request

PAGE = "https://energy.ec.europa.eu/data-and-analysis/weekly-oil-bulletin_en"
OUT = os.path.join("data", "manual", "eu_wob_prices.xlsx")
UA = "Mozilla/5.0 (compatible; FuelGrid-bot/1.0)"


def rendered_anchors():
    """Return every link on the fully-rendered page as {href, text}."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        page.goto(PAGE, wait_until="networkidle", timeout=90000)
        try:
            page.wait_for_timeout(3000)          # let late JS settle
        except Exception:
            pass
        anchors = page.eval_on_selector_all(
            "a", "els => els.map(e => ({href: e.href, text: (e.textContent||'').trim()}))")
        browser.close()
    return anchors


def date_in(text):
    m = re.search(r"(20\d\d)-(\d\d)-(\d\d)", text or "")
    return m.group(0) if m else ""


def pick_candidates(anchors):
    """'Prices with taxes' download links, newest first."""
    cands = []
    for a in anchors:
        href = a.get("href") or ""
        text = a.get("text") or ""
        blob = (href + " " + text).lower()
        if "without" in blob:                      # skip 'prices WITHOUT taxes'
            continue
        is_prices_taxes = ("prices with taxes" in blob) or ("with taxes" in blob) or ("prices" in blob and "taxes" in blob)
        looks_file = ("document/download" in href) or (".xlsx" in blob) or ("filename=" in href)
        if is_prices_taxes and looks_file:
            cands.append({"href": href, "text": text, "date": date_in(href + " " + text)})
    cands.sort(key=lambda a: a["date"], reverse=True)
    return cands


def validate_xlsx(data):
    """True if bytes are a real workbook containing country rows."""
    if data[:2] != b"PK":
        return False
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        blob = ""
        for part in ("xl/sharedStrings.xml", "xl/worksheets/sheet1.xml"):
            if part in z.namelist():
                blob += z.read(part).decode("utf-8", "ignore")
        return ("Romania" in blob or "Germany" in blob or "Czechia" in blob)
    except Exception:
        return False


def current_saved_date():
    """Date (yyyy-mm-dd) of the file already in the repo, if any."""
    if not os.path.exists(OUT):
        return ""
    try:
        z = zipfile.ZipFile(OUT)
        sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8", "ignore")
        m = re.search(r'<c r="A2"[^>]*><v>(\d+)', sheet)
        if m:
            import datetime as dt
            d = dt.date(1899, 12, 30) + dt.timedelta(days=int(m.group(1)))
            return d.isoformat()
    except Exception:
        pass
    return ""


def main():
    try:
        anchors = rendered_anchors()
    except Exception as exc:
        print(f"Could not render the EU page: {exc}")
        print("Leaving the existing bulletin file untouched.")
        return 0

    print(f"Links on rendered page: {len(anchors)}")
    cands = pick_candidates(anchors)
    print(f"'Prices with taxes' download candidates: {len(cands)}")
    for c in cands[:8]:
        print(f"   [{c['date'] or '????-??-??'}] {c['text'][:55]} -> {c['href'][:110]}")

    if not cands:
        print("No candidate link found - the page layout may have changed.")
        print("Leaving the existing file untouched (manual upload still works).")
        return 0

    best = cands[0]
    url = best["href"]
    print(f"Downloading newest: {best['date'] or '(no date in name)'} -> {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        data = urllib.request.urlopen(req, timeout=90).read()
    except Exception as exc:
        print(f"Download failed: {exc} - keeping existing file.")
        return 0

    if not validate_xlsx(data):
        print("Downloaded file is not a valid bulletin workbook - keeping existing file.")
        return 0

    old = current_saved_date()
    new = best["date"]
    print(f"existing file date: {old or '(none)'} | downloaded date: {new or '(unknown)'}")
    if old and new and new <= old:
        print("Not newer than what we already have - no change.")
        return 0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "wb") as f:
        f.write(data)
    print(f"Updated {OUT} ({len(data)} bytes). New bulletin saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
