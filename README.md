# PageSpeed PDF Service

FastAPI + Playwright service that runs a URL through
[PageSpeed Insights](https://pagespeed.web.dev/) in headless Chromium and returns
the finished report as a **PDF** (returned directly, nothing saved to disk) or as PNG screenshots.

## How it works

1. Caller sends a URL plus a secret token (constant-time compared with `API_SECRET_TOKEN`; mismatch gives `401`).
2. Headless browser opens pagespeed.web.dev, enters the URL and submits.
3. It waits until the **`.lh-scores-container`** (the row of score gauges) is visible and the Performance gauge
   (`a.lh-gauge__wrapper[href="#performance"] .lh-gauge__percentage`) contains a number. That only happens when the lab run finishes (often 2+ minutes). The field-data section
   and the "Diagnose performance issues" heading are on the page from the start, so they are not used as signals.
4. It then waits for the Diagnostics group and network idle (best effort), plus `SETTLE_MS` for animations.
5. It scrolls the page once to render lazy content.
6. `output=pdf`: `page.pdf()` returns the PDF bytes straight in the HTTP response.
   `output=screenshots`: PNGs of each section are saved in `screenshots/<run>/`.

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

# PNG screenshots (saved on disk, JSON response)
curl "http://localhost:8000/screenshots?url=https://example.com&token=YOUR_SECRET&output=screenshots"
```

Screenshot files are served at `http://localhost:8000/files/<run>/<image>.png`.

## Endpoints

| Method | Path           | Description                                   |
|--------|----------------|-----------------------------------------------|
| GET    | `/screenshots` | `url`, `token`, `output=pdf|screenshots`      |
| GET    | `/files/...`   | Serve saved screenshots                       |
| GET    | `/health`      | Health check                                  |

## Configuration

| Variable            | Default         | Description                                          |
|---------------------|-----------------|------------------------------------------------------|
| `API_SECRET_TOKEN`  | (required)      | Shared secret                                        |
| `WAIT_MS`           | `5000`          | Delay before the first screenshot                    |
| `REPORT_TIMEOUT_MS` | `300000`        | Max wait for the lab results (5 min)                 |
| `SETTLE_MS`         | `3000`          | Extra wait after results appear                      |
| `PRE_PDF_WAIT_MS`   | `2000`          | Final wait right before printing the PDF             |
| `DEBUG_DIR`         | (unset)         | If set, saves screenshot + HTML when results time out|
| `SCREENSHOT_DIR`    | `./screenshots` | Folder for PNG screenshots                           |
| `USE_SYSTEM_CHROME` | `false`         | `true` uses installed Google Chrome                  |
| `MAX_CONCURRENT`    | `1`             | Simultaneous browser jobs                            |
| `PDF_SINGLE_PAGE`   | `true`          | One tall PDF page vs paged A4                        |

## Notes

- A run usually takes 1-3 minutes (up to 5). Set a generous client timeout (browsers, proxies and nginx often cut off at 60s).
- If results don't appear within `REPORT_TIMEOUT_MS`, the PDF request returns `504`
  (instead of a PDF of a loading screen).
- Single-page PDFs are capped at about 18000 px (Chromium limit); taller reports fall back to paged A4.
- If pagespeed.web.dev changes its markup, update the selectors in `wait_for_results` and `SECTIONS`.
- Keep `.env` out of version control and put HTTPS in front of the service if it is public.