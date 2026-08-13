const state = { sources: [], channels: [], lineup: [], visibleIds: [], dragId: null };
const $ = (id) => document.getElementById(id);

function esc(v) {
  return String(v ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
}
function fmtDate(value) {
  if (!value) return '—';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}
function toast(message) {
  const el = $('toast'); el.textContent = message; el.classList.remove('hidden');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.add('hidden'), 3200);
}
async function api(url, options={}) {
  const res = await fetch(url, options);
  let body = null;
  const type = res.headers.get('content-type') || '';
  if (type.includes('application/json')) body = await res.json();
  else body = await res.text();
  if (!res.ok) throw new Error(body?.detail || body?.error || body || `HTTP ${res.status}`);
  return body;
}

async function loadAll() {
  await Promise.all([loadStatus(), loadSources(), loadGroups()]);
  await loadChannels();
  await loadLineup();
}
async function loadStatus() {
  const s = await api('/api/status');
  $('statSources').textContent = s.source_count;
  $('statActive').textContent = s.active_channels;
  $('statSelected').textContent = s.selected_channels;
  $('refreshState').textContent = `Refresh cycle: every ${s.refresh_hours} hours · ${s.timezone}`;
  $('logsBody').innerHTML = s.logs.length ? s.logs.map(l => `
    <tr><td>${esc(l.source_name || '—')}</td><td><span class="badge ${l.status==='ok'?'ok':l.status==='error'?'bad':'warn'}">${esc(l.status)}</span></td><td>${esc(fmtDate(l.started_at))}</td><td>${esc(l.message || '')}</td></tr>`).join('') : '<tr><td colspan="4" class="empty">No refreshes yet.</td></tr>';
}
async function loadSources() {
  state.sources = await api('/api/sources');
  $('sourcesBody').innerHTML = state.sources.length ? state.sources.map(s => `
    <tr>
      <td><div class="channel-name">${esc(s.name)}</div><div class="subtle">${esc(s.m3u_kind.toUpperCase())}</div></td>
      <td>${s.last_status==='OK'?'<span class="badge ok">OK</span>':s.last_status==='ERROR'?'<span class="badge bad">ERROR</span>':'<span class="badge warn">Never</span>'}${s.last_error?`<div class="subtle status-error">${esc(s.last_error)}</div>`:''}</td>
      <td>${esc(s.channel_count)}</td><td>${esc(fmtDate(s.last_refresh))}</td>
      <td>${s.xml_value ? '<span class="badge ok">XMLTV</span>' : '<span class="badge">None</span>'}</td>
      <td><button onclick="refreshSource(${s.id})">Refresh</button> <button class="danger" onclick="deleteSource(${s.id}, '${esc(s.name).replace(/'/g, "\\'")}')">Delete</button></td>
    </tr>`).join('') : '<tr><td colspan="6" class="empty">No sources yet. Add your first M3U above.</td></tr>';
  const f = $('sourceFilter'); const old = f.value;
  f.innerHTML = '<option value="">All sources</option>' + state.sources.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join('');
  f.value = old;
}
async function loadGroups() {
  const groups = await api('/api/groups'); const f = $('groupFilter'); const old = f.value;
  f.innerHTML = '<option value="">All groups</option>' + groups.map(g => `<option value="${esc(g)}">${esc(g)}</option>`).join('');
  f.value = old;
}
async function loadChannels() {
  const p = new URLSearchParams();
  if ($('sourceFilter').value) p.set('source_id', $('sourceFilter').value);
  if ($('groupFilter').value) p.set('group', $('groupFilter').value);
  if ($('searchInput').value.trim()) p.set('q', $('searchInput').value.trim());
  state.channels = await api('/api/channels?' + p.toString());
  state.visibleIds = state.channels.map(c => c.id);
  $('channelsBody').innerHTML = state.channels.length ? state.channels.map(c => `
    <tr>
      <td class="checkcol"><input type="checkbox" ${c.selected?'checked':''} onchange="setSelected(${c.id}, this.checked)"></td>
      <td><div class="channel-name">${esc(c.name)}</div><div class="subtle">${esc(c.stream_url)}</div></td>
      <td>${esc(c.source_name)}</td><td>${esc(c.group_title || '—')}</td><td>${esc(c.tvg_id || '—')}</td>
      <td>${c.tvg_id ? '<span class="badge ok">ID</span>' : '<span class="badge">No ID</span>'}</td>
    </tr>`).join('') : '<tr><td colspan="6" class="empty">No matching channels.</td></tr>';
}
async function loadLineup() {
  state.lineup = await api('/api/channels?selected=true');
  const body = $('lineupBody');
  body.innerHTML = state.lineup.length ? state.lineup.map(c => `
    <tr draggable="true" data-id="${c.id}">
      <td><span class="drag-handle" title="Drag to reorder">☰</span></td>
      <td><input class="number-input" type="number" min="0" value="${c.channel_number ?? ''}" placeholder="—" onchange="setNumber(${c.id}, this.value)"></td>
      <td><div class="channel-name">${esc(c.name)}</div><div class="subtle">${esc(c.tvg_id || 'No TVG-ID')}</div></td>
      <td>${esc(c.source_name)}</td><td>${esc(c.group_title || '—')}</td>
      <td><button onclick="setSelected(${c.id}, false)">Remove</button></td>
    </tr>`).join('') : '<tr><td colspan="6" class="empty">Select channels above to build your master lineup.</td></tr>';
  bindDrag();
  showDuplicateNumbers();
}
function bindDrag() {
  document.querySelectorAll('#lineupBody tr[draggable=true]').forEach(row => {
    row.addEventListener('dragstart', () => { state.dragId = Number(row.dataset.id); row.classList.add('dragging'); });
    row.addEventListener('dragend', () => { row.classList.remove('dragging'); document.querySelectorAll('.drag-over').forEach(x=>x.classList.remove('drag-over')); });
    row.addEventListener('dragover', e => { e.preventDefault(); row.classList.add('drag-over'); });
    row.addEventListener('dragleave', () => row.classList.remove('drag-over'));
    row.addEventListener('drop', async e => {
      e.preventDefault(); row.classList.remove('drag-over');
      const targetId = Number(row.dataset.id); if (!state.dragId || state.dragId === targetId) return;
      const ids = [...document.querySelectorAll('#lineupBody tr[draggable=true]')].map(r => Number(r.dataset.id));
      const from = ids.indexOf(state.dragId), to = ids.indexOf(targetId); ids.splice(to, 0, ids.splice(from,1)[0]);
      await api('/api/lineup/reorder', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids})});
      await loadLineup(); toast('Lineup order saved');
    });
  });
}
function showDuplicateNumbers() {
  const nums = new Map();
  for (const c of state.lineup) if (c.channel_number !== null) nums.set(c.channel_number, (nums.get(c.channel_number)||0)+1);
  const dupes = [...nums.entries()].filter(([,n])=>n>1).map(([n])=>n);
  const el = $('duplicateWarning');
  if (dupes.length) { el.textContent = `Duplicate channel numbers detected: ${dupes.join(', ')}`; el.classList.remove('hidden'); }
  else el.classList.add('hidden');
}

