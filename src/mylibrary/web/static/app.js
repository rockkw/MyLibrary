// MyLibrary UI. Every action is either a DB decision (approve/skip/edit) or a
// whitelisted `mylib` CLI job. The browser never renames anything itself.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const base = p => String(p || '').split('/').pop();
const api = async (url, body) => {
  const r = await fetch(url, body ? {method: 'POST', headers: {'Content-Type': 'application/json'},
                                     body: JSON.stringify(body)} : {});
  if (!r.ok) throw new Error((await r.text()).replace(/<[^>]+>/g, ' ').trim().slice(0, 300));
  return r.json();
};

const STATUSES = [
  ['review', 'Needs review'], ['conflict', 'Conflicts'], ['auto', 'Auto-approved'],
  ['approved', 'Approved'], ['skipped', 'Skipped'], ['applied', 'Applied'],
  ['failed', 'Failed'], ['noop', 'Already in place'], ['ignored', 'Ignored'], ['', 'All'],
];
const state = { status: 'review', root: '', q: '', items: [], cur: -1, checked: new Set(),
                planId: null, job: null };

// ── summary + sidebar ──
async function loadSummary() {
  const s = await api('/api/summary');
  state.planId = s.plan_id;
  const c = s.counts || {};
  const total = Object.values(c).reduce((a, b) => a + b, 0);
  $('#status-list').innerHTML = STATUSES.map(([k, label]) =>
    `<div class="status-item ${state.status === k ? 'active' : ''}" data-s="${k}">
       <span>${label}</span><span class="n">${k ? (c[k] || 0) : total}</span></div>`).join('');
  $('#root-list').innerHTML = [`<div class="root-item ${!state.root ? 'active' : ''}" data-r="">
       <span>All drives</span><span class="n">${s.files}</span></div>`].concat(
    s.roots.map(r => `<div class="root-item ${state.root === r.root ? 'active' : ''}"
       data-r="${esc(r.root)}"><span>${esc(r.root)}</span><span class="n">${r.n}</span></div>`)).join('');
  $('#stats-bar').innerHTML =
    `<span>plan <b>${s.plan_id ? '#' + s.plan_id : '—'}</b></span>
     <span>files <b>${s.files}</b></span>
     <span>to apply <b>${(c.auto || 0) + (c.approved || 0)}</b></span>
     <span>LLM <b>${esc(s.ollama_model || 'off')}</b></span>`;
}

// ── list ──
const confClass = v => v >= 0.9 ? 'hi' : v >= 0.6 ? 'mid' : 'lo';
async function loadList(keep) {
  const qs = new URLSearchParams({status: state.status, root: state.root, q: state.q});
  const d = await api('/api/proposals?' + qs);
  state.items = d.items;
  if (!keep) { state.cur = state.items.length ? 0 : -1; state.checked.clear(); }
  else state.cur = Math.min(state.cur, state.items.length - 1);
  renderList();
  if (state.cur >= 0) loadDetail(); else $('#detail-pane').innerHTML =
    `<p class="empty">${d.plan_id ? 'Nothing here.' : 'No plan yet: run the pipeline.'}</p>`;
}
function episodeCode(r) {
  if (r.kind !== 'episode' || r.season == null || r.episode == null) return '';
  const pad = n => String(n).padStart(2, '0');
  const last = r.episode_last && r.episode_last !== r.episode ? `-E${pad(r.episode_last)}` : '';
  return `S${pad(r.season)}E${pad(r.episode)}${last}`;
}
const sceneName = r => [r.site, r.performers, r.title].filter(Boolean).join(' - ') || 'unidentified scene';
function renderList() {
  $('#list').innerHTML = state.items.map((r, i) => {
    const res = r.height ? `${r.height}p` : '';
    const to = r.kind === 'scene' ? esc(sceneName(r)) : r.title ? `${r.artist ? `${esc(r.artist)} - ` : ''}${esc(r.title)}${r.year ? ` (${r.year})` : ''}` : '<i>unidentified</i>';
    return `<div class="row ${i === state.cur ? 'sel' : ''}" data-i="${i}">
      <input type="checkbox" data-id="${r.id}" ${state.checked.has(r.id) ? 'checked' : ''}>
      <span class="conf ${confClass(r.confidence)}">${(r.confidence ?? 0).toFixed(2)}</span>
      <span class="pill ${r.status}">${r.status}</span>
      <div class="names"><div class="to">${to}${episodeCode(r) ? `<span class="chip">${episodeCode(r)}</span>` : ''}${['music', 'software', 'scene'].includes(r.kind) ? `<span class="chip">${r.kind}</span>` : ''}${r.scene_date ? `<span class="chip">${esc(r.scene_date)}</span>` : ''}${r.edition ? `<span class="chip">${esc(r.edition)}</span>` : ''}${res ? `<span class="chip">${res}</span>` : ''}
        ${r.vcodec ? `<span class="chip">${esc(r.vcodec)}</span>` : ''}
        ${r.source ? `<span class="chip">${esc(r.source)}</span>` : ''}</div>
        <div class="from">${esc(base(r.src))}</div></div></div>`;
  }).join('') || '<p class="empty">Nothing here.</p>';
}

