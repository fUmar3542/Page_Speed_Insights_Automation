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
    output=json         -> stats as JSON, nothing saved to disk
                           filter=bad (default) returns only metrics that are NOT "good"
                           filter=all  returns every metric plus the raw values
    output=screenshots  -> PNGs saved in SCREENSHOT_DIR/<run>/, JSON response
"""

import asyncio
import contextvars
import hmac
import logging
import os
import re
import sys
import time
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

# ----------------------------------------------------------------------------
# Logging: every line carries a short per-request id, e.g.  12:01:07 INFO [a1b2c3d4] ...
# ----------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
request_id: contextvars.ContextVar = contextvars.ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.rid = request_id.get()
        return True


class _RedactTokenFilter(logging.Filter):
    """uvicorn's access log prints the full URL; hide ?token=... so the secret never lands in logs."""

    _pat = re.compile(r"(token=)[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._pat.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        return True


log = logging.getLogger("pagespeed")
log.setLevel(LOG_LEVEL)
log.propagate = False
if not log.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(rid)s] %(message)s", "%H:%M:%S"))
    _handler.addFilter(_RequestIdFilter())
    log.addHandler(_handler)
logging.getLogger("uvicorn.access").addFilter(_RedactTokenFilter())

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

log.info(
    "Config: token_set=%s report_timeout=%ds settle=%dms pre_pdf_wait=%dms single_page_pdf=%s system_chrome=%s",
    bool(SECRET), REPORT_TIMEOUT_MS // 1000, SETTLE_MS, PRE_PDF_WAIT_MS, PDF_SINGLE_PAGE, USE_SYSTEM_CHROME,
)

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
        log.error("API_SECRET_TOKEN is not set on the server")
        raise HTTPException(500, "Server misconfigured: API_SECRET_TOKEN not set")
    if not token or not hmac.compare_digest(token.encode(), SECRET.encode()):
        log.warning("Rejected request: invalid or missing token")
        raise HTTPException(401, "Invalid or missing token")


def validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        log.warning("Rejected request: invalid url %r", url)
        raise HTTPException(400, "url must be a valid http(s) URL")
    return url


async def snap(page, folder: Path, name: str, saved: list, full_page: bool = False) -> None:
    path = folder / f"{name}.png"
    await page.screenshot(path=str(path), full_page=full_page)
    saved.append(path.name)
    log.info("Saved screenshot %s", path.name)


async def _heartbeat(message: str, interval: int = 15) -> None:
    """Log a progress line every `interval` seconds until cancelled."""
    start = time.monotonic()
    while True:
        await asyncio.sleep(interval)
        log.info("%s - still waiting (%ds elapsed)", message, int(time.monotonic() - start))


