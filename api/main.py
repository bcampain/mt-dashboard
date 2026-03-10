"""
mt-dashboard API
================
FastAPI backend that serves the mt-dashboard SPA and exposes a thin JSON API
over the artifacts produced by the media-tools .NET pipeline:

  /logs/runs/*.json   — PipelineRunManifest files written by ManifestWriter
  /logs/*.log         — Per-run and per-day log files
  /incoming, /staging, /library — Media folder trees (read-only stats)

All mounts are read-only from the dashboard's perspective; the .NET app owns
every file here.  The dashboard container adds :ro to its volume mounts.

Endpoints
---------
GET  /api/health                    — Liveness probe
GET  /api/stats                     — Item counts per media folder
GET  /api/runs                      — All run manifests, newest first
GET  /api/runs/{run_id}             — Single run manifest
GET  /api/runs/{run_id}/log         — Log file tail (query: lines=N)
GET  /api/runs/{run_id}/log/stream  — SSE stream of live log lines
GET  /api/events                    — SSE broadcast when any manifest changes
GET  /                              — SPA (index.html)
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# ── Config ────────────────────────────────────────────────────────────────────
LOGS_DIR     = Path(os.getenv("LOGS_DIR",     "/logs"))
RUNS_DIR     = LOGS_DIR / "runs"
INCOMING_DIR = Path(os.getenv("INCOMING_DIR", "/incoming"))
STAGING_DIR  = Path(os.getenv("STAGING_DIR",  "/staging"))
LIBRARY_DIR  = Path(os.getenv("LIBRARY_DIR",  "/library"))
WEB_DIR      = Path(os.getenv("WEB_DIR",      "/app/web"))

# How often (seconds) the SSE manifest-change broadcaster polls for new/updated
# files.  Low enough to feel live; high enough not to thrash the disk.
MANIFEST_POLL_INTERVAL = 2.0
LOG_POLL_INTERVAL      = 1.0

app = FastAPI(title="mt-dashboard", docs_url=None, redoc_url=None)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_manifest(path: Path) -> dict | None:
    """Parse a manifest JSON file, returning None on any error."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _all_manifests() -> list[dict]:
    """Return all manifests sorted newest-first by started_at."""
    if not RUNS_DIR.exists():
        return []
    manifests = []
    for f in RUNS_DIR.glob("*.json"):
        m = _load_manifest(f)
        if m:
            manifests.append(m)
    manifests.sort(key=lambda m: m.get("started_at", ""), reverse=True)
    return manifests


def _count_kind_dir(root: Path) -> dict:
    """
    Count items inside /root/tv, /root/movies, /root/clips.
    For tv: count subdirectory show entries.
    For movies/clips: count files recursively (video extensions only).
    """
    VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov"}

    def count_videos(p: Path) -> int:
        if not p.exists():
            return 0
        return sum(1 for f in p.rglob("*") if f.is_file() and f.suffix.lower() in VIDEO_EXTS)

    def count_shows(p: Path) -> int:
        if not p.exists():
            return 0
        return sum(1 for d in p.iterdir() if d.is_dir())

    if not root.exists():
        return {"exists": False, "tv": 0, "movies": 0, "clips": 0, "total": 0}

    tv     = count_shows(root / "tv")
    movies = count_videos(root / "movies")
    clips  = count_videos(root / "clips")
    return {"exists": True, "tv": tv, "movies": movies, "clips": clips, "total": tv + movies + clips}


def _sse_event(data: object, event: str | None = None) -> str:
    """Format a Server-Sent Event string."""
    lines = []
    if event:
        lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data)}")
    lines.append("")
    lines.append("")
    return "\n".join(lines)


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "runs_dir": str(RUNS_DIR), "runs_dir_exists": RUNS_DIR.exists()}


@app.get("/api/stats")
def stats() -> dict:
    return {
        "incoming": _count_kind_dir(INCOMING_DIR),
        "staging":  _count_kind_dir(STAGING_DIR),
        "library":  _count_kind_dir(LIBRARY_DIR),
    }


@app.get("/api/runs")
def list_runs() -> list:
    return _all_manifests()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    path = RUNS_DIR / f"{run_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    m = _load_manifest(path)
    if m is None:
        raise HTTPException(status_code=500, detail="Could not parse manifest")
    return m


@app.get("/api/runs/{run_id}/log")
def get_log(run_id: str, lines: int = 500) -> dict:
    """Return the last `lines` lines of a run's log file."""
    path = RUNS_DIR / f"{run_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    manifest = _load_manifest(path)
    if not manifest:
        return {"lines": [], "total_lines": 0, "log_file": None}

    log_file = manifest.get("log_file")
    if not log_file or not Path(log_file).exists():
        return {"lines": [], "total_lines": 0, "log_file": log_file}

    content = Path(log_file).read_text(errors="replace")
    all_lines = content.splitlines()
    tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
    return {"lines": tail, "total_lines": len(all_lines), "log_file": log_file}


@app.get("/api/runs/{run_id}/log/stream")
async def stream_log(run_id: str) -> StreamingResponse:
    """
    SSE endpoint: streams new log lines for a running pipeline.
    Sends all existing lines first, then polls for appended content
    until the run reaches a terminal status (complete / failed / cancelled)
    or the client disconnects.
    """

    async def generate() -> AsyncGenerator[str, None]:
        path = RUNS_DIR / f"{run_id}.json"
        if not path.exists():
            yield _sse_event({"error": f"Run {run_id} not found"}, event="error")
            return

        manifest = _load_manifest(path)
        if not manifest:
            yield _sse_event({"error": "Cannot parse manifest"}, event="error")
            return

        log_file = manifest.get("log_file")
        if not log_file:
            yield _sse_event({"error": "No log file in manifest"}, event="error")
            return

        log_path = Path(log_file)
        sent_bytes = 0

        # Send a heartbeat comment so the browser doesn't time out
        yield ": heartbeat\n\n"

        while True:
            # Re-read manifest to check if run is still active
            manifest = _load_manifest(path) or manifest
            status = manifest.get("status", "running")

            if log_path.exists():
                current_size = log_path.stat().st_size
                if current_size > sent_bytes:
                    with log_path.open("rb") as f:
                        f.seek(sent_bytes)
                        new_content = f.read(current_size - sent_bytes).decode("utf-8", errors="replace")
                    for line in new_content.splitlines():
                        yield _sse_event({"line": line}, event="log_line")
                    sent_bytes = current_size

            if status in ("complete", "failed", "cancelled"):
                yield _sse_event({"status": status}, event="run_done")
                break

            await asyncio.sleep(LOG_POLL_INTERVAL)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/events")
async def manifest_events() -> StreamingResponse:
    """
    SSE endpoint: fires a 'runs_updated' event whenever any manifest file
    is created or modified.  The frontend uses this to refresh the run list
    without constant polling.
    """

    async def generate() -> AsyncGenerator[str, None]:
        # snapshot: {filename: mtime}
        def snapshot() -> dict[str, float]:
            if not RUNS_DIR.exists():
                return {}
            return {f.name: f.stat().st_mtime for f in RUNS_DIR.glob("*.json")}

        last = snapshot()
        yield ": heartbeat\n\n"

        while True:
            await asyncio.sleep(MANIFEST_POLL_INTERVAL)
            current = snapshot()
            if current != last:
                last = current
                yield _sse_event({"ts": time.time()}, event="runs_updated")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Static files (SPA) ────────────────────────────────────────────────────────
# The SPA lives in /app/web/ inside the container.
# Any path that doesn't match /api/* falls through to index.html.

if WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIR / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        return FileResponse(str(WEB_DIR / "index.html"))