// ── detail ──
const fmtSize = b => b > 1e9 ? (b / 1e9).toFixed(2) + ' GB' : (b / 1e6).toFixed(0) + ' MB';
async function loadDetail() {
  const item = state.items[state.cur];
  if (!item) return;
  const d = await api('/api/proposal/' + item.id);
  const decidable = !['applied', 'noop', 'ignored'].includes(d.status);
  $('#detail-pane').innerHTML = `
    <h2>${d.kind === 'scene' ? esc(sceneName(d)) : d.title ? (d.artist ? esc(d.artist) + ' - ' : '') + esc(d.title) + (d.year ? ` (${d.year})` : '') + (d.edition ? ` - ${esc(d.edition)}` : '') + (episodeCode(d) ? ` - ${episodeCode(d)}` : '') : 'Unidentified file'}</h2>
    <span class="pill ${d.status}">${d.status}</span>
    <span class="conf ${confClass(d.confidence)}" style="padding:0 .4rem;margin-left:.3rem">
      ${(d.confidence ?? 0).toFixed(2)}</span>
    <span class="chip">${esc(d.source || '')}</span>
    ${d.imdb_id ? `<span class="chip">${esc(d.imdb_id)}</span>` : ''}
    <div class="detail-label">Current</div><div class="path old">${esc(d.src)}</div>
    <div class="arrow">▼ rename (same drive, no overwrite)</div>
    <div class="path new">${d.dst ? esc(d.dst) : `— ${{episode: 'needs series, season and episode', music: 'needs artist, album and title', scene: 'needs a site and a performer or title'}[d.kind] || 'needs a title'} —`}</div>
    ${d.reason ? `<div class="reason">${esc(d.reason)}</div>` : ''}
    ${decidable ? `
    <div class="detail-label">Identify</div>
    <div class="edit"><select id="ed-kind">
        <option value="movie">Movie</option>
        <option value="episode" ${d.kind === 'episode' ? 'selected' : ''}>TV episode</option>
        <option value="music" ${d.kind === 'music' ? 'selected' : ''}>Music</option>
        <option value="software" ${d.kind === 'software' ? 'selected' : ''}>Software</option>
        <option value="scene" ${d.kind === 'scene' ? 'selected' : ''}>Adult scene</option></select>
      <input id="ed-title" value="${esc(d.title || '')}" placeholder="Title">
      <input id="ed-year" value="${d.year || ''}" placeholder="Year">
      <input id="ed-edition" value="${esc(d.edition || '')}" placeholder="Edition (optional)">
      <div id="ed-scene">
        <input id="ed-site" value="${esc(d.site || '')}" placeholder="Site">
        <input id="ed-date" value="${esc(d.scene_date || '')}" placeholder="YYYY-MM-DD">
        <input id="ed-performers" value="${esc(d.performers || '')}" placeholder="Performers, comma separated">
      </div>
      <div id="ed-tv">
        <input id="ed-season" value="${d.season ?? ''}" placeholder="Season" inputmode="numeric">
        <input id="ed-episode" value="${d.episode ?? ''}" placeholder="Episode" inputmode="numeric">
        <input id="ed-episode-last" value="${d.episode_last ?? ''}" placeholder="Last ep" inputmode="numeric">
        <input id="ed-episode-title" value="${esc(d.episode_title || '')}" placeholder="Episode title (optional)">
      </div>
      <div id="ed-music">
        <input id="ed-artist" value="${esc(d.artist || '')}" placeholder="Artist">
        <input id="ed-album" value="${esc(d.album || '')}" placeholder="Album">
        <input id="ed-track" value="${d.track ?? ''}" placeholder="Track" inputmode="numeric">
        <input id="ed-disc" value="${d.disc ?? ''}" placeholder="Disc" inputmode="numeric">
      </div></div>
    <div class="btnrow">
      <button class="btn go" id="ed-save">Save & approve (e)</button>
      <button class="btn ok" data-dec="approve">Approve (a)</button>
      <button class="btn no" data-dec="skip">Skip (s)</button>
      ${['approved', 'skipped'].includes(d.status) ? '<button class="btn" data-dec="reset">Reset</button>' : ''}
    </div>
    <div class="cli">mylib approve ${d.proposal_id} · mylib edit ${d.proposal_id} --title "…" --year … --edition "…" · mylib edit ${d.proposal_id} --title "…" --season … --episode …</div>` : ''}
    <div class="detail-label">Media</div>
    <div class="info">
      <span>Size</span><span>${fmtSize(d.size || 0)}</span>
      <span>Duration</span><span>${d.duration ? Math.round(d.duration / 60) + ' min' : '?'}</span>
      <span>Video</span><span>${d.width ? `${d.width}×${d.height}` : '?'} ${esc(d.vcodec || '')}</span>
      <span>Audio</span><span>${esc(d.acodec || '?')} ${esc(d.audio_langs || '')}</span>
      <span>Subtitles</span><span>${esc(d.sub_langs || 'none embedded')}</span>
      ${d.probe_error ? `<span>Probe</span><span style="color:#c44">${esc(d.probe_error)}</span>` : ''}
    </div>
    ${d.llm_json ? `<details open><summary>LLM answer</summary><pre>${esc(JSON.stringify(d.llm_json, null, 2))}</pre></details>` : ''}
    <details><summary>guessit parse</summary><pre>${esc(JSON.stringify(d.guessit_json, null, 2))}</pre></details>`;
  $$('[data-dec]').forEach(b => b.onclick = () => decide([d.proposal_id], b.dataset.dec));
  const save = $('#ed-save');
  if (save) save.onclick = saveEdit;
  $$('#ed-title, #ed-year, #ed-edition, #ed-tv input, #ed-music input, #ed-scene input').forEach(el => el.onkeydown = e => {
    if (e.key === 'Enter') saveEdit(); e.stopPropagation(); });
  const kind = $('#ed-kind');
  if (kind) {
    const show = () => {
      $('#ed-tv').hidden = kind.value !== 'episode';
      $('#ed-music').hidden = kind.value !== 'music';
      $('#ed-edition').hidden = kind.value !== 'movie';
      $('#ed-scene').hidden = kind.value !== 'scene';
      $('#ed-year').hidden = ['software', 'scene'].includes(kind.value);
      $('#ed-title').placeholder = {episode: 'Series', music: 'Track title',
                                    software: 'Product', scene: 'Scene title (optional)'}[kind.value] || 'Title';
    };
    kind.onchange = show;
    kind.onkeydown = e => e.stopPropagation();
    show();
  }
}
async function saveEdit() {
  const item = state.items[state.cur];
  try {
    const r = await api('/api/edit', {id: item.id, title: $('#ed-title').value,
                                      year: $('#ed-year').value,
                                      edition: $('#ed-edition').value,
                                      kind: $('#ed-kind').value,
                                      season: $('#ed-season').value,
                                      episode: $('#ed-episode').value,
                                      episode_last: $('#ed-episode-last').value,
                                      episode_title: $('#ed-episode-title').value,
                                      artist: $('#ed-artist').value,
                                      album: $('#ed-album').value,
                                      track: $('#ed-track').value,
                                      disc: $('#ed-disc').value,
                                      site: $('#ed-site').value,
                                      scene_date: $('#ed-date').value,
                                      performers: $('#ed-performers').value});
    note(`${r.cli} → ${r.status}`);
    await refresh(true);
  } catch (e) { note(e.message, true); }
}
async function decide(ids, action) {
  if (!ids.length) return;
  const r = await api('/api/decision', {ids, action});
  note(`${r.cli} → ${r.updated} updated`);
  state.checked.clear();
  const newStatus = {approve: 'approved', skip: 'skipped', reset: 'review'}[action];
  const staysInView = !state.status || state.status.split(',').includes(newStatus);
  if (ids.length === 1 && staysInView && state.cur < state.items.length - 1) state.cur++;
  await refresh(true);
}
async function refresh(keep) { await loadSummary(); await loadList(keep); }
const note = (msg, bad) => { $('#job-last').textContent = msg;
  $('#job-last').style.color = bad ? '#c44' : '#aaa'; };

