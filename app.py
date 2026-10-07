"""
PageSpeed PDF / screenshot service (FastAPI + Playwright, async).

Setup:
    pip install -r requirements.txt
    playwright install chromium

Run:
    uvicorn app:app --host 0.0.0.0 --port 8000

Call:
    curl -o report.pdf "http://localhost:8000/screenshots?url=https://example.com&token=YOUR_SECRET"
    (token can also be sent as header:  X-API-Token: YOUR_SECRET)

    output=pdf          (default) -> PDF bytes returned directly, nothing saved to disk
    output=screenshots  -> PNGs saved in SCREENSHOT_DIR/<run>/, JSON response
"""

import asyncio
import hmac
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

# Always load .env from this file's folder, regardless of where uvicorn is started
load_dotenv(Path(__file__).resolve().parent / ".env")

SECRET = os.getenv("API_SECRET_TOKEN", "")
WAIT_MS = int(os.getenv("WAIT_MS", "5000"))                    # delay before 1st screenshot
REPORT_TIMEOUT_MS = int(os.getenv("REPORT_TIMEOUT_MS", "300000"))  # max wait for lab results (5 min)
SETTLE_MS = int(os.getenv("SETTLE_MS", "3000"))                # extra wait after results appear
PRE_PDF_WAIT_MS = int(os.getenv("PRE_PDF_WAIT_MS", "2000"))    # final wait right before printing
DEBUG_DIR = os.getenv("DEBUG_DIR", "")                         # if set: save screenshot+HTML when results time out
SCREENSHOT_DIR = Path(os.getenv("SCREENSHOT_DIR", "./screenshots")).resolve()
USE_SYSTEM_CHROME = os.getenv("USE_SYSTEM_CHROME", "false").lower() == "true"
PDF_SINGLE_PAGE = os.getenv("PDF_SINGLE_PAGE", "true").lower() == "true"
MAX_PDF_HEIGHT_PX = 18000  # Chromium caps PDF page size (~200in); above this we paginate
VIEWPORT_WIDTH = 1440

app = FastAPI(title="PageSpeed Screenshotter")

# Screenshot folder is only created / served when screenshots are used
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=SCREENSHOT_DIR), name="files")

_semaphore = asyncio.Semaphore(int(os.getenv("MAX_CONCURRENT", "1")))

SECTIONS = [
    ("03_diagnose_performance_issues", lambda p: p.locator("div.nfmh5d", has_text="Diagnose performance issues")),
    ("04_metrics", lambda p: p.locator("span.lh-audit-group__title", has_text=re.compile(r"^Metrics$"))),
    ("05_insights", lambda p: p.locator("span.lh-audit-group__title", has_text=re.compile(r"^Insights$"))),
    ("06_diagnostics", lambda p: p.locator("span.lh-audit-group__title", has_text=re.compile(r"^Diagnostics$"))),
]


def check_token(token: Optional[str]) -> None:
    if not SECRET:
        raise HTTPException(500, "Server misconfigured: API_SECRET_TOKEN not set")
    if not token or not hmac.compare_digest(token.encode(), SECRET.encode()):
        raise HTTPException(401, "Invalid or missing token")


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "url must be a valid http(s) URL")
    return url


async def snap(page, folder: Path, name: str, saved: list, full_page: bool = False) -> None:
    path = folder / f"{name}.png"
    await page.screenshot(path=str(path), full_page=full_page)
    saved.append(path.name)


async def wait_for_results(page) -> bool:
    """
    Block until the Lighthouse LAB report is rendered (can take 2+ minutes).

    The field-data section and headings like "Diagnose performance issues" are on the page
    from the start, so they say nothing about the lab run. The lab report is the
    `.lh-scores-container` block (the row of score gauges), which only exists after the
    analysis finishes. We wait for it, then for the Performance score to hold a number.

    Returns False if it doesn't appear within REPORT_TIMEOUT_MS (default 5 minutes).
    """
    try:
        # 1) the scores container exists and is visible
        await page.locator(".lh-scores-container").first.wait_for(state="visible", timeout=REPORT_TIMEOUT_MS)

        # 2) the Performance gauge contains an actual number
        await page.locator(
            ".lh-scores-container a.lh-gauge__wrapper[href=\"#performance\"] .lh-gauge__percentage",
            has_text=re.compile(r"\d"),
        ).first.wait_for(state="visible", timeout=60000)
    except PWTimeout:
        return False

    # Soft waits (best effort): the rest of the report rendered, network quiet
    try:
        await page.locator("span.lh-audit-group__title", has_text=re.compile(r"^Diagnostics$")).first.wait_for(
            state="attached", timeout=20000
        )
    except PWTimeout:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass

    await page.wait_for_timeout(SETTLE_MS)  # gauge arc / score animations
    return True


