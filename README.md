# PageSpeed PDF Service

FastAPI + Playwright service that runs a URL through
[PageSpeed Insights](https://pagespeed.web.dev/) in headless Chromium and returns
the result as a **PDF** (default), a **JSON** list of problem metrics, or PNG screenshots.

## How it works

1. Caller sends a URL plus a secret token (constant-time compared with `API_SECRET_TOKEN`; mismatch gives `401`).
2. Headless browser opens pagespeed.web.dev, enters the URL and submits.
3. It waits until the **`.lh-scores-container`** (the row of score gauges) is visible and the Performance gauge
   (`a.lh-gauge__wrapper[href="#performance"] .lh-gauge__percentage`) contains a number. That only happens when the lab run finishes (often 2+ minutes). The field-data section
   and the "Diagnose performance issues" heading are on the page from the start, so they are not used as signals.
4. It then waits for the Diagnostics group and network idle (best effort), plus `SETTLE_MS` for animations.
5. It scrolls the page once to render lazy content.
6. Depending on `output`:
   - `pdf`: `page.pdf()` returns the PDF bytes straight in the HTTP response.
   - `json`: stats are read from the page, rated against the thresholds below and returned as JSON.
   - `screenshots`: PNGs of each section are saved in `screenshots/<run>/`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env      # then set API_SECRET_TOKEN
```

## Run

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Usage

```bash
# PDF (default) - returned directly
curl -o report.pdf "http://localhost:8000/screenshots?url=https://example.com&token=YOUR_SECRET"

# Token in a header instead
curl -H "X-API-Token: YOUR_SECRET" -o report.pdf \
     "http://localhost:8000/screenshots?url=https://example.com"

# Only the problem metrics as JSON (default filter=bad) - nothing saved to disk
curl "http://localhost:8000/screenshots?url=https://example.com&token=YOUR_SECRET&output=json"

# Every metric with its rating, plus the raw values
curl "http://localhost:8000/screenshots?url=https://example.com&token=YOUR_SECRET&output=json&filter=all"

# PNG screenshots (saved on disk, JSON response)
curl "http://localhost:8000/screenshots?url=https://example.com&token=YOUR_SECRET&output=screenshots"
```

Screenshot files are served at `http://localhost:8000/files/<run>/<image>.png`.

## Endpoints

| Method | Path           | Description                                              |
|--------|----------------|----------------------------------------------------------|
| GET    | `/screenshots` | Run a capture. Parameters are listed below               |
| GET    | `/files/...`   | Serve saved screenshots                                  |
| GET    | `/health`      | Health check, returns `{"status": "ok"}`                 |

### Parameters of `/screenshots`

| Parameter | Required | Values | Description |
|---|---|---|---|
| `url` | yes | http(s) URL | Page to analyse |
| `token` | yes* | string | Secret token. *Or send it in the `X-API-Token` header instead |
| `output` | no | `pdf` (default), `json`, `screenshots` | What to return |
| `filter` | no | `bad` (default), `all` | Only used with `output=json` |

## Responses

| `output` | Success response | Saved on disk? |
|---|---|---|
| `pdf` (default) | `200`, `application/pdf` file (the report as one tall page, or paged A4 if very long) | No |
| `json`, `filter=bad` (default) | `200`, JSON with `summary` and `issues` (only metrics that are not good) | No |
| `json`, `filter=all` | `200`, JSON with `summary`, `metrics` (all rated) and the raw `stats`, `scores`, `lab_metrics` | No |
| `screenshots` | `200`, JSON with the list of image paths | Yes, in `SCREENSHOT_DIR/<run>/` |

### `output=json` (filter=bad)

```json
{
  "url": "https://example.com",
  "filter": "bad",
  "summary": {
    "cwv_assessment": "Failed",
    "checked": 11,
    "poor": 5,
    "needs_improvement": 3,
    "good": 3,
    "unavailable": []
  },
  "issues": [
    {"metric": "Performance score", "source": "lab", "value": "26", "rating": "poor", "good": ">= 90", "poor": "< 50"},
    {"metric": "LCP", "source": "lab", "value": "18.3 s", "rating": "poor", "good": "<= 2.5 s", "poor": "> 4 s"},
    {"metric": "FCP", "source": "lab", "value": "6.8 s", "rating": "poor", "good": "<= 1.8 s", "poor": "> 3 s"},
    {"metric": "TBT", "source": "lab", "value": "3,350 ms", "rating": "poor", "good": "<= 200 ms", "poor": "> 600 ms"},
    {"metric": "SI", "source": "lab", "value": "11.3 s", "rating": "poor", "good": "<= 3.4 s", "poor": "> 5.8 s"},
    {"metric": "INP", "source": "field", "value": "480 ms", "rating": "needs_improvement", "good": "<= 200 ms", "poor": "> 500 ms"},
    {"metric": "FCP", "source": "field", "value": "2 s", "rating": "needs_improvement", "good": "<= 1.8 s", "poor": "> 3 s"},
    {"metric": "TTFB", "source": "field", "value": "0.9 s", "rating": "needs_improvement", "good": "<= 0.8 s", "poor": "> 1.8 s"}
  ]
}
```

- `source` is `field` (real users, Chrome UX Report, last 28 days) or `lab` (the Lighthouse run).
- `summary.poor` + `summary.needs_improvement` always equals the number of entries in `issues`.
- `rating` is `poor` or `needs_improvement` here (`good` also appears with `filter=all`). Issues are sorted worst first.
- `summary.unavailable` lists metrics that could not be read from the page.
- With `filter=all` the response also has `metrics` (every rated metric), `stats` (field-data values), `scores` (Performance, Accessibility, Best Practices, SEO, Agentic Browsing) and `lab_metrics` (FCP, LCP, TBT, CLS, Speed Index).

### `output=screenshots`

```json
{
  "url": "https://example.com",
  "folder": "/app/screenshots/20261012_120000_ab12cd",
  "count": 7,
  "files": ["/files/20261012_120000_ab12cd/01_after_submit.png", "..."]
}
```

### Errors

| Status | Meaning |
|---|---|
| `400` | `url` is not a valid http(s) URL |
| `401` | Token missing or wrong |
| `422` | Invalid `output` or `filter` value |
| `500` | Server error, e.g. `API_SECRET_TOKEN` not set, or the browser failed (details in the message and logs) |
| `504` | PageSpeed results did not finish loading within `REPORT_TIMEOUT_MS` |

## Configuration

| Variable            | Default         | Description                                          |
|---------------------|-----------------|------------------------------------------------------|
| `API_SECRET_TOKEN`  | (required)      | Shared secret                                        |
| `WAIT_MS`           | `5000`          | Delay before the first screenshot                    |
| `REPORT_TIMEOUT_MS` | `300000`        | Max wait for the lab results (5 min)                 |
| `SETTLE_MS`         | `3000`          | Extra wait after results appear                      |
| `PRE_PDF_WAIT_MS`   | `2000`          | Final wait right before printing the PDF             |
| `DEBUG_DIR`         | (unset)         | If set, saves screenshot + HTML when results time out|
| `LOG_LEVEL`         | `INFO`          | DEBUG, INFO, WARNING or ERROR                        |
| `SCREENSHOT_DIR`    | `./screenshots` | Folder for PNG screenshots                           |
| `USE_SYSTEM_CHROME` | `false`         | `true` uses installed Google Chrome                  |
| `MAX_CONCURRENT`    | `1`             | Simultaneous browser jobs                            |
| `PDF_SINGLE_PAGE`   | `true`          | One tall PDF page vs paged A4                        |

## Rating thresholds (JSON output)

| Metric | Good | Needs improvement | Poor |
|---|---|---|---|
| Performance score | 90-100 | 50-89 | 0-49 |
| LCP | <= 2.5 s | 2.5-4.0 s | > 4.0 s |
| INP | <= 200 ms | 200-500 ms | > 500 ms |
| CLS | <= 0.1 | 0.1-0.25 | > 0.25 |
| FCP | <= 1.8 s | 1.8-3.0 s | > 3.0 s |
| TTFB | <= 0.8 s | 0.8-1.8 s | > 1.8 s |
| TBT | <= 200 ms | 200-600 ms | > 600 ms |
| Speed Index | <= 3.4 s | 3.4-5.8 s | > 5.8 s |

`filter=bad` (default) returns only metrics rated needs-improvement or poor, worst first.
Metrics are rated from both sources where available: `field` (real users, Chrome UX Report) and `lab` (the Lighthouse run).
Anything that could not be read is listed under `summary.unavailable`.

## Logging

Each request gets a short id and logs its progress to stdout (visible in the terminal and in Render's Logs tab):

```
12:01:07 INFO    [a1b2c3d4] Request received: url=https://example.com output=json
12:01:08 INFO    [a1b2c3d4] Launching headless Chromium
12:01:12 INFO    [a1b2c3d4] Submitted https://example.com - analysis started
12:01:17 INFO    [a1b2c3d4] Waiting for Lighthouse lab results (max 300s)
12:01:32 INFO    [a1b2c3d4] Lab results not ready yet - still waiting (15s elapsed)
...
12:03:20 INFO    [a1b2c3d4] Report ready (waited 123s in total)
12:03:21 INFO    [a1b2c3d4] Finished in 134s
```

The `token=` value in uvicorn's access log is replaced with `***`.

## Notes

- A run usually takes 1-3 minutes (up to 5). Set a generous client timeout (browsers, proxies and nginx often cut off at 60s).
- If results don't appear within `REPORT_TIMEOUT_MS`, the PDF request returns `504`
  (instead of a PDF of a loading screen).
- Single-page PDFs are capped at about 18000 px (Chromium limit); taller reports fall back to paged A4.
- `stats` (field data) is read with text patterns and can come back `null` if PageSpeed changes its wording; `scores` and `lab_metrics` read page elements and are sturdier.
- If pagespeed.web.dev changes its markup, update the selectors in `wait_for_results` and `SECTIONS`.
- Keep `.env` out of version control and put HTTPS in front of the service if it is public.

## Deploying on Render

Chromium has to be installed at build time, together with its Linux libraries.
The simplest reliable way is Docker (a `Dockerfile` is included).

1. Push the repo (with `Dockerfile`, `app.py`, `requirements.txt`) to GitHub. Do **not** commit `.env`.
2. In Render, create a **Web Service** and choose **Docker** as the runtime (not "Python").
3. Under **Environment**, add `API_SECRET_TOKEN` (and any other variables from `.env.example`).
4. Deploy. Render sets `$PORT` automatically and the Dockerfile uses it.

Notes:
- Chromium needs memory. Render's smallest/free instances (512 MB) may crash during a long run; use a larger plan if you see out-of-memory restarts.
- A run can take up to 5 minutes. Check that your plan's and client's request timeouts allow that.
- Don't put the real token in shared URLs or screenshots; prefer the `X-API-Token` header.

### Without Docker (native Python runtime)

Set the build command to
`pip install -r requirements.txt && playwright install chromium`
and add the env var `PLAYWRIGHT_BROWSERS_PATH=/opt/render/project/.playwright`
so the browser is stored in the project folder and survives into runtime. This can still fail
on missing system libraries, which is why Docker is recommended.