// ── jobs ──
async function startJob(name, params = {}, logEl = null) {
  try {
    const j = await api('/api/jobs', {name, params});
    state.job = j.id;
    $('#job-cli').textContent = j.cli;
    $('#job-log').textContent = `$ ${j.cli}\n`;
    if (logEl) logEl.textContent += (logEl.textContent ? '\n' : '') + `$ ${j.cli}\n`;
    $$('.job-btn').forEach(b => b.disabled = true);
    return await pollJob(j.id, logEl);
  } catch (e) { note(e.message, true); return null; }
}
async function pollJob(id, logEl) {
  let since = 0;
  for (;;) {
    const j = await api(`/api/jobs/${id}?since=${since}`);
    since = j.log_len;
    if (j.log.length) {
      const text = j.log.join('\n') + '\n';
      for (const el of [$('#job-log'), logEl].filter(Boolean)) {
        el.textContent += text; el.scrollTop = el.scrollHeight; }
      note(j.log[j.log.length - 1]);
    }
    $('#job-state').className = 'pill ' + j.state;
    $('#job-state').textContent = j.state;
    if (j.state !== 'running') {
      $$('.job-btn').forEach(b => b.disabled = false);
      await refresh(true);
      return j;
    }
    await new Promise(r => setTimeout(r, 700));
  }
}

