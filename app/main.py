from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .db import connect, init_db, rows_to_dicts
from .iptv import generate_outputs, refresh_source

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path("/app/data")
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = Path("/app/output")
TZ_NAME = os.getenv("TZ", "America/New_York")
REFRESH_HOURS = max(1, int(os.getenv("REFRESH_HOURS", "4")))

refresh_lock = asyncio.Lock()
scheduler = AsyncIOScheduler(timezone=ZoneInfo(TZ_NAME))


async def refresh_all() -> dict:
    async with refresh_lock:
        with connect() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM sources WHERE enabled=1 ORDER BY id").fetchall()]
        results = []
        for source_id in ids:
            try:
                results.append(await refresh_source(source_id))
            except Exception as exc:
                results.append({"source_id": source_id, "status": "error", "error": str(exc)})
        output = generate_outputs()
        return {"sources": results, "output": output}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    generate_outputs()
    scheduler.add_job(
        refresh_all,
        CronTrigger(hour=f"*/{REFRESH_HOURS}", minute=0, timezone=ZoneInfo(TZ_NAME)),
        id="refresh-all",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="IPTV Merge Manager", version="0.1.1", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")


class ChannelPatch(BaseModel):
    selected: bool | None = None
    channel_number: int | None = None


class BulkSelection(BaseModel):
    ids: list[int]
    selected: bool


class ReorderRequest(BaseModel):
    ids: list[int]


class AutoNumberRequest(BaseModel):
    start: int = 1
    increment: int = 1
    mode: str = "sequential"


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "refresh_hours": REFRESH_HOURS})


@app.get("/api/status")
def api_status():
    with connect() as conn:
        source_count = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        active_channels = conn.execute("SELECT COUNT(*) FROM channels WHERE is_active=1").fetchone()[0]
        selected_channels = conn.execute("SELECT COUNT(*) FROM channels WHERE selected=1 AND is_active=1").fetchone()[0]
        last_logs = rows_to_dicts(conn.execute(
            """
            SELECT l.*, s.name AS source_name
              FROM refresh_log l LEFT JOIN sources s ON s.id=l.source_id
             ORDER BY l.id DESC LIMIT 20
            """
        ).fetchall())
    return {
        "version": "0.1.1",
        "timezone": TZ_NAME,
        "refresh_hours": REFRESH_HOURS,
        "source_count": source_count,
        "active_channels": active_channels,
        "selected_channels": selected_channels,
        "logs": last_logs,
    }


@app.get("/api/sources")
def list_sources():
    with connect() as conn:
        return rows_to_dicts(conn.execute("SELECT * FROM sources ORDER BY id").fetchall())


@app.post("/api/sources")
async def add_source(
    name: str = Form(...),
    m3u_url: str | None = Form(None),
    xml_url: str | None = Form(None),
    m3u_file: UploadFile | None = File(None),
    xml_file: UploadFile | None = File(None),
):
    name = name.strip()
    m3u_url = (m3u_url or "").strip() or None
    xml_url = (xml_url or "").strip() or None
    if m3u_file is not None and not m3u_file.filename:
        m3u_file = None
    if xml_file is not None and not xml_file.filename:
        xml_file = None
    if not name:
        raise HTTPException(400, "Source name is required")
    if not m3u_url and not m3u_file:
        raise HTTPException(400, "Provide an M3U URL or upload an M3U file")
    if m3u_url and m3u_file:
        raise HTTPException(400, "Use either M3U URL or M3U upload, not both")
    if xml_url and xml_file:
        raise HTTPException(400, "Use either XMLTV URL or XMLTV upload, not both")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    if m3u_file:
        suffix = Path(m3u_file.filename or "source.m3u").suffix or ".m3u"
        path = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
        with path.open("wb") as f:
            shutil.copyfileobj(m3u_file.file, f)
        m3u_kind, m3u_value = "file", str(path)
    else:
        m3u_kind, m3u_value = "url", m3u_url

    xml_kind = None
    xml_value = None
    if xml_file:
        suffixes = "".join(Path(xml_file.filename or "guide.xml").suffixes) or ".xml"
        path = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffixes}"
        with path.open("wb") as f:
            shutil.copyfileobj(xml_file.file, f)
        xml_kind, xml_value = "file", str(path)
    elif xml_url:
        xml_kind, xml_value = "url", xml_url

    with connect() as conn:
        source_id = conn.execute(
            "INSERT INTO sources(name,m3u_kind,m3u_value,xml_kind,xml_value) VALUES(?,?,?,?,?)",
            (name, m3u_kind, m3u_value, xml_kind, xml_value),
        ).lastrowid

    try:
        async with refresh_lock:
            result = await refresh_source(source_id)
            generate_outputs()
        return {"id": source_id, "refresh": result}
    except Exception as exc:
        return {"id": source_id, "refresh": {"status": "error", "error": str(exc)}}


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int):
    with connect() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Source not found")
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
    for suffix in ("m3u", "xml"):
        p = DATA_DIR / "cache" / f"source_{source_id}.{suffix}"
        if p.exists():
            p.unlink()
    generate_outputs()
    return {"ok": True}