window.setSelected = async (id, selected) => {
  await api(`/api/channels/${id}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({selected})});
  await Promise.all([loadChannels(), loadLineup(), loadStatus()]);
};
window.setNumber = async (id, value) => {
  const channel_number = value === '' ? null : Number(value);
  await api(`/api/channels/${id}`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({channel_number})});
  await loadLineup();
};
window.refreshSource = async (id) => {
  try { toast('Refreshing source…'); await api(`/api/sources/${id}/refresh`, {method:'POST'}); await loadAll(); toast('Source refreshed'); }
  catch(e) { toast(`Refresh failed: ${e.message}`); await loadAll(); }
};
window.deleteSource = async (id, name) => {
  if (!confirm(`Delete source “${name}” and all of its saved channels?`)) return;
  await api(`/api/sources/${id}`, {method:'DELETE'}); await loadAll(); toast('Source deleted');
};

$('sourceForm').addEventListener('submit', async e => {
  e.preventDefault(); const btn = e.submitter; btn.disabled = true; btn.textContent = 'Adding…';
  const msg = $('sourceMessage'); msg.classList.add('hidden');
  try {
    const body = new FormData(e.currentTarget); const result = await api('/api/sources', {method:'POST', body});
    if (result.refresh?.status === 'error') { msg.textContent = `Source saved, but initial refresh failed: ${result.refresh.error}`; msg.classList.remove('hidden'); }
    else { e.currentTarget.reset(); toast('Source added and refreshed'); }
    await loadAll();
  } catch(err) { msg.textContent = err.message; msg.classList.remove('hidden'); }
  finally { btn.disabled = false; btn.textContent = 'Add Source'; }
});
$('refreshAllBtn').addEventListener('click', async e => {
  e.currentTarget.disabled = true; e.currentTarget.textContent = 'Refreshing…';
  try { const r = await api('/api/refresh', {method:'POST'}); await loadAll(); const failed = r.sources.filter(x=>x.status==='error').length; toast(failed ? `Refresh complete with ${failed} failure(s)` : 'All sources refreshed'); }
  catch(err) { toast(err.message); }
  finally { e.currentTarget.disabled = false; e.currentTarget.textContent = 'Refresh All'; }
});
let searchTimer;
$('searchInput').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer=setTimeout(loadChannels,250); });
$('sourceFilter').addEventListener('change', loadChannels);
$('groupFilter').addEventListener('change', loadChannels);
$('selectVisibleBtn').addEventListener('click', async () => {
  await api('/api/channels/bulk-selection', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:state.visibleIds,selected:true})}); await Promise.all([loadChannels(),loadLineup(),loadStatus()]);
});
$('deselectVisibleBtn').addEventListener('click', async () => {
  await api('/api/channels/bulk-selection', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:state.visibleIds,selected:false})}); await Promise.all([loadChannels(),loadLineup(),loadStatus()]);
});
$('autoNumberBtn').addEventListener('click', async () => {
  const start=Number($('numberStart').value), increment=Number($('numberIncrement').value), mode=$('numberMode').value;
  await api('/api/lineup/autonumber',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({start,increment,mode})}); await loadLineup(); toast('Channel numbers updated');
});

loadAll().catch(e => toast(e.message));