// ── apply dialog ──
async function openApply() {
  if (!state.planId) return note('No plan yet', true);
  $('#apply-plan').textContent = '#' + state.planId;
  $('#apply-overlay').classList.add('open');
  $('#apply-exec').disabled = true;
  $('#apply-log').textContent = '';
  const j = await startJob('apply_dry', {plan: state.planId}, $('#apply-log'));
  const ready = j && j.state === 'done' && /\b([1-9]\d*) files/.test($('#apply-log').textContent);
  $('#apply-exec').disabled = !ready;
}
$('#apply-btn').onclick = openApply;
$('#apply-cancel').onclick = () => $('#apply-overlay').classList.remove('open');
$('#apply-exec').onclick = async () => {
  $('#apply-exec').disabled = true;
  await startJob('apply', {plan: state.planId}, $('#apply-log'));
};

// ── runs + dupes pages ──
async function loadRuns() {
  const runs = await api('/api/runs');
  $('#runs').innerHTML = runs.length ? `<table><tr><th>Run</th><th>Kind</th><th>Plan</th>
    <th>Started</th><th>Moved</th><th>Status</th><th>Verify</th><th></th></tr>` + runs.map(r => `
    <tr><td>#${r.id}</td><td>${r.kind}${r.undo_of ? ` of #${r.undo_of}` : ''}</td><td>${r.plan_id ? `#${r.plan_id}` : '—'}</td>
    <td>${esc(r.started_at)}</td><td>${r.moved}</td><td><span class="pill ${r.status === 'ok' ? 'done' :
    r.status === 'running' ? 'running' : 'error'}">${r.status}</span></td>
    <td>${r.verify_ok ? '✓' : (r.verify_report && r.verify_report !== '[]' ? esc(r.verify_report) : '')}</td>
    <td><button class="act" data-run="${r.id}" data-j="verify">Verify</button>
    ${['apply', 'cleanup'].includes(r.kind) ? `<button class="act" data-run="${r.id}" data-j="undo_dry">Undo (dry)</button>
      <button class="act skip" data-run="${r.id}" data-j="undo">Undo</button>` : ''}</td></tr>`).join('') +
    '</table>' : '<p class="empty">No runs yet.</p>';
  $$('#runs [data-j]').forEach(b => b.onclick = async () => {
    if (b.dataset.j === 'undo' && !confirmUndo(b.dataset.run)) return;
    $('#jobbar').classList.add('open');
    await startJob(b.dataset.j, {run: b.dataset.run});
    loadRuns();
  });
}
// Inline confirm (no browser dialogs): second click within 4s confirms.
const pending = {};
function confirmUndo(id) {
  if (pending[id] && Date.now() - pending[id] < 4000) { delete pending[id]; return true; }
  pending[id] = Date.now(); note(`Click Undo again to reverse run #${id}`); return false;
}
function leftoverCard(i, label) {
  const box = i.torrent ? '' : `<input type="checkbox" class="leftover" data-path="${esc(i.path)}"> `;
  return `<div class="card"><h3><label>${box}${esc(i.path)}</label></h3><span class="chip">${label}</span>
    <span class="chip">${fmtSize(i.size)}</span>
    ${i.torrent ? `<div class="reason">Remove from Transmission first: ${esc(i.torrent)}</div>` : ''}
    ${i.files.map(f => `<div class="from">${esc(f.name)} · ${fmtSize(f.size)}</div>`).join('')}</div>`;
}
async function loadLeftovers() {
  const d = await api('/api/leftovers');
  if (d.error) {
    $('#leftovers').innerHTML = `<p class="empty">${esc(d.error)}: not suggesting anything.</p>`;
    return;
  }
  const cards = d.empty.map(i => leftoverCard(i, 'empty')).join('') +
    d.leftovers.map(i => leftoverCard(i, 'leftovers only')).join('') +
    d.held.map(i => leftoverCard(i, 'in torrent')).join('');
  const ready = d.empty.length + d.leftovers.length;
  const bar = ready ? `<div class="btnrow"><button class="btn" id="left-all">Select all</button>
    <button class="btn go" id="left-go" disabled>Approve selected</button></div>
    <pre id="left-log" class="cli"></pre>` : '';
  $('#leftovers').innerHTML = cards ? bar + cards : '<p class="empty">No leftover folders.</p>';
  if (!ready) return;
  const boxes = $$('.leftover');
  const sync = () => { $('#left-go').disabled = !boxes.some(b => b.checked); };
  boxes.forEach(b => b.onchange = sync);
  $('#left-all').onclick = () => { boxes.forEach(b => b.checked = true); sync(); };
  $('#left-go').onclick = async () => {
    const paths = boxes.filter(b => b.checked).map(b => b.dataset.path);
    if (!confirm(`Move ${paths.length} folder(s) to .mylibrary-safety?`)) return;
    const log = $('#left-log');
    const j = await startJob('leftovers_clean', {paths}, log);
    if (j && j.state === 'done') { note(`Moved ${paths.length} folder(s) aside`); loadLeftovers(); }
  };
}
async function loadDupes() {
  const d = await api('/api/dupes');
  const ex = d.exact.map(g => `<div class="card"><h3>Identical files (${fmtSize(g.size)})</h3>
    ${g.files.map(f => `<div>${esc(f)}</div>`).join('')}</div>`).join('');
  const sm = d.same_movie.map(g => `<div class="card"><h3>${esc(g.title)} (${g.year ?? '?'})${g.edition ? ` - ${esc(g.edition)}` : ''}</h3>
    ${g.files.map((f, i) => `<div class="${i ? '' : 'best'}">${i ? '' : '★ '}${f.height || '?'}p
    ${esc(f.vcodec || '')} ${fmtSize(f.size)} — ${esc(f.path)}</div>`).join('')}</div>`).join('');
  $('#dupes').innerHTML = (ex || sm) ? ex + sm : '<p class="empty">No duplicates found.</p>';
}

