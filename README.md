# Asia Miles Flight Finder

Search award flight availability on Cathay Pacific across multiple destinations and date ranges at once.

## Quick start

```bash
cd asia-miles-search
chmod +x start.sh
./start.sh
```

Then open **http://localhost:8000** in your browser.

On the first search, a real Chromium browser window will open. Sign in to your Cathay / Asia Miles account there — the search will start automatically once login is detected. Your session is saved so you only need to log in once.

---

## How searches work

- Each (destination × departure date × cabin class) — plus nights range for return trips — is one Cathay search
- Searches run sequentially with a short delay to avoid bot detection
- Results stream into the table as they complete
- The live estimate in the form shows how many searches and roughly how long they'll take

### Caps to keep in mind

| Scenario | Searches |
|---|---|
| 3 destinations, 7 depart days, 1 cabin, one-way | 21 |
| 3 destinations, 7 depart days, 1 cabin, return 5–9 nights | 105 |
| 5 destinations, 14 depart days, 2 cabins, return 5–9 nights | 700 |

Keep total searches under ~30 for a 10-minute run. Use the live estimate before hitting Search.

---

## Calibrating the scraper

The first time you run a search, open the terminal and watch the logs. If the results extraction isn't finding miles/taxes (you'll see a `"selector calibration needed"` note in the results table), do this:

1. Find the URL logged in the terminal (the results page)
2. Open that URL in your browser
3. Right-click a flight result → Inspect
4. Find the class names for miles, taxes, flight numbers, times
5. Update `backend/scraper.py` → `extract_results()` with the real selectors

---

## Project structure

```
asia-miles-search/
├── backend/
│   ├── main.py      # FastAPI server + SSE streaming
│   ├── scraper.py   # Playwright automation against Cathay's site
│   ├── models.py    # Data models (search request + flight result)
│   └── requirements.txt
├── frontend/
│   ├── index.html   # Search form + results table
│   ├── style.css
│   └── app.js
└── start.sh
```

## Scaling to a real app

- **Database**: swap the in-memory `allResults` list for Postgres/SQLite to persist results across sessions
- **Background workers**: replace the in-process asyncio task with Celery + Redis for multi-user support
- **Deployment**: the FastAPI backend can be containerised with Docker; the frontend is plain HTML/JS and can be hosted on any CDN
- **More airlines**: the scraper layer (`scraper.py`) is isolated from the API — add a new file per airline and expose it via the same SSE endpoint
