from __future__ import annotations

import gzip
import hashlib
import html
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx
from lxml import etree

from .db import connect

DATA_DIR = Path("/app/data")
CACHE_DIR = DATA_DIR / "cache"
OUTPUT_DIR = Path("/app/output")

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower()).strip()


def stable_key(tvg_id: str | None, name: str, group_title: str | None, stream_url: str) -> str:
    if tvg_id and tvg_id.strip():
        return "tvg:" + tvg_id.strip().lower()
    base = f"{normalize_name(name)}|{normalize_name(group_title or '')}"
    if normalize_name(name):
        return "name:" + hashlib.sha1(base.encode("utf-8")).hexdigest()
    return "url:" + hashlib.sha1(stream_url.encode("utf-8")).hexdigest()


def parse_m3u(text: str) -> list[dict]:
    lines = [line.strip() for line in text.replace("\r", "").split("\n")]
    channels: list[dict] = []
    used_keys: set[str] = set()
    pending: dict | None = None
    pending_group: str | None = None

    for line in lines:
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs = {k.lower(): v for k, v in ATTR_RE.findall(line)}
            name = line.split(",", 1)[1].strip() if "," in line else attrs.get("tvg-name", "Unnamed Channel")
            pending = {
                "tvg_id": attrs.get("tvg-id") or None,
                "name": attrs.get("tvg-name") or name or "Unnamed Channel",
                "group_title": attrs.get("group-title") or None,
                "logo": attrs.get("tvg-logo") or None,
            }
            pending_group = None
        elif line.startswith("#EXTGRP:") and pending is not None:
            pending_group = line.split(":", 1)[1].strip() or None
        elif line.startswith("#"):
            continue
        elif pending is not None:
            pending["stream_url"] = line
            if not pending.get("group_title") and pending_group:
                pending["group_title"] = pending_group
            key = stable_key(
                pending.get("tvg_id"),
                pending["name"],
                pending.get("group_title"),
                line,
            )
            if key in used_keys:
                key = key + "|dup:" + hashlib.sha1(line.encode("utf-8")).hexdigest()[:12]
            used_keys.add(key)
            pending["stable_key"] = key
            channels.append(pending)
            pending = None
            pending_group = None

    return channels


async def fetch_bytes(url: str) -> bytes:
    headers = {"User-Agent": "IPTV-Merge-Manager/0.1.1"}
    timeout = httpx.Timeout(60.0, connect=20.0)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


def maybe_decompress_xml(data: bytes) -> bytes:
    if data.startswith(b"\x1f\x8b"):
        return gzip.decompress(data)
    return data


async def materialize_source(kind: str, value: str, destination: Path, is_xml: bool = False) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if kind == "url":
        data = await fetch_bytes(value)
    else:
        src = Path(value)
        if not src.exists():
            raise FileNotFoundError(f"Uploaded source file no longer exists: {src}")
        data = src.read_bytes()
    if is_xml:
        data = maybe_decompress_xml(data)
    destination.write_bytes(data)


async def refresh_source(source_id: int) -> dict:
    with connect() as conn:
        source = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not source:
            raise ValueError("Source not found")
        source = dict(source)
        log_id = conn.execute(
            "INSERT INTO refresh_log(source_id, started_at, status, message) VALUES(?,?,?,?)",
            (source_id, utc_now(), "running", "Refresh started"),
        ).lastrowid

    if not source["enabled"]:
        return {"source_id": source_id, "status": "disabled"}

    m3u_cache = CACHE_DIR / f"source_{source_id}.m3u"
    xml_cache = CACHE_DIR / f"source_{source_id}.xml"

    try:
        await materialize_source(source["m3u_kind"], source["m3u_value"], m3u_cache)
        text = m3u_cache.read_text(encoding="utf-8", errors="replace")
        parsed = parse_m3u(text)
        if not parsed:
            raise ValueError("No IPTV channels with #EXTINF entries were found in the M3U")

        if source.get("xml_kind") and source.get("xml_value"):
            await materialize_source(source["xml_kind"], source["xml_value"], xml_cache, is_xml=True)

        now = utc_now()
        with connect() as conn:
            max_order = conn.execute("SELECT COALESCE(MAX(sort_order),0) FROM channels").fetchone()[0]
            existing = {
                row["stable_key"]: dict(row)
                for row in conn.execute("SELECT * FROM channels WHERE source_id = ?", (source_id,)).fetchall()
            }
            seen: set[str] = set()
            new_count = 0
            changed_count = 0

            for item in parsed:
                key = item["stable_key"]
                seen.add(key)
                old = existing.get(key)
                if old:
                    changed = any(
                        (old.get(field) or "") != (item.get(field) or "")
                        for field in ("tvg_id", "name", "group_title", "logo", "stream_url")
                    )
                    if changed:
                        changed_count += 1
                    conn.execute(
                        """
                        UPDATE channels
                           SET tvg_id=?, name=?, group_title=?, logo=?, stream_url=?, is_active=1, last_seen=?
                         WHERE id=?
                        """,
                        (
                            item.get("tvg_id"), item["name"], item.get("group_title"),
                            item.get("logo"), item["stream_url"], now, old["id"],
                        ),
                    )
                else:
                    max_order += 10
                    new_count += 1
                    conn.execute(
                        """
                        INSERT INTO channels(
                            source_id, stable_key, tvg_id, name, group_title, logo, stream_url,
                            selected, sort_order, channel_number, is_active, last_seen
                        ) VALUES(?,?,?,?,?,?,?,0,?,NULL,1,?)
                        """,
                        (
                            source_id, key, item.get("tvg_id"), item["name"], item.get("group_title"),
                            item.get("logo"), item["stream_url"], max_order, now,
                        ),
                    )

            removed_count = 0
            for key, old in existing.items():
                if key not in seen and old["is_active"]:
                    removed_count += 1
                    conn.execute("UPDATE channels SET is_active=0 WHERE id=?", (old["id"],))

            msg = f"{len(parsed)} channels; {new_count} new; {removed_count} removed; {changed_count} changed"
            conn.execute(
                "UPDATE sources SET last_status='OK', last_refresh=?, last_error=NULL, channel_count=? WHERE id=?",
                (now, len(parsed), source_id),
            )
            conn.execute(
                "UPDATE refresh_log SET finished_at=?, status='ok', message=? WHERE id=?",
                (now, msg, log_id),
            )

        return {
            "source_id": source_id,
            "status": "ok",
            "channel_count": len(parsed),
            "new": new_count,
            "removed": removed_count,
            "changed": changed_count,
        }
    except Exception as exc:
        now = utc_now()
        with connect() as conn:
            conn.execute(
                "UPDATE sources SET last_status='ERROR', last_refresh=?, last_error=? WHERE id=?",
                (now, str(exc), source_id),
            )
            conn.execute(
                "UPDATE refresh_log SET finished_at=?, status='error', message=? WHERE id=?",
                (now, str(exc), log_id),
            )
        raise