@app.post("/api/sources/{source_id}/refresh")
async def refresh_one(source_id: int):
    async with refresh_lock:
        try:
            result = await refresh_source(source_id)
            output = generate_outputs()
            return {"refresh": result, "output": output}
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise HTTPException(502, str(exc))


@app.post("/api/refresh")
async def refresh_everything():
    return await refresh_all()


@app.get("/api/channels")
def list_channels(
    source_id: int | None = None,
    group: str | None = None,
    q: str | None = None,
    selected: bool | None = None,
    active: bool = True,
):
    sql = """
        SELECT c.*, s.name AS source_name
          FROM channels c JOIN sources s ON s.id=c.source_id
         WHERE 1=1
    """
    params: list = []
    if active:
        sql += " AND c.is_active=1"
    if source_id is not None:
        sql += " AND c.source_id=?"
        params.append(source_id)
    if group:
        sql += " AND COALESCE(c.group_title,'')=?"
        params.append(group)
    if q:
        sql += " AND (c.name LIKE ? OR COALESCE(c.tvg_id,'') LIKE ? OR s.name LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like])
    if selected is not None:
        sql += " AND c.selected=?"
        params.append(1 if selected else 0)
    sql += " ORDER BY CASE WHEN c.selected=1 THEN 0 ELSE 1 END, c.sort_order, c.name COLLATE NOCASE"
    with connect() as conn:
        return rows_to_dicts(conn.execute(sql, params).fetchall())


@app.get("/api/groups")
def list_groups():
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT group_title FROM channels WHERE is_active=1 AND group_title IS NOT NULL AND TRIM(group_title)<>'' ORDER BY group_title COLLATE NOCASE"
        ).fetchall()
    return [r[0] for r in rows]


@app.patch("/api/channels/{channel_id}")
def patch_channel(channel_id: int, patch: ChannelPatch):
    fields = []
    params = []
    if patch.selected is not None:
        fields.append("selected=?")
        params.append(1 if patch.selected else 0)
    if "channel_number" in patch.model_fields_set:
        if patch.channel_number is not None and patch.channel_number < 0:
            raise HTTPException(400, "Channel number cannot be negative")
        fields.append("channel_number=?")
        params.append(patch.channel_number)
    if not fields:
        return {"ok": True}
    params.append(channel_id)
    with connect() as conn:
        cur = conn.execute(f"UPDATE channels SET {', '.join(fields)} WHERE id=?", params)
        if cur.rowcount == 0:
            raise HTTPException(404, "Channel not found")
    generate_outputs()
    return {"ok": True}


@app.post("/api/channels/bulk-selection")
def bulk_selection(req: BulkSelection):
    if not req.ids:
        return {"updated": 0}
    placeholders = ",".join("?" for _ in req.ids)
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE channels SET selected=? WHERE id IN ({placeholders})",
            [1 if req.selected else 0, *req.ids],
        )
    generate_outputs()
    return {"updated": cur.rowcount}


@app.post("/api/lineup/reorder")
def reorder_lineup(req: ReorderRequest):
    with connect() as conn:
        for index, channel_id in enumerate(req.ids, start=1):
            conn.execute(
                "UPDATE channels SET sort_order=? WHERE id=? AND selected=1",
                (index * 10, channel_id),
            )
    generate_outputs()
    return {"ok": True}


@app.post("/api/lineup/autonumber")
def auto_number(req: AutoNumberRequest):
    if req.start < 0 or req.increment < 1:
        raise HTTPException(400, "Start must be >= 0 and increment must be >= 1")
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, channel_number FROM channels WHERE selected=1 AND is_active=1 ORDER BY sort_order,id"
        ).fetchall()
        if req.mode == "fill-gaps":
            used = {r["channel_number"] for r in rows if r["channel_number"] is not None}
            n = req.start
            for row in rows:
                if row["channel_number"] is not None:
                    continue
                while n in used:
                    n += req.increment
                conn.execute("UPDATE channels SET channel_number=? WHERE id=?", (n, row["id"]))
                used.add(n)
                n += req.increment
        else:
            n = req.start
            for row in rows:
                conn.execute("UPDATE channels SET channel_number=? WHERE id=?", (n, row["id"]))
                n += req.increment
    generate_outputs()
    return {"ok": True}


@app.get("/output/master.m3u")
def output_m3u():
    path = OUTPUT_DIR / "master.m3u"
    if not path.exists():
        generate_outputs()
    return FileResponse(path, media_type="audio/x-mpegurl", filename="master.m3u")


@app.get("/output/master.xml")
def output_xml():
    path = OUTPUT_DIR / "master.xml"
    if not path.exists():
        generate_outputs()
    return FileResponse(path, media_type="application/xml", filename="master.xml")
