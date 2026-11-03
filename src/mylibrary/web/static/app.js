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
     ${s.trash ? `<span>to trash <b>${s.trash}</b></span>` : ''}
     <span>LLM <b>${esc(s.ollama_model || 'off')}</b></span>`;
}

// ── list ──
const confClass = v => v >= 0.9 ? 'hi' : v >= 0.6 ? 'mid' : 'lo';
async function loadList(keep, detail = true) {
  const qs = new URLSearchParams({status: state.status, root: state.root, q: state.q});
  const d = await api('/api/proposals?' + qs);
  state.items = d.items;
  if (!keep) { state.cur = state.items.length ? 0 : -1; state.checked.clear(); }
  else state.cur = Math.min(state.cur, state.items.length - 1);
  renderList();
  if (state.cur >= 0) { if (detail) loadDetail(); } else $('#detail-pane').innerHTML =
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
// Tree sizes (TASK-32): decimal units, one decimal, moving up a unit before it would read 1000.0.
const fmtUnits = b => { for (const [u, d] of [['TB', 1e12], ['GB', 1e9]]) if (b >= d * 0.99995) return (b / d).toFixed(1) + ' ' + u; return b > 0 && b < 5e4 ? '< 0.1 MB' : (b / 1e6).toFixed(1) + ' MB'; };
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
// ── bulk edit ── Shared fields for every checked file; empty fields keep each file's own.
function bulkForm() {
  const ids = [...state.checked];
  if (ids.length < 2) return note('Check two or more files to edit them together.', true);
  $('#detail-pane').innerHTML = `
    <h2>Edit ${ids.length} selected files</h2>
    <p class="reason">Fill in only what they share. Empty fields keep each file's own value, and
      episode numbers, episode titles and track numbers always stay per file. Each file then gets
      its own target and is approved if nothing clashes.</p>
    <div class="detail-label">Identify</div>
    <div class="edit"><select id="bk-kind">
        <option value="">Keep each file's kind</option>
        <option value="movie">Movie</option><option value="episode">TV episode</option>
        <option value="music">Music</option><option value="software">Software</option>
        <option value="scene">Adult scene</option></select>
      <input id="bk-title" placeholder="Title (empty keeps each file's own)">
      <input id="bk-year" placeholder="Year" inputmode="numeric" data-for="movie">
      <input id="bk-edition" placeholder="Edition" data-for="movie">
      <div id="bk-scene" data-for="scene">
        <input id="bk-site" placeholder="Site"><input id="bk-date" placeholder="YYYY-MM-DD">
        <input id="bk-performers" placeholder="Performers, comma separated"></div>
      <div id="bk-tv" data-for="episode"><input id="bk-season" placeholder="Season" inputmode="numeric"></div>
      <div id="bk-music" data-for="music">
        <input id="bk-artist" placeholder="Artist"><input id="bk-album" placeholder="Album">
        <input id="bk-disc" placeholder="Disc" inputmode="numeric"></div></div>
    <div class="btnrow"><button class="btn go" id="bk-save">Save & approve ${ids.length} files</button>
      <button class="btn" id="bk-cancel">Cancel</button></div>
    <div id="bk-result"></div>`;
  const kind = $('#bk-kind');
  const show = () => $$('[data-for]').forEach(el => el.hidden = !!kind.value && el.dataset.for !== kind.value);
  kind.onchange = show; show();
  $$('#detail-pane input, #detail-pane select').forEach(el => el.onkeydown = e => {
    if (e.key === 'Enter') saveBulk(ids); e.stopPropagation(); });
  $('#bk-save').onclick = () => saveBulk(ids);
  $('#bk-cancel').onclick = () => loadDetail();
}
async function saveBulk(ids) {
  const v = id => $(id).closest('[hidden]') ? '' : $(id).value;
  const fields = {kind: $('#bk-kind').value, title: $('#bk-title').value, year: v('#bk-year'),
                  edition: v('#bk-edition'), site: v('#bk-site'), scene_date: v('#bk-date'),
                  performers: v('#bk-performers'), season: v('#bk-season'), artist: v('#bk-artist'),
                  album: v('#bk-album'), disc: v('#bk-disc')};
  try {
    const r = await api('/api/edit-many', {ids, fields});
    const c = r.counts;
    note(`${r.cli} → ${['approved', 'review', 'conflict', 'error'].filter(k => c[k]).map(k => `${c[k]} ${k}`).join(', ')}`,
         !!(c.review || c.conflict || c.error));
    const left = r.results.filter(x => x.status !== 'approved');
    $('#bk-result').innerHTML = left.length ? `<div class="detail-label">Not approved</div>` +
      left.map(x => `<div class="reason"><span class="pill ${x.status}">${x.status}</span> #${x.id}: ${esc(x.reason || '')}</div>`).join('') :
      `<p class="reason">All ${r.results.length} approved.</p>`;
    state.checked.clear();
    await loadSummary(); await loadList(true, false);  // keep the result on screen
  } catch (e) { note(e.message, true); }
}
$('#bulk-edit').onclick = bulkForm;

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
    if (r.dst) note(`${r.cli} → ${r.status}`);
    else note(`Saved, but there is no target yet: ${r.reason}. It stays in review.`, true);
    await refresh(true);
  } catch (e) { note(e.message, true); }
}
async function decide(ids, action) {
  if (!ids.length) return;
  const r = await api('/api/decision', {ids, action});
  if (r.no_target) note(`${r.no_target} item(s) not approved: no target yet. Fill in the missing fields and Save.`, true);
  else note(`${r.cli} → ${r.updated} updated`);
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

// ── tree ── Read only: clicking a video plays it in an overlay (TASK-30); other files and
// the ↗ review link jump to the file's Review entry.
const TREE_MARKS = {in: ['moving in', 'in'], out: ['moving out', 'out'], decide: ['needs a decision', 'decide'],
                    skipped: ['skipped', 'quiet'], ignored: ['ignored', 'quiet'], untracked: ['not tracked', 'quiet'],
                    downloading: ['still downloading', 'off'], trash: ['to trash', 'trash']};
let treeNodes = [];
const treeChanged = n => n.type === 'dir' ? n.in + n.out > 0 : ['in', 'out', 'trash'].includes(n.state);
const treeRel = p => { const r = state.tree.roots.find(x => p.startsWith(x.path + '/')); return r ? p.slice(r.path.length + 1) : p; };
const treeFiles = n => Object.values(n.counts).reduce((a, b) => a + b, 0);
// On Analyze the tree shows only one keyword's files, and folders count only those.
let treeFilter = null;
const wordFiles = n => n.type === 'dir' ? n.children.reduce((a, c) => a + wordFiles(c), 0)
                                        : +treeFilter.paths.has(n.path);
const treeShown = n => treeFilter ? wordFiles(n) > 0 : true;
function treeRow(n, open) {
  const i = treeNodes.push(n) - 1;
  if (n.type === 'dir') {
    const delta = treeFilter ? '' : `${n.in ? `<span class="t-in">+${n.in}</span>` : ''}${n.out ? `<span class="t-out">−${n.out}</span>` : ''}`;
    return `<div class="t-dir" data-n="${i}"><div class="t-row t-folder ${n.new ? 't-new' : ''}">
      <span class="t-arrow">${open ? '▾' : '▸'}</span><span class="t-name">${esc(n.name)}/</span>
      <span class="n">${treeFilter ? wordFiles(n) : treeFiles(n)}</span>${delta}
      ${treeFilter ? '' : `<span class="t-size" title="Size of the files in this folder now">${fmtUnits(n.bytes || 0)}</span>`}
      ${n.new ? '<span class="chip">new folder</span>' : ''}
      ${n.left ? '<span class="chip t-left">left behind</span>' : ''}
      ${n.error ? `<span class="chip t-left">${esc(n.error)}</span>` : ''}</div>
      <div class="t-kids">${open ? treeKids(n) : ''}</div></div>`;
  }
  const mark = TREE_MARKS[n.state];
  const detail = n.to || n.from || n.reason || '';
  const tip = n.play ? 'Play' + (detail ? ' · ' + detail : '') : detail;
  return `<div class="t-row t-file ${mark ? 't-' + mark[1] : ''} ${n.play || n.proposal_id ? 't-link' : ''}" data-n="${i}" title="${esc(tip)}">
    ${n.play ? '<span class="t-play" aria-hidden="true">▶</span>' : ''}<span class="t-name">${esc(n.name)}</span>
    ${n.size != null ? `<span class="n">${fmtUnits(n.size)}</span>` : ''}
    ${mark ? `<span class="chip ${n.state === 'trash' ? 't-trash-chip' : ''}">${mark[0]}</span>` : ''}
    ${n.torrent ? `<span class="chip t-off">in torrent</span>` : ''}
    ${n.symlink ? '<span class="chip">link</span>' : ''}
    ${n.play && n.proposal_id ? '<span class="t-rev" title="Open in Review">review ↗</span>' : ''}
    ${n.state === 'trash' ? '<span class="t-act t-keep" title="Take the trash mark off">keep</span>'
      : canTrash(n) ? '<span class="t-act t-bin" title="Move to trash">🗑</span>' : ''}
    ${detail ? `<span class="t-why">${esc(n.to ? '→ ' + treeRel(n.to) : n.from ? '← ' + treeRel(n.from) : detail)}</span>` : ''}</div>`;
}
const treeAllOpen = () => !!treeFilter || (state.treeOpen ?? $('#tree-changes').checked);
function treeKids(n) {
  const only = $('#tree-changes').checked && !treeFilter;
  return n.children.filter(c => treeShown(c) && (!only || treeChanged(c)))
    .map(c => treeRow(c, treeAllOpen() && c.type === 'dir')).join('');
}
async function loadTree() {
  const d = await api('/api/tree' + ($('#tree-hidden').checked ? '?hidden=1' : ''));
  state.tree = d;
  const t = d.totals;
  $('#tree-head').innerHTML = `
    <div class="t-totals"><span>Video files <b>${t.video}</b></span><span>Music <b>${t.music}</b></span>
      <span>Software <b>${t.software}</b></span><span>Other <b>${t.other}</b></span>
      <span>${d.plan_id ? `Plan #${d.plan_id} moves <b>${d.moves}</b>` : 'No plan yet'}</span>
      <span id="tree-trash-total" ${d.trash ? '' : 'hidden'}>To trash <b>${d.trash}</b></span></div>
    ${d.stale ? '<p class="t-banner">A scan is newer than this plan, so these changes are out of date. Run the pipeline to rebuild it.</p>' : ''}
    ${d.error ? `<p class="t-banner">${esc(d.error)}: nothing would move.</p>` : ''}
    ${d.missing.map(r => `<p class="t-banner">${esc(r)} is not mounted.</p>`).join('')}`;
  renderTree();
}
function renderTree() {
  treeNodes = []; treeFilter = null;
  $('#tree-toggle').textContent = treeAllOpen() ? 'Collapse all' : 'Expand all';
  $('#tree').innerHTML = (state.tree?.roots || []).map(r => treeRow(r, true)).join('') ||
    '<p class="empty">No roots to show.</p>';
}
function treeClick(e, filter) {
  treeFilter = filter;
  const row = e.target.closest('.t-row');
  if (!row) return;
  if (row.classList.contains('t-folder')) {
    const box = row.parentElement, kids = $('.t-kids', box);
    const opening = !kids.children.length;
    kids.innerHTML = opening ? treeKids(treeNodes[+box.dataset.n]) : '';
    $('.t-arrow', row).textContent = opening ? '▾' : '▸';
    return;
  }
  const n = treeNodes[+row.dataset.n];
  if (e.target.closest('.t-bin')) return openTrash(n);
  if (e.target.closest('.t-keep')) return keepFile(n);
  if (n.play && !e.target.closest('.t-rev')) return openPlayer(n);
  if (n.proposal_id) openInReview(n);
}
function openInReview(n) {
  state.status = ''; state.root = ''; state.q = base(n.from || n.path); $('#q').value = state.q;
  location.hash = '#review'; refresh();
}

// ── player ── Streams one file from /api/play; the server checks the path again.
const video = $('#player-video');
const playerOpen = () => $('#player-overlay').classList.contains('open');
const fmtTime = s => s ? `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, '0')}:${String(Math.floor(s % 60)).padStart(2, '0')}` : '';
function playerWarn(lines) {
  $('#player-warn').hidden = !lines.length;
  $('#player-warn').innerHTML = lines.map(esc).join('<br>');
}
// Previous / next follow the tree's order (folders first, then names), through collapsed
// folders too, and respect Only changes and the Analyze keyword filter. Moving-in rows are
// left out: they play the same file as their source row.
function playlist() {
  const only = $('#tree-changes').checked && !treeFilter, out = [];
  const visit = n => n.children.forEach(c => {
    if (!treeShown(c) || (only && !treeChanged(c))) return;
    if (c.type === 'dir') visit(c); else if (c.play && c.state !== 'in') out.push(c);
  });
  (state.tree?.roots || []).forEach(r => treeShown(r) && visit(r));
  return out;
}
function playerNeighbours(n) {
  const list = playlist();
  let i = list.indexOf(n);
  if (i < 0) i = list.findIndex(c => c.play === n.play);
  return {list, i, prev: i > 0 ? list[i - 1] : null, next: i >= 0 && i < list.length - 1 ? list[i + 1] : null};
}
function playerNav(n) {
  const {list, i, prev, next} = playerNeighbours(n);
  $('#player-prev').disabled = !prev; $('#player-next').disabled = !next;
  $('#player-prev').title = prev ? `Previous (P): ${prev.name}` : 'No previous video';
  $('#player-next').title = next ? `Next (N): ${next.name}` : 'No next video';
  $('#player-pos').textContent = i >= 0 ? `${i + 1} / ${list.length}` : '';
}
function playerStep(dir) {
  if (!state.playing) return;
  const nb = playerNeighbours(state.playing), to = dir < 0 ? nb.prev : nb.next;
  if (to) openPlayer(to);
}
$('#player-prev').onclick = () => playerStep(-1);
$('#player-next').onclick = () => playerStep(1);
async function openPlayer(n) {
  state.playing = n;
  playerNav(n);
  $('#player-title').textContent = base(n.play);
  $('#player-meta').textContent = 'Loading…';
  $('#player-review').hidden = !n.proposal_id;
  playerTrashButton(n);
  playerWarn([]);
  if (!playerOpen()) { $('#player-overlay').classList.add('open'); $('#player-close').focus(); }
  try {
    const d = state.playInfo = await api('/api/play/info?path=' + encodeURIComponent(n.play));
    if (state.playing !== n) return;
    $('#player-meta').innerHTML = [
      `<b>${fmtSize(d.size)}</b>`, `.${esc(d.ext)}`,
      d.vcodec && `video <b>${esc(d.vcodec)}</b>`, d.acodec && `audio <b>${esc(d.acodec)}</b>`,
      d.width && `<b>${d.width}×${d.height}</b>`, d.duration && `<b>${fmtTime(d.duration)}</b>`,
      n.state === 'in' ? `planned as ${esc(treeRel(n.path))}` : '',
    ].filter(Boolean).join(' · ');
    $('#player-review').hidden = !(n.proposal_id || d.proposal_id);
    playerWarn(d.warnings);
    video.src = '/api/play?path=' + encodeURIComponent(n.play);
    video.play().catch(() => {});  // autoplay may be blocked; the controls still work
  } catch (e) {
    $('#player-meta').textContent = '';
    playerWarn([`Cannot play this file: ${e.message}`]);
  }
}
function closePlayer() {
  if (!playerOpen()) return;
  video.pause();
  video.removeAttribute('src'); video.load();  // stops the download, not just the picture
  $('#player-overlay').classList.remove('open');
  state.playing = null;
}
video.onerror = () => {
  if (!video.getAttribute('src')) return;
  const d = state.playInfo || {};
  playerWarn([`This browser cannot play this file (.${d.ext || '?'}${d.vcodec ? ', ' + d.vcodec : ''}${d.acodec ? ' / ' + d.acodec : ''}).`,
              ...(d.warnings || []), 'Play it in Jellyfin or VLC instead.']);
};
video.onloadedmetadata = () => {
  if (video.videoWidth) return;
  const d = state.playInfo || {};
  playerWarn([`No picture: this browser cannot decode the video${d.vcodec ? ' (' + d.vcodec + ')' : ''}. Only the sound may play.`,
              ...(d.warnings || []).filter(w => !w.startsWith('The video'))]);
};
$('#player-close').onclick = closePlayer;
$('#player-overlay').onclick = e => { if (e.target.id === 'player-overlay') closePlayer(); };
$('#player-full').onclick = () => video.requestFullscreen?.().catch(() => {});
function playerTrashButton(n) {
  const b = $('#player-trash');
  b.hidden = !(n.state === 'trash' || canTrash(n));
  b.textContent = n.state === 'trash' ? '↩ Keep file (marked for trash)' : '🗑 Move to trash';
}
$('#player-trash').onclick = () => { const n = state.playing; if (n) n.state === 'trash' ? keepFile(n) : openTrash(n); };
$('#player-review').onclick = () => { const n = state.playing; closePlayer(); if (n) openInReview(n); };
$('#tree').onclick = e => treeClick(e, null);
$('#tree-changes').onchange = () => { state.treeOpen = null; renderTree(); };
$('#tree-toggle').onclick = () => { state.treeOpen = !treeAllOpen(); renderTree(); };
$('#tree-hidden').onchange = loadTree;
// Only a class changes, so open folders stay open and nothing is fetched again.
const showSizes = () => $('#tree').classList.toggle('sizes', $('#tree-sizes').checked);
$('#tree-sizes').onchange = showSizes;
showSizes();
$('#tree-refresh').onclick = loadTree;

// ── move to trash (TASK-31) ── Marks are decisions in the database; the next Apply moves the
// files into .mylibrary-safety/run-N/trash/. Rows update in place so the tree keeps its state.
const canTrash = n => n.type === 'file' && n.trash_ok && !['in', 'trash'].includes(n.state);
const trashOpen = () => $('#trash-overlay').classList.contains('open');
function treeRowsFor(path) {  // every rendered row for this file, and its shared node
  return treeNodes.map((n, i) => [n, i]).filter(([n]) => n.type === 'file' && n.path === path && n.state !== 'in');
}
function rerenderRows(path) {
  for (const [n, i] of treeNodes.map((n, i) => [n, i]).filter(([n]) => n.type === 'file' && n.path === path)) {
    const el = $(`#tree .t-file[data-n="${i}"], #az-tree .t-file[data-n="${i}"]`);
    if (el) el.outerHTML = treeRow(n);
  }
}
function setTrashTotal(delta) {
  if (!state.tree) return;
  state.tree.trash = Math.max(0, (state.tree.trash || 0) + delta);
  const el = $('#tree-trash-total');
  if (el) { el.hidden = !state.tree.trash; $('b', el).textContent = state.tree.trash; }
}
// After a mark changes, the plan around it can change too (a held-back video keeps its
// subtitles). Fetch the tree again and update file rows in place, so open folders stay open.
async function refreshTreeMarks() {
  loadSummary().catch(() => {});
  if (!state.tree) return;
  try {
    const d = await api('/api/tree' + ($('#tree-hidden').checked ? '?hidden=1' : ''));
    const fresh = new Map();
    const walk = n => n.children.forEach(c => c.type === 'dir' ? walk(c) : fresh.set(c.path + '|' + (c.state === 'in'), c));
    d.roots.forEach(walk);
    const seen = new Set();
    treeNodes.forEach(n => {
      if (n.type !== 'file' || seen.has(n)) return;
      seen.add(n);
      const f = fresh.get(n.path + '|' + (n.state === 'in'));
      if (!f) return;
      for (const k of ['state', 'reason', 'to', 'from', 'was', 'trash_refused', 'trash_ok', 'proposal_id', 'play']) n[k] = f[k];
    });
    [...new Set(treeNodes.filter(n => n.type === 'file').map(n => n.path))].forEach(rerenderRows);
    state.tree.trash = d.trash; setTrashTotal(0);
  } catch (e) { /* the rows already show the change; a refresh fixes the rest */ }
}
async function openTrash(n) {
  state.trashFor = n;
  $('#trash-body').innerHTML = '<p class="side-note">Checking…</p>';
  $('#trash-ok').disabled = true;
  $('#trash-overlay').classList.add('open');
  $('#trash-cancel').focus();
  try {
    const d = await api('/api/trash/preview?path=' + encodeURIComponent(n.path));
    if (state.trashFor !== n) return;
    if (d.error) { $('#trash-body').innerHTML = `<p class="t-banner">Cannot move this file to trash: ${esc(d.error)}</p>`; return; }
    $('#trash-body').innerHTML = `<div class="trash-file">${esc(d.name)}<span class="n">${fmtSize(d.size)}</span></div>
      <div class="side-note">${esc(treeRel(d.path))}</div>
      ${d.sidecars.length ? `<div class="trash-side">Files that belong with it:
        ${d.sidecars.map(sc => `<label><input type="checkbox" data-p="${esc(sc.path)}" ${sc.error ? 'disabled' : 'checked'}>
          ${esc(sc.name)} <span class="n">${sc.size != null ? fmtSize(sc.size) : ''}</span>
          ${sc.error ? `<span class="t-why">${esc(sc.error)}</span>` : ''}</label>`).join('')}</div>` : ''}`;
    $('#trash-ok').disabled = false;
    $('#trash-ok').focus();
  } catch (e) { $('#trash-body').innerHTML = `<p class="t-banner">${esc(e.message)}</p>`; }
}
function closeTrash() { $('#trash-overlay').classList.remove('open'); state.trashFor = null; }
$('#trash-cancel').onclick = closeTrash;
$('#trash-overlay').onclick = e => { if (e.target.id === 'trash-overlay') closeTrash(); };
$('#trash-ok').onclick = async () => {
  const n = state.trashFor;
  if (!n) return;
  const paths = [n.path, ...$$('#trash-body .trash-side input:checked').map(i => i.dataset.p)];
  $('#trash-ok').disabled = true;
  try {
    const r = await api('/api/trash', {paths});
    for (const p of r.marked) {
      for (const [node] of treeRowsFor(p)) {
        if (node.state === 'trash') continue;
        node.was = {state: node.state, reason: node.reason};
        Object.assign(node, {state: 'trash', reason: 'moves to trash on next Apply', trash_refused: false});
      }
      rerenderRows(p);
    }
    setTrashTotal(r.marked.length);
    closeTrash();
    refreshTreeMarks();
    if (state.playing) playerTrashButton(state.playing);
    note(r.refused.length ? `Marked ${r.marked.length}; refused: ${r.refused.map(x => base(x.path) + ' (' + x.reason + ')').join(', ')}`
                          : `Marked ${r.marked.length} for trash. Apply moves them. ${r.cli}`, !!r.refused.length);
  } catch (e) { note(e.message, true); $('#trash-ok').disabled = false; }
};
async function keepFile(n) {
  try {
    const r = await api('/api/trash/clear', {paths: [n.path]});
    for (const [node] of treeRowsFor(n.path)) {
      const was = node.was || {};
      Object.assign(node, {state: was.state || '', reason: was.reason || null, trash_refused: false});
    }
    rerenderRows(n.path);
    setTrashTotal(-r.cleared);
    refreshTreeMarks();
    if (state.playing) playerTrashButton(state.playing);
    note(`Kept ${base(n.path)}. ${r.cli}`);
  } catch (e) { note(e.message, true); }
}