def m3u_escape(value: str | None) -> str:
    return (value or "").replace('"', "'")


def generate_master_m3u() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c.*, s.name AS source_name
              FROM channels c
              JOIN sources s ON s.id=c.source_id
             WHERE c.selected=1 AND c.is_active=1 AND s.enabled=1
             ORDER BY c.sort_order ASC, c.id ASC
            """
        ).fetchall()

    lines = ["#EXTM3U"]
    for row in rows:
        d = dict(row)
        attrs = []
        if d.get("tvg_id"):
            attrs.append(f'tvg-id="{m3u_escape(d["tvg_id"])}"')
        attrs.append(f'tvg-name="{m3u_escape(d["name"])}"')
        if d.get("logo"):
            attrs.append(f'tvg-logo="{m3u_escape(d["logo"])}"')
        if d.get("group_title"):
            attrs.append(f'group-title="{m3u_escape(d["group_title"])}"')
        if d.get("channel_number") is not None:
            attrs.append(f'tvg-chno="{d["channel_number"]}"')
        lines.append(f"#EXTINF:-1 {' '.join(attrs)},{d['name']}")
        lines.append(d["stream_url"])

    (OUTPUT_DIR / "master.m3u").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)


def _iter_xml_matches(xml_path: Path, selected_ids: set[str], tag: str) -> Iterable[etree._Element]:
    if not xml_path.exists() or not selected_ids:
        return
    context = etree.iterparse(str(xml_path), events=("end",), tag=tag, recover=True, huge_tree=True)
    for _, elem in context:
        key = elem.get("id") if tag == "channel" else elem.get("channel")
        if key in selected_ids:
            yield elem
        else:
            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]


def generate_master_xml() -> tuple[int, int]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c.source_id, c.tvg_id
              FROM channels c
              JOIN sources s ON s.id=c.source_id
             WHERE c.selected=1 AND c.is_active=1 AND s.enabled=1
               AND c.tvg_id IS NOT NULL AND TRIM(c.tvg_id) <> ''
            """
        ).fetchall()

    by_source: dict[int, set[str]] = {}
    for row in rows:
        by_source.setdefault(row["source_id"], set()).add(row["tvg_id"])

    out = OUTPUT_DIR / "master.xml"
    channel_written: set[str] = set()
    channel_count = 0
    programme_count = 0

    with etree.xmlfile(str(out), encoding="utf-8") as xf:
        xf.write_declaration()
        with xf.element("tv", {"generator-info-name": "IPTV Merge Manager v0.1.1"}):
            for source_id, ids in by_source.items():
                xml_path = CACHE_DIR / f"source_{source_id}.xml"
                for elem in _iter_xml_matches(xml_path, ids, "channel") or []:
                    cid = elem.get("id")
                    if cid and cid not in channel_written:
                        xf.write(elem)
                        channel_written.add(cid)
                        channel_count += 1
                    elem.clear()

            for source_id, ids in by_source.items():
                xml_path = CACHE_DIR / f"source_{source_id}.xml"
                for elem in _iter_xml_matches(xml_path, ids, "programme") or []:
                    xf.write(elem)
                    programme_count += 1
                    elem.clear()

    return channel_count, programme_count


def generate_outputs() -> dict:
    m3u_channels = generate_master_m3u()
    xml_channels, programmes = generate_master_xml()
    return {
        "m3u_channels": m3u_channels,
        "xml_channels": xml_channels,
        "programmes": programmes,
    }