async def wait_for_results(page) -> bool:
    """
    Block until the Lighthouse LAB report is rendered (can take 2+ minutes).

    The field-data section and headings like "Diagnose performance issues" are on the page
    from the start, so they say nothing about the lab run. The lab report is the
    `.lh-scores-container` block (the row of score gauges), which only exists after the
    analysis finishes. We wait for it, then for the Performance score to hold a number.

    Returns False if it doesn't appear within REPORT_TIMEOUT_MS (default 5 minutes).
    """
    start = time.monotonic()
    log.info("Waiting for Lighthouse lab results (max %ds)", REPORT_TIMEOUT_MS // 1000)
    heartbeat = asyncio.create_task(_heartbeat("Lab results not ready yet"))
    try:
        # 1) the scores container exists and is visible
        await page.locator(".lh-scores-container").first.wait_for(state="visible", timeout=REPORT_TIMEOUT_MS)
        log.info(".lh-scores-container appeared after %ds", int(time.monotonic() - start))

        # 2) the Performance gauge contains an actual number
        await page.locator(
            ".lh-scores-container a.lh-gauge__wrapper[href=\"#performance\"] .lh-gauge__percentage",
            has_text=re.compile(r"\d"),
        ).first.wait_for(state="visible", timeout=60000)
        log.info("Performance score is present")
    except PWTimeout:
        log.error("Timed out waiting for results after %ds", int(time.monotonic() - start))
        return False
    finally:
        heartbeat.cancel()

    # Soft waits (best effort): the rest of the report rendered, network quiet
    try:
        await page.locator("span.lh-audit-group__title", has_text=re.compile(r"^Diagnostics$")).first.wait_for(
            state="attached", timeout=20000
        )
    except PWTimeout:
        log.warning("Diagnostics section not found within 20s (continuing)")
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        log.debug("Network not idle after 10s (continuing)")

    log.info("Settling for %dms (gauge animations)", SETTLE_MS)
    await page.wait_for_timeout(SETTLE_MS)
    log.info("Report ready (waited %ds in total)", int(time.monotonic() - start))
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
    log.info("Debug files saved in %s (timeout_%s.png / .html)", d, stamp)


# Text patterns run over the page's visible text (field data / Core Web Vitals section)
STAT_PATTERNS = {
    "CWV Status": r"Core Web Vitals Assessment:\s*([A-Za-z]+)",
    "LCP (Mobile)": r"Largest Contentful Paint[^\d]*([\d\.]+\s*[ms]+)",
    "INP": r"Interaction to Next Paint[^\d]*([\d]+\s*ms)",
    "CLS": r"Cumulative Layout Shift[^\d]*([\d\.]+)",
    "FCP": r"First Contentful Paint[^\d]*([\d\.]+\s*s)",
    "Total Blocking Time": r"Total Blocking Time[^\d]*([\d,]+\s*ms)",
    "TTFB": r"Time to First Byte[^\d]*([\d\.]+\s*m?s)",
}


async def extract_stats(page) -> dict:
    """Collect stats from the loaded report: regex stats, category scores, Lighthouse lab metrics."""
    log.info("Extracting stats from the report")
    # 1) regex over visible text
    text = await page.locator("body").inner_text()
    stats = {}
    for key, pattern in STAT_PATTERNS.items():
        m = re.search(pattern, text, re.IGNORECASE)
        stats[key] = m.group(1).strip() if m else None

    # 2) category scores from .lh-scores-container (Performance, Accessibility, ...)
    scores = {}
    items = page.locator(".lh-scores-container .lh-scores-header > a")
    for i in range(await items.count()):
        item = items.nth(i)
        try:
            label = await item.locator(".lh-gauge__label, .lh-fraction__label").first.inner_text(timeout=2000)
            value = await item.locator(".lh-gauge__percentage, .lh-fraction__content span").first.inner_text(timeout=2000)
            scores[label.strip()] = value.strip()
        except PWTimeout:
            continue

    # 3) Lighthouse lab metrics (FCP, LCP, TBT, CLS, Speed Index...) straight from the DOM
    lab_metrics = {}
    metrics = page.locator(".lh-metric")
    for i in range(await metrics.count()):
        m = metrics.nth(i)
        try:
            title = await m.locator(".lh-metric__title").first.inner_text(timeout=2000)
            value = await m.locator(".lh-metric__value").first.inner_text(timeout=2000)
            lab_metrics[title.strip()] = value.strip()
        except PWTimeout:
            continue

    log.info(
        "Extracted: %d/%d regex stats, %d scores, %d lab metrics",
        sum(v is not None for v in stats.values()), len(stats), len(scores), len(lab_metrics),
    )
    return {"stats": stats, "scores": scores, "lab_metrics": lab_metrics}


# ----------------------------------------------------------------------------
# Rating: Good / Needs improvement / Poor
#   name: (unit, good_max, poor_above)   -> good if v <= good_max, poor if v > poor_above
# ----------------------------------------------------------------------------
THRESHOLDS = {
    "LCP": ("s", 2.5, 4.0),
    "INP": ("ms", 200, 500),
    "CLS": ("", 0.1, 0.25),
    "FCP": ("s", 1.8, 3.0),
    "TTFB": ("s", 0.8, 1.8),
    "TBT": ("ms", 200, 600),
    "SI": ("s", 3.4, 5.8),
}
# where each metric is read from: field data = real users (regex stats), lab = Lighthouse run
FIELD_KEYS = {"LCP": "LCP (Mobile)", "INP": "INP", "CLS": "CLS", "FCP": "FCP", "TTFB": "TTFB"}
LAB_KEYS = {
    "LCP": "Largest Contentful Paint",
    "CLS": "Cumulative Layout Shift",
    "FCP": "First Contentful Paint",
    "TBT": "Total Blocking Time",
    "SI": "Speed Index",
}
RATING_ORDER = {"poor": 0, "needs_improvement": 1, "good": 2}


def parse_value(raw: Optional[str], unit: str) -> Optional[float]:
    """'2.2 s' / '484 ms' / '1,800 ms' / '0.01' -> number in the metric's unit (s, ms or unitless)."""
    if not raw:
        return None
    m = re.search(r"([\d,]*\.?\d+)\s*(ms|s)?\b", raw.strip(), re.IGNORECASE)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    found = (m.group(2) or "").lower()
    if unit == "":
        return num
    if not found:
        return None  # a time metric without a unit - don't guess
    if unit == "ms":
        return num * 1000 if found == "s" else num
    return num / 1000 if found == "ms" else num


def rate(value: float, good_max: float, poor_above: float) -> str:
    if value <= good_max:
        return "good"
    if value > poor_above:
        return "poor"
    return "needs_improvement"


def evaluate(data: dict) -> dict:
    """Rate every metric we could read. Returns {'metrics': [...], 'unavailable': [...]}."""
    metrics: list = []
    unavailable: list = []

    # Overall Lighthouse Performance score: 90-100 good, 50-89 needs improvement, 0-49 poor
    raw_score = data["scores"].get("Performance")
    if raw_score and raw_score.strip().isdigit():
        score = int(raw_score)
        rating = "good" if score >= 90 else "needs_improvement" if score >= 50 else "poor"
        metrics.append({
            "metric": "Performance score", "source": "lab", "value": raw_score.strip(),
            "rating": rating, "good": ">= 90", "poor": "< 50",
        })
    else:
        unavailable.append("Performance score")

    for name, (unit, good_max, poor_above) in THRESHOLDS.items():
        for source, mapping, bucket in (
            ("field", FIELD_KEYS, data["stats"]),
            ("lab", LAB_KEYS, data["lab_metrics"]),
        ):
            key = mapping.get(name)
            if not key:
                continue  # this metric isn't reported in that source
            raw = bucket.get(key)
            value = parse_value(raw, unit)
            if value is None:
                unavailable.append(f"{name} ({source})")
                continue
            suffix = f" {unit}" if unit else ""
            metrics.append({
                "metric": name, "source": source, "value": raw.strip(),
                "rating": rate(value, good_max, poor_above),
                "good": f"<= {good_max:g}{suffix}", "poor": f"> {poor_above:g}{suffix}",
            })

    return {"metrics": metrics, "unavailable": unavailable}


def build_json_response(url: str, data: dict, filter_mode: str) -> dict:
    result = evaluate(data)
    metrics = sorted(result["metrics"], key=lambda m: RATING_ORDER[m["rating"]])
    counts = {r: sum(m["rating"] == r for m in metrics) for r in RATING_ORDER}
    summary = {
        "cwv_assessment": data["stats"].get("CWV Status"),
        "checked": len(metrics),
        "poor": counts["poor"],
        "needs_improvement": counts["needs_improvement"],
        "good": counts["good"],
        "unavailable": result["unavailable"],
    }
    log.info(
        "Rated %d metrics: %d poor, %d need improvement, %d good (filter=%s)",
        len(metrics), counts["poor"], counts["needs_improvement"], counts["good"], filter_mode,
    )

    if filter_mode == "bad":
        return {
            "url": url, "filter": "bad", "summary": summary,
            "issues": [m for m in metrics if m["rating"] != "good"],
        }
    return {"url": url, "filter": "all", "summary": summary, "metrics": metrics, **data}


async def run_capture(target_url: str, output: str, folder: Optional[Path]) -> dict:
    """output: 'pdf' | 'json' | 'screenshots'. Returns {'screenshots': [...], 'pdf': bytes | None, 'data': dict | None}."""
    want_shots = output == "screenshots"
    want_pdf = output == "pdf"
    want_json = output == "json"
    saved: list = []
    pdf_bytes: Optional[bytes] = None
    data: Optional[dict] = None

    async with async_playwright() as pw:
        launch_kwargs = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if USE_SYSTEM_CHROME:
            launch_kwargs["channel"] = "chrome"
        log.info("Launching headless %s", "Chrome" if USE_SYSTEM_CHROME else "Chromium")
        browser = await pw.chromium.launch(**launch_kwargs)
        log.info("Browser launched")

        context = await browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            log.info("Opening https://pagespeed.web.dev/")
            await page.goto("https://pagespeed.web.dev/", wait_until="domcontentloaded", timeout=60000)
            log.info("PageSpeed page loaded")

            url_input = page.locator('input[name="url"]').first
            await url_input.wait_for(state="visible", timeout=30000)
            await url_input.fill(target_url)
            await url_input.press("Enter")
            log.info("Submitted %s - analysis started (waiting %dms before first check)", target_url, WAIT_MS)

            await page.wait_for_timeout(WAIT_MS)
            if want_shots:
                await snap(page, folder, "01_after_submit", saved)

            if not await wait_for_results(page):
                await save_debug(page)
                if want_shots:
                    await snap(page, folder, "02_results_timeout", saved)
                return {"screenshots": saved, "pdf": None, "data": None}

            # Scroll through the whole page once so lazy content is rendered
            log.info("Scrolling through the page to render lazy content")
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

            if want_json:
                await page.evaluate("window.scrollTo(0, 0)")
                await page.wait_for_timeout(PRE_PDF_WAIT_MS)
                data = await extract_stats(page)
                log.info("JSON data ready")

            if want_pdf:
                await page.evaluate("window.scrollTo(0, 0)")
                # final pause so the .lh-scores-container and rest of the report are fully painted
                await page.wait_for_timeout(PRE_PDF_WAIT_MS)
                await page.emulate_media(media="screen")
                height = await page.evaluate(
                    "Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)"
                )
                log.info(
                    "Generating PDF (page height %dpx, %s)", height,
                    "single page" if PDF_SINGLE_PAGE and height <= MAX_PDF_HEIGHT_PX else "paged A4",
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
                log.info("PDF generated: %d KB", len(pdf_bytes) // 1024)
        finally:
            await context.close()
            await browser.close()
            log.info("Browser closed")

    return {"screenshots": saved, "pdf": pdf_bytes, "data": data}


@app.get("/screenshots")
async def screenshots(
    url: str = Query(..., description="URL to analyse on PageSpeed Insights"),
    token: Optional[str] = Query(None, description="Secret token (or use X-API-Token header)"),
    output: str = Query("pdf", pattern="^(pdf|json|screenshots)$", description="pdf (default) | json | screenshots"),
    filter_mode: str = Query("bad", alias="filter", pattern="^(bad|all)$", description="json only: bad (default) | all"),
    x_api_token: Optional[str] = Header(None),
):
    request_id.set(uuid.uuid4().hex[:8])
    t0 = time.monotonic()
    log.info("Request received: url=%s output=%s%s", url, output, f" filter={filter_mode}" if output == "json" else "")
    check_token(token or x_api_token)
    validate_url(url)

    folder = None
    run_id = None
    if output == "screenshots":
        run_id = f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
        folder = SCREENSHOT_DIR / run_id
        folder.mkdir(parents=True, exist_ok=True)

    if _semaphore.locked():
        log.info("Another job is running - queued until it finishes")
    async with _semaphore:
        try:
            result = await run_capture(url, output, folder)
        except Exception as exc:  # noqa: BLE001
            log.exception("Capture failed")
            raise HTTPException(500, f"Capture failed: {exc}") from exc

    log.info("Finished in %ds", int(time.monotonic() - t0))

    if output == "pdf":
        if not result["pdf"]:
            raise HTTPException(504, "PageSpeed results did not finish loading in time")
        host = urlparse(url).netloc.replace(":", "_")
        return Response(
            content=result["pdf"],
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="pagespeed_{host}.pdf"'},
        )

    if output == "json":
        if not result["data"]:
            raise HTTPException(504, "PageSpeed results did not finish loading in time")
        return build_json_response(url, result["data"], filter_mode)

    return {
        "url": url,
        "folder": str(folder),
        "count": len(result["screenshots"]),
        "files": [f"/files/{run_id}/{n}" for n in result["screenshots"]],
    }


@app.get("/health")
async def health():
    return {"status": "ok"}