async def save_debug(page) -> None:
    """When DEBUG_DIR is set, keep a screenshot + HTML of the page we gave up on."""
    if not DEBUG_DIR:
        return
    d = Path(DEBUG_DIR)
    d.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d_%H%M%S}"
    await page.screenshot(path=str(d / f"timeout_{stamp}.png"), full_page=True)
    (d / f"timeout_{stamp}.html").write_text(await page.content(), encoding="utf-8")


async def run_capture(target_url: str, output: str, folder: Optional[Path]) -> dict:
    """output: 'pdf' | 'screenshots'. Returns {'screenshots': [...], 'pdf': bytes | None}."""
    want_shots = output == "screenshots"
    want_pdf = output == "pdf"
    saved: list = []
    pdf_bytes: Optional[bytes] = None

    async with async_playwright() as pw:
        launch_kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if USE_SYSTEM_CHROME:
            launch_kwargs["channel"] = "chrome"
        browser = await pw.chromium.launch(**launch_kwargs)

        context = await browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            await page.goto("https://pagespeed.web.dev/", wait_until="domcontentloaded", timeout=60000)

            url_input = page.locator('input[name="url"]').first
            await url_input.wait_for(state="visible", timeout=30000)
            await url_input.fill(target_url)
            await url_input.press("Enter")

            await page.wait_for_timeout(WAIT_MS)
            if want_shots:
                await snap(page, folder, "01_after_submit", saved)

            if not await wait_for_results(page):
                await save_debug(page)
                if want_shots:
                    await snap(page, folder, "02_results_timeout", saved)
                return {"screenshots": saved, "pdf": None}

            # Scroll through the whole page once so lazy content is rendered
            await page.evaluate(
                """async () => {
                    for (let y = 0; y < document.body.scrollHeight; y += 600) {
                        window.scrollTo(0, y);
                        await new Promise(r => setTimeout(r, 120));
                    }
                    window.scrollTo(0, 0);
                }"""
            )
            await page.wait_for_timeout(800)

            if want_shots:
                await snap(page, folder, "02_report_top", saved)
                for name, locator_factory in SECTIONS:
                    try:
                        el = locator_factory(page).first
                        await el.scroll_into_view_if_needed(timeout=10000)
                        await page.wait_for_timeout(700)
                        await snap(page, folder, name, saved)
                    except PWTimeout:
                        continue  # section missing on this page
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(500)
                await snap(page, folder, "99_full_page", saved, full_page=True)

            if want_pdf:
                await page.evaluate("window.scrollTo(0, 0)")
                # final pause so the .lh-scores-container and rest of the report are fully painted
                await page.wait_for_timeout(PRE_PDF_WAIT_MS)
                await page.emulate_media(media="screen")
                height = await page.evaluate(
                    "Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)"
                )
                # No `path` given -> Playwright returns the PDF as bytes (nothing written to disk)
                if PDF_SINGLE_PAGE and height <= MAX_PDF_HEIGHT_PX:
                    pdf_bytes = await page.pdf(
                        width=f"{VIEWPORT_WIDTH}px",
                        height=f"{height + 2}px",
                        print_background=True,
                        page_ranges="1",
                        margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                    )
                else:
                    pdf_bytes = await page.pdf(
                        format="A4",
                        print_background=True,
                        margin={"top": "10mm", "right": "10mm", "bottom": "10mm", "left": "10mm"},
                    )
        finally:
            await context.close()
            await browser.close()

    return {"screenshots": saved, "pdf": pdf_bytes}


@app.get("/screenshots")
async def screenshots(
    url: str = Query(..., description="URL to analyse on PageSpeed Insights"),
    token: Optional[str] = Query(None, description="Secret token (or use X-API-Token header)"),
    output: str = Query("pdf", pattern="^(pdf|screenshots)$", description="pdf | screenshots"),
    x_api_token: Optional[str] = Header(None),
):
    check_token(token or x_api_token)
    validate_url(url)

    folder = None
    run_id = None
    if output == "screenshots":
        run_id = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
        folder = SCREENSHOT_DIR / run_id
        folder.mkdir(parents=True, exist_ok=True)

    async with _semaphore:
        try:
            result = await run_capture(url, output, folder)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"Capture failed: {exc}") from exc

    if output == "pdf":
        if not result["pdf"]:
            raise HTTPException(504, "PageSpeed results did not finish loading in time")
        host = urlparse(url).netloc.replace(":", "_")
        return Response(
            content=result["pdf"],
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="pagespeed_{host}.pdf"'},
        )

    return {
        "url": url,
        "folder": str(folder),
        "count": len(result["screenshots"]),
        "files": [f"/files/{run_id}/{n}" for n in result["screenshots"]],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}