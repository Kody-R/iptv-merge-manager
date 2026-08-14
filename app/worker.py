from __future__ import annotations

import asyncio
import heapq
import json
import resource
import sys
from difflib import SequenceMatcher

from .db import connect, init_db
from .iptv import CACHE_DIR, generate_outputs, normalize_name, rebuild_epg_index, refresh_source


def peak_rss_kb() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def epg_suggestions(channel_id: int) -> list[dict]:
    with connect() as conn:
        row = conn.execute('SELECT custom_name,name FROM channels WHERE id=?', (channel_id,)).fetchone()
        if not row:
            raise ValueError('Channel not found')
        target = normalize_name(row['custom_name'] or row['name'])
        top: list[tuple[float, int, dict]] = []
        seq = 0
        for e in conn.execute(
            '''SELECT e.tvg_id,e.display_name,s.name source_name
               FROM epg_channels e JOIN sources s ON s.id=e.source_id'''
        ):
            candidate = normalize_name(e['display_name'] or e['tvg_id'])
            score = SequenceMatcher(None, target, candidate).ratio()
            if score < .45:
                continue
            seq += 1
            item = {'tvg_id': e['tvg_id'], 'name': e['display_name'] or e['tvg_id'], 'source_name': e['source_name'], 'score': round(score*100)}
            if len(top) < 10:
                heapq.heappush(top, (score, seq, item))
            elif score > top[0][0]:
                heapq.heapreplace(top, (score, seq, item))
    return [x[2] for x in sorted(top, key=lambda x: x[0], reverse=True)]



async def main() -> int:
    init_db()
    action = sys.argv[1] if len(sys.argv) > 1 else ''
    if action == 'generate':
        result = generate_outputs()
    elif action == 'reindex':
        result = {}
        with connect() as conn:
            source_ids = [r['id'] for r in conn.execute('SELECT id FROM sources WHERE xml_value IS NOT NULL')]
        for sid in source_ids:
            path = CACHE_DIR / f'source_{sid}.xml'
            if path.exists():
                result[str(sid)] = rebuild_epg_index(sid, path)
    elif action == 'refresh':
        sid = int(sys.argv[2])
        result = await refresh_source(sid)
        with connect() as conn:
            log = conn.execute('SELECT id FROM refresh_log WHERE source_id=? ORDER BY id DESC LIMIT 1', (sid,)).fetchone()
            if log:
                conn.execute('UPDATE refresh_log SET peak_rss_kb=? WHERE id=?', (peak_rss_kb(), log['id']))
    elif action == 'epg-suggest':
        result = epg_suggestions(int(sys.argv[2]))
    else:
        raise ValueError(f'Unknown worker action: {action}')
    print(json.dumps({'result': result, 'peak_rss_kb': peak_rss_kb()}))
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