// ── analyze ── Read only: a keyword shows its files in a tree under the heatmap.
const azBars = items => {
  const top = Math.max(...items.map(i => i.count), 1);
  return items.map(i => `<div class="bar-row" title="${esc(i.label)}: ${i.count} file(s)">
    <span class="bar-label">${esc(i.label)}</span>
    <span class="bar-track"><span class="bar-fill" style="width:${Math.round(i.count / top * 100)}%"></span></span>
    <span class="bar-val">${i.count}</span></div>`).join('') || '<p class="empty">No files.</p>';
};
function azCloud(words) {
  if (!words.length) return '<p class="empty">Not enough files to show keywords yet.</p>';
  const counts = words.map(w => w.count);
  const logs = words.map(w => Math.log(w.avg_size + 1));  // sizes span KB to GB
  const scale = (v, lo, hi) => hi > lo ? (v - lo) / (hi - lo) : 1;
  const [cMin, cMax, lMin, lMax] = [Math.min(...counts), Math.max(...counts), Math.min(...logs), Math.max(...logs)];
  return '<div class="word-cloud">' + words.map((w, i) => {
    const em = (0.72 + scale(w.count, cMin, cMax) * 1.6).toFixed(2);
    const col = `color-mix(in srgb, #f90 ${Math.round(scale(logs[i], lMin, lMax) * 100)}%, #4af)`;
    return `<button class="wc-word ${state.azWord?.word === w.word ? 'active' : ''}" data-i="${i}"
      style="font-size:${em}rem;--c:${col}"
      title="${esc(w.word)}: ${w.count} file(s), average ${fmtSize(w.avg_size)}. Click to show its files.">${esc(w.word)}</button>`;
  }).join('') + '</div>';
}
async function loadAnalyze() {
  const d = await api('/api/analyze?root=' + encodeURIComponent($('#analyze-root').value));
  state.analyze = d;
  $('#analyze-root').innerHTML = [['', 'All roots'], ...d.roots.map(r => [r, r])].map(([v, label]) =>
    `<option value="${esc(v)}" ${v === d.root ? 'selected' : ''}>${esc(label)}</option>`).join('');
  $('#analyze-wrap').innerHTML = `
    ${d.missing.map(r => `<p class="t-banner">${esc(r)} is not mounted.</p>`).join('')}
    ${d.error ? `<p class="t-banner">${esc(d.error)}</p>` : ''}
    <p class="az-meta"><b>${d.total}</b> file(s) · <b>${d.words.length}</b> keyword(s)</p>
    <div class="az-grid">
      <div class="az-card"><h3>Files by type</h3>${azBars(d.by_type)}</div>
      <div class="az-card"><h3>Files by state</h3>${azBars(d.by_state)}</div>
    </div>
    <div class="az-card"><h3>Word heatmap
      <span class="az-legend">size = files · colour = average file size (blue = small → amber = large)</span></h3>
      ${azCloud(d.words)}<div id="az-drill"></div></div>`;
  if (state.azWord) azPick(d.words.find(w => w.word === state.azWord.word));
}
function azPick(w) {
  state.azWord = w ? {word: w.word, paths: new Set(w.paths)} : null;
  $$('.wc-word').forEach(el => el.classList.toggle('active', el.textContent === w?.word));
  azDrill();
}
async function azDrill() {
  const w = state.azWord;
  if (!w) { $('#az-drill').innerHTML = ''; return; }
  const d = await api('/api/tree');
  if (state.azWord !== w) return;  // another word was clicked meanwhile
  state.tree = d; treeNodes = []; treeFilter = w;
  const roots = d.roots.filter(treeShown);
  $('#az-drill').innerHTML = `<div class="az-drill-head">
      <h4>"${esc(w.word)}" — ${roots.reduce((a, r) => a + wordFiles(r), 0)} file(s)</h4>
      <button class="btn" id="az-clear">Clear</button></div>
    <div id="az-tree">${roots.map(r => treeRow(r, true)).join('') ||
      '<p class="empty">No files match this keyword now.</p>'}</div>`;
  $('#az-drill').scrollIntoView({behavior: 'smooth', block: 'nearest'});
}
$('#analyze-root').onchange = () => { state.azWord = null; loadAnalyze(); };
$('#analyze-wrap').onclick = e => {
  if (e.target.id === 'az-clear') return azPick(null);
  if (e.target.closest('#az-tree')) return treeClick(e, state.azWord);
  const el = e.target.closest('.wc-word');
  if (el) azPick(el.classList.contains('active') ? null : state.analyze.words[+el.dataset.i]);
};

