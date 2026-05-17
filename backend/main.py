"""
FastAPI backend for the Asia Miles Flight Finder.

Endpoints:
  POST /api/search          – start a search job, returns {search_id, total_searches}
  GET  /api/results/{id}/stream – SSE stream of progress + results
  GET  /api/search-count    – estimate how many searches a request would trigger
  DELETE /api/search/{id}   – cancel a running search
"""

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from models import SearchRequest
from scraper import run_search_job, scrape_all_destinations

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Asia Miles Flight Finder")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store: search_id → asyncio.Queue
active_searches: Dict[str, asyncio.Queue] = {}
active_tasks: Dict[str, asyncio.Task] = {}
login_events: Dict[str, asyncio.Event] = {}
otp_events: Dict[str, asyncio.Event] = {}
otp_holders: Dict[str, list] = {}


@app.post("/api/search")
async def start_search(request: SearchRequest):
    combos = request.get_combinations()
    total = len(combos)

    if total == 0:
        raise HTTPException(400, "No search combinations generated — check your date ranges.")

    search_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    login_event = asyncio.Event()
    otp_event = asyncio.Event()
    otp_holder: list = []
    active_searches[search_id] = queue
    login_events[search_id] = login_event
    otp_events[search_id] = otp_event
    otp_holders[search_id] = otp_holder

    task = asyncio.create_task(run_search_job(request, queue, login_event, otp_event, otp_holder))
    active_tasks[search_id] = task

    return {"search_id": search_id, "total_searches": total}


@app.get("/api/results/{search_id}/stream")
async def stream_results(search_id: str, http_request: Request):
    queue = active_searches.get(search_id)
    if not queue:
        raise HTTPException(404, "Search not found or already completed.")

    async def event_generator():
        try:
            while True:
                # Stop streaming if the client disconnected
                if await http_request.is_disconnected():
                    task = active_tasks.pop(search_id, None)
                    if task:
                        task.cancel()
                    break

                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Send a heartbeat to keep the connection alive
                    yield f"event: heartbeat\ndata: {{}}\n\n"
                    continue

                event_type = item.get("type", "message")

                if event_type == "complete":
                    yield f"event: complete\ndata: {{}}\n\n"
                    active_searches.pop(search_id, None)
                    active_tasks.pop(search_id, None)
                    break
                elif event_type == "error":
                    yield f"event: error\ndata: {json.dumps({'message': item['message']})}\n\n"
                    active_searches.pop(search_id, None)
                    active_tasks.pop(search_id, None)
                    break
                else:
                    yield f"event: {event_type}\ndata: {json.dumps(item)}\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if deployed
        },
    )


@app.post("/api/search-count")
async def estimate_search_count(request: SearchRequest):
    """Returns how many searches this configuration would run."""
    combos = request.get_combinations()
    estimated_minutes = round(len(combos) * 15 / 60, 1)
    return {
        "total_searches": len(combos),
        "estimated_minutes": estimated_minutes,
        "warning": (
            f"This will take approximately {estimated_minutes} minutes."
            if estimated_minutes > 15 else None
        ),
    }


@app.post("/api/search/{search_id}/otp")
async def submit_otp(search_id: str, body: dict):
    """User submitted the 6-digit OTP — unblock the scraper."""
    code = str(body.get("code", "")).strip()
    holder = otp_holders.get(search_id)
    event = otp_events.pop(search_id, None)
    if holder is None or event is None:
        raise HTTPException(404, "OTP session not found.")
    holder.clear()
    holder.append(code)
    event.set()
    return {"status": "ok"}


@app.post("/api/search/{search_id}/continue")
async def continue_after_login(search_id: str):
    """User clicked 'Continue' after logging in — unblock the scraper."""
    event = login_events.pop(search_id, None)
    if event:
        event.set()
        return {"status": "ok"}
    raise HTTPException(404, "Search not found or already continued.")


@app.get("/api/destinations")
async def get_destinations():
    """Return all Cathay destination airports. Scrapes once and caches forever."""
    try:
        return await scrape_all_destinations()
    except Exception as e:
        raise HTTPException(500, f"Could not load destinations: {e}")


@app.delete("/api/search/{search_id}")
async def cancel_search(search_id: str):
    task = active_tasks.pop(search_id, None)
    active_searches.pop(search_id, None)
    if task:
        task.cancel()
        return {"status": "cancelled"}
    raise HTTPException(404, "Search not found.")


# Serve frontend — mount last so API routes take priority
frontend_path = Path(__file__).parent.parent / "frontend"
if frontend_path.exists():
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="frontend")