// ── nav + events ──
function showPage(p) {
  $$('.page').forEach(el => el.classList.toggle('active', el.id === 'page-' + p));
  $$('.nav-link').forEach(el => el.classList.toggle('active', el.dataset.page === p));
  if (p === 'runs') loadRuns();
  if (p === 'dupes') loadDupes();
  if (p === 'leftovers') loadLeftovers();
}
window.onhashchange = () => showPage(location.hash.slice(1) || 'review');
$('#status-list').onclick = e => { const it = e.target.closest('[data-s]');
  if (it) { state.status = it.dataset.s; refresh(); } };
$('#root-list').onclick = e => { const it = e.target.closest('[data-r]');
  if (it) { state.root = it.dataset.r; refresh(); } };
$('#list').onclick = e => {
  const cb = e.target.closest('input[type=checkbox]');
  if (cb) { const id = +cb.dataset.id; cb.checked ? state.checked.add(id) : state.checked.delete(id); return; }
  const row = e.target.closest('.row');
  if (row) { state.cur = +row.dataset.i; renderList(); loadDetail(); }
};
$('#select-all').onchange = e => {
  state.checked = e.target.checked ? new Set(state.items.map(r => r.id)) : new Set(); renderList(); };
$$('.act[data-act]').forEach(b => b.onclick = () => decide([...state.checked], b.dataset.act));
let qTimer;
$('#q').oninput = e => { clearTimeout(qTimer); qTimer = setTimeout(() => { state.q = e.target.value; refresh(); }, 250); };
$$('.job-btn').forEach(b => b.onclick = () => startJob(b.dataset.job));
$('#job-toggle').onclick = () => $('#jobbar').classList.toggle('open');

document.addEventListener('keydown', e => {
  if (e.target.matches('input') && e.key !== 'Escape') return;
  if (e.key === 'Escape') { e.target.blur?.(); $('#apply-overlay').classList.remove('open'); return; }
  const it = state.items[state.cur];
  if (e.key === 'j' && state.cur < state.items.length - 1) { state.cur++; renderList(); loadDetail(); }
  else if (e.key === 'k' && state.cur > 0) { state.cur--; renderList(); loadDetail(); }
  else if (e.key === 'a' && it) decide([it.id], 'approve');
  else if (e.key === 's' && it) decide([it.id], 'skip');
  else if (e.key === 'e') { $('#ed-title')?.focus(); e.preventDefault(); }
  else if (e.key === 'x' && it) { state.checked.has(it.id) ? state.checked.delete(it.id) : state.checked.add(it.id); renderList(); }
  else if (e.key === '/') { $('#q').focus(); e.preventDefault(); }
  else return;
  document.querySelector('.row.sel')?.scrollIntoView({block: 'nearest'});
});

showPage(location.hash.slice(1) || 'review');
refresh();