// ── nav + events ──
function showPage(p) {
  closeTrash(); closePlayer();
  $$('.page').forEach(el => el.classList.toggle('active', el.id === 'page-' + p));
  $$('.nav-link').forEach(el => el.classList.toggle('active', el.dataset.page === p));
  if (p === 'tree') loadTree();
  if (p === 'analyze') loadAnalyze();
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
  if (trashOpen()) {  // the confirmation sits above the player and the tree
    if (e.key === 'Escape') closeTrash();
    return;
  }
  if (playerOpen()) {  // the video keeps its own keys; no Review shortcuts behind the player
    if (e.key === 'Escape' && !document.fullscreenElement) closePlayer();
    else if ((e.key === 'n' || e.key === 'N') && !e.ctrlKey && !e.metaKey) playerStep(1);
    else if ((e.key === 'p' || e.key === 'P') && !e.ctrlKey && !e.metaKey) playerStep(-1);
    return;
  }
  if (menuOpen() || $('#about-overlay').classList.contains('open')) {  // no Review shortcuts here
    if (e.key === 'Escape') { $('#about-overlay').classList.contains('open') ? closeAbout() : (toggleConfig(false), $('#config-btn').focus()); }
    return;
  }
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

// ── ⚙ Config menu (TASK-27) and ℹ About (TASK-26) ──
async function loadVersion() {
  state.version = await api('/api/version');
  $('#app-version').textContent = state.version.label;
}
const menuOpen = () => $('#config-menu').classList.contains('open');
function toggleConfig(open = !menuOpen()) {
  $('#config-menu').classList.toggle('open', open);
  $('#config-btn').setAttribute('aria-expanded', open);
  if (open) $('#config-list button').focus();
}
async function showAbout() {
  toggleConfig(false);
  $('#about-body').innerHTML = '<span>Loading…</span><span></span>';
  $('#about-overlay').classList.add('open');
  try {
    const v = state.version = await api('/api/version');
    const m = v.milestone;
    $('#app-version').textContent = v.label;
    $('#about-body').innerHTML = [
      ['Version', `<code>${esc(v.label)}</code>`],
      ['Release', `v${esc(v.version)}${v.tag ? ` (tag ${esc(v.tag)})` : ' (this commit is not tagged)'}`],
      ['Code', `commit ${esc(v.commit || 'unknown')}${v.branch ? ` on ${esc(v.branch)}` : ''}${v.dirty ? ', with uncommitted changes' : ''}`],
      ['Milestone', m ? `${esc(m.title)}: ${m.done} of ${m.total} task(s) done` : `none named v${esc(v.version)}`],
    ].map(([k, val]) => `<span>${k}</span><span>${val}</span>`).join('');
  } catch (e) { $('#about-body').innerHTML = `<span>Error</span><span>${esc(e.message)}</span>`; }
  $('#about-close').focus();
}
const closeAbout = () => { $('#about-overlay').classList.remove('open'); $('#config-btn').focus(); };
$('#config-btn').onclick = e => { e.stopPropagation(); toggleConfig(); };
$('#about-btn').onclick = showAbout;
$('#about-close').onclick = closeAbout;
$('#about-overlay').onclick = e => { if (e.target.id === 'about-overlay') closeAbout(); };
document.addEventListener('click', e => { if (menuOpen() && !e.target.closest('#config-menu')) toggleConfig(false); });

showPage(location.hash.slice(1) || 'review');
refresh();
loadVersion().catch(() => {});
