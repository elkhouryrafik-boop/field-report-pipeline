/* s5b review app — vanilla JS, no framework, no build step, no CDN.
 *
 * Every request goes to this server's own origin: GET /api/stops, GET
 * /api/orphans, GET /media/<filename>, POST /api/stop/<cluster_id>,
 * POST /api/finish. Nothing else. The reviewer's machine is often offline.
 *
 * Two rules that shape the whole file:
 *   1. review.json is merged per top-level key by the server, so a patch that
 *      touches photos or audios must carry the WHOLE projected array.
 *   2. Never lose an edit. Failed POSTs stay queued, the value stays in the
 *      DOM, and the header says so in words the reviewer cannot miss.
 */

'use strict';

var DEBOUNCE_MS = 400;

var state = {
  stops: [],
  idx: 0,
  saving: 0,
  mode: 'browse',           // 'browse' | 'rebind'
  site: '',
  reviewer: '',
  total: 0,
  counts: {},
  hasPrelabels: false,
  resumeAt: null,
  filter: 'todo',           // 'todo' = pending or conflicted only | 'all'
  audioIdx: 0,              // focused audio row inside the current card
  rebind: null,             // { audioIdx: n, sel: Set<filename> }
  orphans: [],
  ui: {},                   // transient per-card UI (open editors, inline bars)
  lastSavedAt: null,
};

/* ------------------------------------------------------------------ utils */

function el(tag, attrs) {
  var n = document.createElement(tag), k, v;
  if (attrs) {
    for (k in attrs) {
      if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
      v = attrs[k];
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = String(v);
      else if (k === 'value') n.value = v;
      else if (k.slice(0, 2) === 'on') n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? '' : String(v));
    }
  }
  for (var i = 2; i < arguments.length; i++) append(n, arguments[i]);
  return n;
}

function append(parent, child) {
  if (child === null || child === undefined || child === false) return;
  if (Array.isArray(child)) { child.forEach(function (c) { append(parent, c); }); return; }
  parent.appendChild(typeof child === 'object' ? child : document.createTextNode(String(child)));
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function hhmm(iso) {
  if (!iso) return '—';
  var m = String(iso).match(/T(\d{2}:\d{2})/);
  return m ? m[1] : String(iso);
}

function clock(d) {
  function p(n) { return (n < 10 ? '0' : '') + n; }
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

// "DOC-0001-10-39-22.opus" -> "AUD-3011"
function shortAudio(name) {
  var m = String(name).match(/^0*(\d+)-AUDIO/i);
  if (m) return 'AUD-' + m[1];
  return String(name).replace(/\.[a-z0-9]+$/i, '').slice(-14);
}

function sameSet(a, b) {
  if (a.length !== b.length) return false;
  var s = {}, i;
  for (i = 0; i < a.length; i++) s[a[i]] = 1;
  for (i = 0; i < b.length; i++) if (!s[b[i]]) return false;
  return true;
}

function currentStop() { return state.stops[state.idx] || null; }

function shortPhoto(name) {
  var m = String(name).match(/^0*(\d+)-PHOTO/i);
  if (m) return 'IMG-' + m[1];
  return String(name).replace(/\.[a-z0-9]+$/i, '').slice(-14);
}

/* ------------------------------------------------------ what still needs me */
/* A client-side mirror of s5c's pass-1 validation and the bound-to-a-removed-
 * photo check in _apply_audios. Same predicates, same wording, so what the rail
 * flags is exactly what FINISH and s5c would refuse. */

function stopConflicts(s) {
  var out = [], known = {}, discarded = {}, excluded = {}, moved = {};
  state.stops.forEach(function (x) {
    known[x.cluster_id] = 1;
    if (x.verdict === 'discarded') discarded[x.cluster_id] = 1;
  });
  s.photos.forEach(function (p) {
    if (p.excluded) excluded[p.filename] = 1;
    if (p.move_to) moved[p.filename] = 1;
  });

  if (s.split_at && s.merge_with) out.push('split and merge are both set — pick one');
  if (s.merge_with) {
    if (!known[s.merge_with]) out.push('merge target ' + s.merge_with + ' does not exist');
    else if (discarded[s.merge_with]) out.push('merge target ' + s.merge_with + ' was discarded');
    else if (s.merge_with === s.cluster_id) out.push('cannot merge a stop with itself');
  }
  s.photos.forEach(function (p) {
    if (!p.move_to) return;
    if (!known[p.move_to]) out.push(shortPhoto(p.filename) + ' moves to ' + p.move_to + ', which does not exist');
    else if (discarded[p.move_to]) out.push(shortPhoto(p.filename) + ' moves to ' + p.move_to + ', which was discarded');
    else if (p.move_to === s.cluster_id) out.push(shortPhoto(p.filename) + ' moves to its own stop');
  });
  s.audios.forEach(function (a) {
    if (a.dropped) return;
    var bad = (a.binding || []).filter(function (f) { return excluded[f] || moved[f]; });
    if (bad.length) {
      out.push(shortAudio(a.file) + ' is bound to ' + bad.map(shortPhoto).join(', ') +
               ' — this review excluded or moved that photo away');
    }
  });
  return out;
}

function needsWork(s) { return s.verdict === 'pending' || stopConflicts(s).length > 0; }

function visibleIndices() {
  var out = [];
  state.stops.forEach(function (s, i) {
    if (state.filter === 'all' || needsWork(s) || i === state.idx) out.push(i);
  });
  return out;
}

/* -------------------------------------------------------- incoming photos */
/* A photo with move_to === this stop is not in this stop's cluster yet — s5c
 * relocates it in pass 3. But pass 2 only rejects a binding that names a photo
 * leaving the stop, so a binding onto an INCOMING photo is legal and survives.
 * Without this the reviewer cannot express the commonest correction there is:
 * the pin cut a stop in half, so the photo and its voice note sit apart. */

function incomingPhotos(s) {
  var out = [];
  state.stops.forEach(function (src) {
    if (src.cluster_id === s.cluster_id) return;
    src.photos.forEach(function (p) {
      if (p.move_to === s.cluster_id && !p.excluded) out.push({ photo: p, from: src.cluster_id });
    });
  });
  return out;
}

/* Own photos keep their numbers; incoming ones continue the sequence. */
function bindablePhotos(s) {
  var list = s.photos.slice();
  incomingPhotos(s).forEach(function (inc) { list.push(inc.photo); });
  return list;
}

// Split "[unclear]" out of a transcript line so it reads as a marker, not text.
function withUnclear(text) {
  var out = [], parts = String(text == null ? '' : text).split('[unclear]');
  for (var i = 0; i < parts.length; i++) {
    if (parts[i]) out.push(document.createTextNode(parts[i]));
    if (i < parts.length - 1) out.push(el('span', { class: 'unclear', text: '[unclear]' }));
  }
  return out;
}

/* ------------------------------------------------------- save engine ---- */
/* One in-flight POST at a time. Patches are keyed by cluster_id and merged, so
 * a retry always sends the newest value rather than replaying a stale one. */

var saver = { pending: [], byId: {}, timer: null, inflight: false, attempt: 0, failed: false };

function saveStop(patch, immediate) {
  var stop = currentStop();
  if (!stop) return;
  saveStopFor(stop.cluster_id, patch, immediate);
}

function saveStopFor(clusterId, patch, immediate) {
  var cur = saver.byId[clusterId];
  if (!cur) { cur = saver.byId[clusterId] = {}; saver.pending.push(clusterId); }
  for (var k in patch) if (Object.prototype.hasOwnProperty.call(patch, k)) cur[k] = patch[k];
  if (saver.timer) { clearTimeout(saver.timer); saver.timer = null; }
  if (immediate) flush();
  else { setSaveState('saving'); saver.timer = setTimeout(flush, DEBOUNCE_MS); }
}

function flush() {
  if (saver.timer) { clearTimeout(saver.timer); saver.timer = null; }
  if (saver.inflight || !saver.pending.length) { if (!saver.pending.length) settleSaveState(); return; }

  var clusterId = saver.pending.shift();
  var patch = saver.byId[clusterId];
  delete saver.byId[clusterId];
  saver.inflight = true;
  state.saving++;
  setSaveState('saving');

  var body = { cluster_id: clusterId };
  for (var k in patch) if (Object.prototype.hasOwnProperty.call(patch, k)) body[k] = patch[k];

  fetch('/api/stop/' + encodeURIComponent(clusterId), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  }).then(function (res) {
    return res.text().then(function (t) {
      var data = {};
      try { data = JSON.parse(t); } catch (e) { data = { error: t.slice(0, 200) }; }
      if (!res.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + res.status));
      return data;
    });
  }).then(function (data) {
    saver.inflight = false;
    state.saving--;
    saver.failed = false;
    saver.attempt = 0;
    if (data.counts) state.counts = data.counts;
    state.resumeAt = data.resume_at;
    state.lastSavedAt = new Date();
    renderHeader();
    renderRail();
    if (saver.pending.length) flush(); else settleSaveState();
  }).catch(function (err) {
    saver.inflight = false;
    state.saving--;
    // Put the patch back UNDER anything newer, so the newest value still wins.
    var newer = saver.byId[clusterId];
    if (newer) {
      for (var k2 in newer) if (Object.prototype.hasOwnProperty.call(newer, k2)) patch[k2] = newer[k2];
    } else {
      saver.pending.unshift(clusterId);
    }
    saver.byId[clusterId] = patch;
    saver.failed = true;
    saver.attempt++;
    setSaveState('failed', String(err && err.message ? err.message : err));
    var wait = Math.min(1500 * Math.pow(2, Math.min(saver.attempt - 1, 4)), 20000);
    saver.timer = setTimeout(flush, wait);
  });
}

function setSaveState(kind, detail) {
  var n = document.getElementById('savestate');
  if (!n) return;
  n.className = 'save ' + kind;
  if (kind === 'saving') n.textContent = 'saving…';
  else if (kind === 'saved') n.textContent = 'saved ' + clock(state.lastSavedAt || new Date());
  else if (kind === 'failed') n.textContent = 'SAVE FAILED — do not close this tab';
  else n.textContent = 'ready';
  n.title = detail || (kind === 'failed' ? 'retrying…' : '');
}

function settleSaveState() {
  if (saver.failed || saver.pending.length) return;
  setSaveState(state.lastSavedAt ? 'saved' : 'idle');
}

window.addEventListener('beforeunload', function (e) {
  if (saver.pending.length || saver.inflight) {
    e.preventDefault();
    e.returnValue = 'Edits are still saving.';
    return e.returnValue;
  }
});

/* ------------------------------------------------------------ projections */
/* review.schema.json forbids extra keys, and the API payload carries plenty of
 * them (url, ts, caption, …). Project down to exactly the recorded decision. */

function photoPatch(stop) {
  return stop.photos.map(function (p) {
    var excluded = !!p.excluded;
    return {
      filename: p.filename,
      caption_override: p.caption_override || null,
      excluded: excluded,
      exclude_reason: p.exclude_reason || null,
      move_to: excluded ? null : (p.move_to || null)   // schema: excluded ⇒ move_to null
    };
  });
}

function audioPatch(stop) {
  return stop.audios.map(function (a) {
    return {
      file: a.file,
      text_override: a.text_override || null,
      en_override: a.en_override || null,
      binding: (a.binding || []).slice(),
      binding_source: a.binding_source || 'none',
      dropped: !!a.dropped,
      drop_reason: a.dropped ? (a.drop_reason || null) : null
    };
  });
}

function savePhotos() { var s = currentStop(); saveStop({ photos: photoPatch(s) }, true); }
function saveAudios() { var s = currentStop(); saveStop({ audios: audioPatch(s) }, true); }

/* ------------------------------------------------------------------- load */

function load() {
  return fetch('/api/stops').then(function (r) {
    if (!r.ok) throw new Error('GET /api/stops → HTTP ' + r.status);
    return r.json();
  }).then(function (data) {
    state.stops = data.stops || [];
    state.total = data.total || state.stops.length;
    state.site = data.site || '';
    state.reviewer = data.reviewer || '';
    state.counts = data.counts || {};
    state.hasPrelabels = !!data.has_prelabels;
    state.resumeAt = data.resume_at;
    document.title = (state.site || 'review') + ' — s5b';
    var start = 0;
    if (state.resumeAt) {
      for (var i = 0; i < state.stops.length; i++) {
        if (state.stops[i].cluster_id === state.resumeAt) { start = i; break; }
      }
    }
    goTo(start, true);
    return loadOrphans();
  }).catch(function (err) {
    var host = document.getElementById('card-host');
    clear(host);
    append(host, el('p', { class: 'loading', text: 'could not load: ' + err.message }));
  });
}

function loadOrphans() {
  return fetch('/api/orphans').then(function (r) { return r.json(); }).then(function (d) {
    state.orphans = d.orphans || [];
    renderTray();
  }).catch(function () { /* the tray is secondary; never block the cards */ });
}

/* ----------------------------------------------------------- render: chrome */

function render() {
  renderHeader();
  renderRail();
  renderCard();
}

function renderHeader() {
  var c = state.counts || {};
  var done = (c.approved || 0) + (c.discarded || 0);
  document.getElementById('site').textContent = state.site || '';
  document.getElementById('reviewer').textContent = state.reviewer ? 'reviewer: ' + state.reviewer : '';
  document.getElementById('progress').textContent = done + '/' + state.total;
  document.getElementById('progressfill').style.width =
    (state.total ? Math.round(100 * done / state.total) : 0) + '%';

  var box = document.getElementById('counts');
  clear(box);
  function stat(label, value) {
    return el('span', null, label + ' ', el('b', { text: String(value == null ? 0 : value) }));
  }
  append(box, [
    stat('approved', c.approved), stat('discarded', c.discarded), stat('pending', c.pending),
    stat('bind a/c/m', (c.bindings_accepted || 0) + '/' + (c.bindings_corrected || 0) + '/' + (c.bindings_manual || 0)),
    stat('photos excl/moved', (c.photos_excluded || 0) + '/' + (c.photos_moved || 0)),
    stat('texts edited', c.texts_edited)
  ]);
}

function renderRail() {
  var rail = document.getElementById('rail');
  clear(rail);

  var todo = 0, conflicted = 0;
  state.stops.forEach(function (s) {
    if (needsWork(s)) todo++;
    if (stopConflicts(s).length) conflicted++;
  });

  var chips = el('div', { class: 'railfilter' });
  [['todo', 'To do', todo], ['all', 'All', state.stops.length]].forEach(function (f) {
    append(chips, el('button', {
      type: 'button', class: 'chip' + (state.filter === f[0] ? ' on' : ''),
      title: f[0] === 'todo' ? 'pending stops and anything s5c would refuse' : 'every stop, including the ones you finished',
      onclick: function () { state.filter = f[0]; renderRail(); }
    }, el('span', { text: f[1] }), el('b', { text: String(f[2]) })));
  });
  append(rail, chips);
  if (conflicted) {
    append(rail, el('div', { class: 'railnote bad',
      text: conflicted + ' stop' + (conflicted === 1 ? '' : 's') + ' would make s5c exit 4' }));
  }

  var vis = visibleIndices();
  if (!vis.length) {
    append(rail, el('p', { class: 'dim railnote', text: 'nothing left — press FINISH' }));
    return;
  }
  vis.forEach(function (i) {
    var s = state.stops[i];
    var bad = stopConflicts(s);
    var cls = 'rail-item ' + s.verdict;
    if (i === state.idx) cls += ' current';
    if (s.cluster_id === state.resumeAt) cls += ' resume';
    if (bad.length) cls += ' problem';
    var tick = bad.length ? '⚠' : (s.verdict === 'approved' ? '✓' : (s.verdict === 'discarded' ? '✗' : '·'));
    append(rail, el('button', {
      class: cls, type: 'button',
      title: s.cluster_id + ' — ' + s.verdict + (bad.length ? '\n' + bad.join('\n') : ''),
      onclick: function () { goTo(i); }
    },
      el('span', { class: 'tick', text: tick }),
      el('span', { text: s.cluster_id }),
      el('span', { class: 'meta', text: s.photos.length + 'p/' + s.audios.length + 'a' })
    ));
  });
}

function goTo(i, silent) {
  if (i < 0 || i >= state.stops.length) return;
  state.idx = i;
  state.audioIdx = 0;
  state.mode = 'browse';
  state.rebind = null;
  state.ui = {};
  if (silent) render(); else { renderRail(); renderCard(); }
  document.getElementById('card-host').scrollTop = 0;
  var active = document.querySelector('.rail-item.current');
  if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
}

/* J/K walk what the filter is showing, so "next" never lands on a stop you
 * already finished while you are working the To-do list. */
function step(dir) {
  var vis = visibleIndices();
  if (!vis.length) return;
  var at = vis.indexOf(state.idx);
  if (at === -1) {
    var side = vis.filter(function (i) { return dir > 0 ? i > state.idx : i < state.idx; });
    goTo(side.length ? (dir > 0 ? side[0] : side[side.length - 1])
                     : vis[dir > 0 ? 0 : vis.length - 1]);
    return;
  }
  var nx = at + dir;
  if (nx < 0 || nx >= vis.length) return;
  goTo(vis[nx]);
}

function nextStop() { step(1); }
function prevStop() { step(-1); }

function jumpToNextPending() {
  var i;
  for (i = state.idx + 1; i < state.stops.length; i++) {
    if (needsWork(state.stops[i])) { goTo(i); return true; }
  }
  for (i = 0; i < state.stops.length; i++) {
    if (needsWork(state.stops[i])) { goTo(i); return true; }
  }
  return false;
}

/* ------------------------------------------------------------ render: card */

function renderCard() {
  var host = document.getElementById('card-host');
  clear(host);
  var s = currentStop();
  if (!s) { append(host, el('p', { class: 'loading', text: 'no stops' })); return; }

  var card = el('div', { class: 'card is-' + s.verdict, id: 'card' });

  /* header ------------------------------------------------------------- */
  append(card, el('div', { class: 'card-head' },
    el('span', { class: 'cid', text: s.cluster_id }),
    el('span', { class: 'when', text: hhmm(s.start_ts) + '–' + hhmm(s.end_ts) }),
    el('span', { class: 'dim', text: s.photos.length + ' photos, ' + s.audios.length + ' audio' }),
    el('span', { class: 'dim', text: 'boundary: ' + s.boundary_reason }),
    el('span', { class: 'pill ' + s.verdict, text: s.verdict }),
    el('span', { class: 'pos', text: (s.index + 1) + '/' + s.total })
  ));

  /* conflicts — these block s5c, so they sit above everything else -------- */
  var bad = stopConflicts(s);
  if (bad.length) {
    var cbox = el('div', { class: 'conflicts' },
      el('h4', { text: 'Blocks s5c (exit 4) until fixed' }));
    bad.forEach(function (b) { append(cbox, el('div', { text: '✗ ' + b })); });
    append(card, cbox);
  }

  /* warnings ----------------------------------------------------------- */
  var warns = (s.warnings || []).slice();
  var unbound = unboundPhotos(s);
  if (warns.length) {
    var wbox = el('div', { class: 'warnings' });
    warns.forEach(function (w) { append(wbox, el('div', { text: '⚠ ' + w })); });
    append(card, wbox);
  }

  /* map row ------------------------------------------------------------ */
  append(card, renderMapRow(s));

  /* photos ------------------------------------------------------------- */
  var psec = el('div', { class: 'section' },
    el('h3', { text: 'photos (' + s.photos.length + ')' + (state.mode === 'rebind' ? ' — click a tile, or press 1–9, to bind' : '') }));
  var grid = el('div', { class: 'photos' });
  if (!s.photos.length) append(grid, el('p', { class: 'dim', text: 'no photos at this stop' }));
  s.photos.forEach(function (p, i) { append(grid, renderPhoto(p, i)); });
  var incoming = incomingPhotos(s);
  incoming.forEach(function (inc, k) {
    append(grid, renderIncoming(inc, s.photos.length + k));
  });
  append(psec, grid);
  if (incoming.length) {
    append(psec, el('div', { class: 'incoming-note',
      text: incoming.length + ' photo' + (incoming.length === 1 ? '' : 's') +
            ' arriving from another stop. s5c moves them here in pass 3 — you can bind a voice ' +
            'note to them now.' }));
  }
  if (unbound.length && s.audios.length) {
    append(psec, el('div', { class: 'unbound-list',
      text: 'unbound (no voice note refers to these): ' + unbound.map(function (f, i) { return '#' + (photoIndex(s, f) + 1); }).join(', ') }));
  }
  append(card, psec);

  /* audios ------------------------------------------------------------- */
  var asec = el('div', { class: 'section' }, el('h3', { text: 'voice notes (' + s.audios.length + ')' }));
  if (!s.audios.length) append(asec, el('p', { class: 'dim', text: 'no voice notes at this stop' }));
  s.audios.forEach(function (a, i) { append(asec, renderAudio(a, i)); });
  append(card, asec);

  /* chat texts (context, not actionable) -------------------------------- */
  if (s.texts && s.texts.length) {
    var tsec = el('div', { class: 'section' }, el('h3', { text: 'chat text in this stop' }));
    s.texts.forEach(function (t) {
      append(tsec, el('div', { class: 'dim', text: hhmm(t.ts) + '  ' + (t.sender || '') + ': ' + t.text }));
    });
    append(card, tsec);
  }

  /* note ---------------------------------------------------------------- */
  var nsec = el('div', { class: 'section' }, el('h3', { text: '📝 stop note — outranks every machine signal in s6' }));
  var note = el('textarea', {
    id: 'note', rows: 2, placeholder: 'e.g. this is the east stair, not the west one',
    oninput: function () { s.human_note = note.value || null; saveStop({ human_note: s.human_note }, false); },
    onblur: function () { saveStop({ human_note: note.value || null }, true); },
    onkeydown: function (ev) {
      if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); note.blur(); }
      if (ev.key === 'Escape') { note.blur(); }
    }
  });
  note.value = s.human_note || '';
  append(nsec, note);
  append(card, nsec);

  /* actions -------------------------------------------------------------- */
  append(card, renderActions(s));

  append(host, card);

  if (state.ui.focus) {
    var f = document.getElementById(state.ui.focus);
    if (f) { f.focus(); if (f.setSelectionRange && f.value) f.setSelectionRange(f.value.length, f.value.length); }
    state.ui.focus = null;
  }
}

/* Swap one tile / one audio row in place. Used when an editor closes on blur:
 * a full re-render during mousedown eats the click the reviewer aimed at the
 * next control. */
function refreshTile(i) {
  var old = document.getElementById('tile-' + i);
  if (!old || !old.parentNode) { renderCard(); return; }
  old.parentNode.replaceChild(renderPhoto(currentStop().photos[i], i), old);
}

function refreshAudio(i) {
  var old = document.getElementById('audio-' + i);
  if (!old || !old.parentNode) { renderCard(); return; }
  old.parentNode.replaceChild(renderAudio(currentStop().audios[i], i), old);
}

function photoIndex(s, filename) {
  for (var i = 0; i < s.photos.length; i++) if (s.photos[i].filename === filename) return i;
  return -1;
}

function unboundPhotos(s) {
  var bound = {};
  s.audios.forEach(function (a) {
    if (a.dropped) return;
    (a.binding || []).forEach(function (f) { bound[f] = 1; });
  });
  return s.photos.filter(function (p) { return !bound[p.filename] && !p.excluded; })
    .map(function (p) { return p.filename; });
}

function boundBy(s, filename) {
  var who = [];
  s.audios.forEach(function (a) {
    if (!a.dropped && (a.binding || []).indexOf(filename) !== -1) who.push(shortAudio(a.file));
  });
  return who;
}

/* --------------------------------------------------------------- map row */

function renderMapRow(s) {
  var sec = el('div', { class: 'section' });
  var g = s.gps_override || s.gps;
  var row = el('div', { class: 'maprow' });
  append(row, el('span', { text: '📍' }));
  if (g) {
    append(row, el('span', { class: 'coords' + (s.gps_override ? ' overridden' : ''),
      text: g.lat + ', ' + g.lng }));
  } else {
    append(row, el('span', { class: 'dim', text: 'no pin' }));
  }
  if (s.gps_override) append(row, el('span', { class: 'chip moved', text: 'pin corrected by you' }));
  else if (s.gps_inherited) append(row, el('span', { class: 'chip dmg', text: 'inherited from ' + (s.gps_message_id || '?') }));
  append(row, el('button', { type: 'button', text: '✎ fix pin',
    onclick: function () { state.ui.pin = !state.ui.pin; renderCard(); } }));
  if (s.gps_override) {
    append(row, el('button', { type: 'button', text: 'clear override', onclick: function () {
      s.gps_override = null; saveStop({ gps_override: null }, true); renderCard();
    } }));
  }
  append(sec, row);

  if (state.ui.pin) {
    var input = el('input', { type: 'text', id: 'pin-input', placeholder: 'lat, lng   e.g. 0.0012, 0.0021',
      value: g ? (g.lat + ', ' + g.lng) : '' });
    var msg = el('span', { class: 'hint', text: 'no embedded map — this app makes no network request off this machine' });
    function commit() {
      var m = String(input.value).split(',');
      var lat = parseFloat(m[0]), lng = parseFloat(m[1]);
      if (m.length !== 2 || isNaN(lat) || isNaN(lng) || lat < -90 || lat > 90 || lng < -180 || lng > 180) {
        msg.textContent = 'need "lat, lng" inside (-90..90, -180..180)';
        return;
      }
      s.gps_override = { lat: lat, lng: lng };
      saveStop({ gps_override: s.gps_override }, true);
      state.ui.pin = false;
      renderCard();
    }
    append(sec, el('div', { class: 'inline' }, input,
      el('button', { type: 'button', text: 'set pin', onclick: commit }),
      el('button', { type: 'button', text: 'cancel', onclick: function () { state.ui.pin = false; renderCard(); } }),
      msg));
    state.ui.focus = 'pin-input';
  }
  return sec;
}

/* ----------------------------------------------------------- render: photo */

function renderPhoto(p, i) {
  var s = currentStop();
  var selecting = state.mode === 'rebind';
  var selected = selecting && state.rebind.sel[p.filename];
  var cls = 'tile' + (p.excluded ? ' excluded' : '') + (selecting ? ' selectable' : '') + (selected ? ' selected' : '');
  var tile = el('div', { class: cls, id: 'tile-' + i });

  var wrap = el('div', { class: 'thumbwrap', title: selecting ? 'click to toggle binding' : 'click to enlarge',
    onclick: function () {
      if (selecting) toggleRebindPhoto(i);
      else lightbox(p);
    } });
  var img = el('img', { src: p.url, alt: p.filename, loading: 'lazy', decoding: 'async' });
  img.addEventListener('error', function () {
    clear(wrap);
    append(wrap, el('div', { class: 'broken', text: 'image did not load\n' + p.filename }));
  });
  append(wrap, img);
  append(wrap, el('span', { class: 'num', text: String(i + 1) }));
  var who = boundBy(s, p.filename);
  if (who.length) append(wrap, el('span', { class: 'boundmark', text: '← ' + who.join(', ') }));
  append(tile, wrap);

  /* caption */
  var capText = p.caption_override || p.caption;
  if (state.ui.editCap === p.filename) {
    var ta = el('textarea', { id: 'cap-' + i, rows: 3, placeholder: 'describe what the photo shows — no severity words' });
    ta.value = capText || '';
    ta.addEventListener('input', function () {
      p.caption_override = ta.value.trim() || null;
      saveStop({ photos: photoPatch(s) }, false);
    });
    ta.addEventListener('blur', function () {
      p.caption_override = ta.value.trim() || null;
      saveStop({ photos: photoPatch(s) }, true);
      state.ui.editCap = null;
      refreshTile(i);
    });
    ta.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); ta.blur(); }
      else if (ev.key === 'Escape') { ev.preventDefault(); state.ui.editCap = null; renderCard(); }
    });
    append(tile, el('div', { class: 'editor' }, ta,
      el('div', { class: 'row' }, el('span', { class: 'hint', text: 'Ctrl+Enter or click away to commit · Esc cancels' }))));
    state.ui.focus = 'cap-' + i;
  } else {
    append(tile, el('div', {
      class: 'cap' + (p.caption_override ? ' override' : (capText ? '' : ' none')),
      text: capText || (state.hasPrelabels ? 'no caption — look yourself' : 'no pre-labelling — look yourself')
    }));
  }

  /* chips */
  var chips = el('div', { class: 'chips' });
  if (p.sector_guess) {
    append(chips, el('span', { class: 'chip',
      text: 'S' + p.sector_guess + (p.sector_confidence != null ? ' · ' + p.sector_confidence : '') }));
  }
  if (p.visible_damage) append(chips, el('span', { class: 'chip dmg', text: 'damage visible' }));
  if (p.countable && p.countable.what) append(chips, el('span', { class: 'chip', text: p.countable.n + '× ' + p.countable.what }));
  if (p.move_to) append(chips, el('span', { class: 'chip moved', text: '→ ' + p.move_to }));
  if (p.excluded) append(chips, el('span', { class: 'chip excl', text: 'excluded' + (p.exclude_reason ? ': ' + p.exclude_reason : '') }));
  if (chips.childNodes.length) append(tile, chips);

  append(tile, el('div', { class: 'fname', text: p.filename }));

  /* buttons */
  append(tile, el('div', { class: 'btns' },
    el('button', { type: 'button', text: '✎', title: 'edit caption',
      onclick: function () { state.ui.editCap = p.filename; renderCard(); } }),
    el('button', { type: 'button', text: '→', title: 'move to another stop', disabled: p.excluded,
      onclick: function () { state.ui.moveAsk = (state.ui.moveAsk === p.filename ? null : p.filename); renderCard(); } }),
    el('button', { type: 'button', class: p.excluded ? 'on' : '', text: '✗', title: 'exclude from every report',
      onclick: function () {
        p.excluded = !p.excluded;
        if (p.excluded) { p.move_to = null; state.ui.exclAsk = p.filename; }
        else { p.exclude_reason = null; state.ui.exclAsk = null; }
        savePhotos();
        renderCard();
      } })
  ));

  if (state.ui.moveAsk === p.filename) {
    var sel = el('select', { id: 'move-' + i });
    state.stops.forEach(function (o) {
      if (o.cluster_id === s.cluster_id) return;
      append(sel, el('option', { value: o.cluster_id, text: o.cluster_id + ' · ' + hhmm(o.start_ts) }));
    });
    if (p.move_to) sel.value = p.move_to;
    append(tile, el('div', { class: 'inline' }, sel,
      el('button', { type: 'button', text: 'move', onclick: function () {
        p.move_to = sel.value; p.excluded = false; savePhotos(); state.ui.moveAsk = null; renderCard();
      } }),
      p.move_to ? el('button', { type: 'button', text: 'undo move', onclick: function () {
        p.move_to = null; savePhotos(); state.ui.moveAsk = null; renderCard();
      } }) : null,
      el('button', { type: 'button', text: 'cancel', onclick: function () { state.ui.moveAsk = null; renderCard(); } })));
  }

  if (state.ui.exclAsk === p.filename && p.excluded) {
    var rin = el('input', { type: 'text', id: 'excl-' + i, placeholder: 'why? blurry / duplicate / private property', value: p.exclude_reason || '' });
    rin.addEventListener('input', function () { p.exclude_reason = rin.value.trim() || null; saveStop({ photos: photoPatch(s) }, false); });
    rin.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') { ev.preventDefault(); rin.blur(); } });
    rin.addEventListener('blur', function () {
      p.exclude_reason = rin.value.trim() || null; savePhotos(); state.ui.exclAsk = null; refreshTile(i);
    });
    append(tile, el('div', { class: 'inline' }, rin, el('span', { class: 'hint', text: 'optional' })));
    state.ui.focus = 'excl-' + i;
  }

  return tile;
}

/* A photo another stop is sending here. Read-only: its exclude/move/caption
 * decisions live in the stop that owns it, and writing them from here would put
 * two records of one photo into review.json. Bindable, though — that is the
 * whole point. */
function renderIncoming(inc, i) {
  var s = currentStop();
  var p = inc.photo;
  var selecting = state.mode === 'rebind';
  var selected = selecting && state.rebind.sel[p.filename];
  var tile = el('div', {
    class: 'tile incoming' + (selecting ? ' selectable' : '') + (selected ? ' selected' : ''),
    id: 'tile-' + i
  });

  var wrap = el('div', { class: 'thumbwrap',
    title: selecting ? 'click to toggle binding' : 'click to enlarge',
    onclick: function () { if (selecting) toggleRebindPhoto(i); else lightbox(p); } });
  var img = el('img', { src: '/media/' + p.filename, alt: p.filename, loading: 'lazy', decoding: 'async' });
  img.addEventListener('error', function () {
    clear(wrap);
    append(wrap, el('div', { class: 'broken', text: 'image did not load\n' + p.filename }));
  });
  append(wrap, img);
  append(wrap, el('span', { class: 'num', text: String(i + 1) }));
  var who = boundBy(s, p.filename);
  if (who.length) append(wrap, el('span', { class: 'boundmark', text: '← ' + who.join(', ') }));
  append(tile, wrap);

  append(tile, el('div', { class: 'fromstop', text: '↷ arriving from ' + inc.from }));
  append(tile, el('div', { class: 'cap dim', text: p.caption_override || shortPhoto(p.filename) }));
  return tile;
}

/* ----------------------------------------------------------- render: audio */

function renderAudio(a, i) {
  var s = currentStop();
  var rebinding = state.mode === 'rebind' && state.rebind.audioIdx === i;
  var cls = 'audio' + (i === state.audioIdx ? ' focused' : '') + (a.dropped ? ' dropped' : '') + (rebinding ? ' rebinding' : '');
  var row = el('div', { class: cls, id: 'audio-' + i, onclick: function () { if (state.audioIdx !== i) { state.audioIdx = i; renderCard(); } } });

  var bindNames = (a.binding || []).map(function (f) {
    var k = photoIndex(s, f);
    return k >= 0 ? '#' + (k + 1) : f;
  });
  var bindCls = 'binding' + (bindNames.length ? (a.binding_source === 'corrected' ? ' corrected' : '') : ' unbound');

  append(row, el('div', { class: 'audio-head' },
    el('span', { class: 'name', text: '▸ ' + shortAudio(a.file) }),
    el('span', { class: 'dim', text: hhmm(a.ts) }),
    a.confidence != null ? el('span', { class: 'dim', text: 'conf ' + a.confidence }) : null,
    (a.unclear_spans && a.unclear_spans.length)
      ? el('span', { class: 'chip dmg', text: '⚠ ' + a.unclear_spans.length + ' unclear' }) : null,
    a.proposed_confidence != null ? el('span', { class: 'chip', text: 'proposed ' + a.proposed_confidence }) : null,
    a.dropped ? el('span', { class: 'chip excl', text: 'dropped: ' + (a.drop_reason || '') }) : null,
    el('span', { class: bindCls,
      text: bindNames.length ? ('→ ' + bindNames.join(' ') + ' (' + a.binding_source + ')') : 'not bound to any photo' })
  ));

  // preload="none": dozens of opus files eagerly preloaded stall the page.
  var player = el('audio', { id: 'aud-' + i, src: a.url, preload: 'none', controls: true });
  append(row, player);

  if (state.ui.editTxt === a.file) {
    append(row, renderTextEditor(a, i));
  } else {
    append(row, el('div', { class: 'txt' },
      el('span', { class: 'lbl', text: String(a.language || 'SRC').toUpperCase() }),
      el('div', { class: 'src' + (a.text_override ? ' override' : '') },
        withUnclear(a.text_override || a.text || '— no transcript —'))));
    append(row, el('div', { class: 'txt' },
      el('span', { class: 'lbl', text: 'EN' }),
      el('div', { class: 'en' + (a.en_override ? ' override' : '') },
        withUnclear(a.en_override || a.en_text || '— no English translation —'))));
  }

  if (a.basis) append(row, el('div', { class: 'basis', text: 'basis: ' + a.basis }));

  var proposed = a.proposed_binding || [];
  append(row, el('div', { class: 'btns' },
    el('button', {
      type: 'button', text: '✓ ok', class: isConfirmed(a) ? 'on' : '',
      title: proposed.length
        ? 'accept the proposed binding as-is'
        : 'confirm this note refers to no photo (site-level commentary)',
      onclick: function (ev) { ev.stopPropagation(); confirmAudio(a); }
    }),
    el('button', { type: 'button', text: '✎ edit text', title: 'correct AR / EN (E)',
      onclick: function (ev) { ev.stopPropagation(); state.audioIdx = i; state.ui.editTxt = a.file; renderCard(); } }),
    el('button', { type: 'button', text: '⇄ rebind', class: rebinding ? 'on' : '', title: 'pick the photos this note describes',
      onclick: function (ev) { ev.stopPropagation(); rebinding ? cancelRebind() : enterRebind(i); } }),
    el('button', { type: 'button', class: 'danger', text: a.dropped ? '↩ undrop' : '✗ drop',
      title: 'exclude this transcript — coordination chatter, wrong site',
      onclick: function (ev) {
        ev.stopPropagation();
        if (a.dropped) { a.dropped = false; a.drop_reason = null; saveAudios(); renderCard(); }
        else { state.ui.dropAsk = (state.ui.dropAsk === a.file ? null : a.file); renderCard(); }
      } })
  ));

  if (rebinding) {
    var chosen = s.photos.filter(function (p) { return state.rebind.sel[p.filename]; })
      .map(function (p) { return '#' + (photoIndex(s, p.filename) + 1); });
    append(row, el('div', { class: 'inline rebind' },
      el('span', { class: 'hint', text: 'selection mode — click tiles or press 1–9 · selected: ' + (chosen.join(' ') || 'none') }),
      el('button', { type: 'button', text: 'Enter — commit', onclick: function (ev) { ev.stopPropagation(); commitRebind(); } }),
      el('button', { type: 'button', text: 'Esc — cancel', onclick: function (ev) { ev.stopPropagation(); cancelRebind(); } })));
  }

  if (state.ui.dropAsk === a.file && !a.dropped) {
    var rin = el('input', { type: 'text', id: 'drop-' + i, placeholder: 'reason (required, ≥3 chars) — e.g. coordination chatter' });
    var err = el('span', { class: 'hint' });
    function doDrop() {
      var reason = rin.value.trim();
      if (reason.length < 3) { err.textContent = 'a reason of at least 3 characters is required'; return; }
      a.dropped = true; a.drop_reason = reason; saveAudios(); state.ui.dropAsk = null; renderCard();
    }
    rin.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); doDrop(); }
      if (ev.key === 'Escape') { ev.preventDefault(); state.ui.dropAsk = null; renderCard(); }
    });
    append(row, el('div', { class: 'inline' }, rin,
      el('button', { type: 'button', class: 'danger', text: 'drop transcript', onclick: function (ev) { ev.stopPropagation(); doDrop(); } }),
      el('button', { type: 'button', text: 'cancel', onclick: function (ev) { ev.stopPropagation(); state.ui.dropAsk = null; renderCard(); } }),
      err));
    state.ui.focus = 'drop-' + i;
  }

  return row;
}

function renderTextEditor(a, i) {
  var s = currentStop();
  var arTa = el('textarea', { id: 'src-' + i, class: 'src', rows: 3 });
  var enTa = el('textarea', { id: 'en-' + i, class: 'en', rows: 3 });
  arTa.value = a.text_override || a.text || '';
  enTa.value = a.en_override || a.en_text || '';

  // An "override" equal to the machine text is not an override.
  function stage() {
    var ar = arTa.value.trim(), en = enTa.value.trim();
    a.text_override = (ar && ar !== (a.text || '')) ? ar : null;
    a.en_override = (en && en !== (a.en_text || '')) ? en : null;
  }
  function typed() { stage(); saveStop({ audios: audioPatch(s) }, false); }
  function commit(full) {
    stage();
    saveStop({ audios: audioPatch(s) }, true);
    state.ui.editTxt = null;
    if (full) renderCard(); else refreshAudio(i);
  }

  [arTa, enTa].forEach(function (ta) {
    ta.addEventListener('input', typed);
    ta.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); commit(true); }
      else if (ev.key === 'Escape') { ev.preventDefault(); state.ui.editTxt = null; renderCard(); }
    });
    ta.addEventListener('blur', function () {
      // Commit unless focus moved to the sibling textarea of the same editor.
      setTimeout(function () {
        var act = document.activeElement;
        if (act === arTa || act === enTa) return;
        if (state.ui.editTxt !== a.file) return;
        commit(false);
      }, 0);
    });
  });

  var box = el('div', { class: 'editor' },
    el('div', { class: 'txt' }, el('span', { class: 'lbl', text: String(a.language || 'SRC').toUpperCase() }), arTa),
    el('div', { class: 'txt' }, el('span', { class: 'lbl', text: 'EN' }), enTa),
    el('div', { class: 'row' },
      el('span', { class: 'hint', text: 'Ctrl+Enter or click away commits · Esc discards · autosave 400 ms after the last keystroke' }),
      el('button', { type: 'button', text: 'revert to machine text', onmousedown: function (ev) {
        ev.preventDefault();      // keep focus off the blur/commit path
        a.text_override = null; a.en_override = null;
        saveStop({ audios: audioPatch(s) }, true);
        state.ui.editTxt = null; renderCard();
      } })));
  state.ui.focus = 'en-' + i;
  return box;
}

function isConfirmed(a) {
  var proposed = a.proposed_binding || [];
  if (proposed.length) return a.binding_source === 'accepted' && sameSet(a.binding || [], proposed);
  return a.binding_source === 'manual';
}

function confirmAudio(a) {
  var proposed = a.proposed_binding || [];
  if (proposed.length) {
    a.binding = proposed.slice();
    a.binding_source = 'accepted';
  } else if (a.binding && a.binding.length) {
    a.binding_source = 'manual';
  } else {
    // No prelabel and no photo chosen: the reviewer is saying this note is
    // site-level commentary. Click again to take that decision back.
    a.binding_source = (a.binding_source === 'manual') ? 'none' : 'manual';
  }
  saveAudios();
  renderCard();
}

/* --------------------------------------------------------------- rebinding */

function enterRebind(audioIdx) {
  var s = currentStop();
  var a = s.audios[audioIdx];
  if (!a) return;
  var sel = {};
  (a.binding || []).forEach(function (f) { sel[f] = 1; });
  state.mode = 'rebind';
  state.audioIdx = audioIdx;
  state.rebind = { audioIdx: audioIdx, sel: sel };
  renderCard();
}

function toggleRebindPhoto(photoIdx) {
  if (state.mode !== 'rebind') return;
  var s = currentStop();
  var p = bindablePhotos(s)[photoIdx];
  if (!p) return;
  if (state.rebind.sel[p.filename]) delete state.rebind.sel[p.filename];
  else state.rebind.sel[p.filename] = 1;
  renderCard();
}

function commitRebind() {
  if (state.mode !== 'rebind') return;
  var s = currentStop();
  var a = s.audios[state.rebind.audioIdx];
  var chosen = bindablePhotos(s).filter(function (p) { return state.rebind.sel[p.filename]; })
    .map(function (p) { return p.filename; });
  var proposed = a.proposed_binding || [];

  if (!proposed.length) {
    // Nothing was proposed, so whatever is here the human authored.
    a.binding_source = chosen.length ? 'manual' : 'none';
  } else {
    a.binding_source = sameSet(chosen, proposed) ? 'accepted' : 'corrected';
  }
  a.binding = chosen;

  state.mode = 'browse';
  state.rebind = null;
  saveAudios();
  renderCard();
}

function cancelRebind() {
  state.mode = 'browse';
  state.rebind = null;
  renderCard();
}

/* ---------------------------------------------------------------- actions */

function renderActions(s) {
  var box = el('div', { class: 'actions' });

  append(box, el('button', {
    type: 'button', class: 'primary', id: 'btn-approve',
    text: s.verdict === 'approved' ? '✓ APPROVED' : '✓ APPROVE STOP', title: 'A',
    onclick: function () { approve(); }
  }));

  append(box, el('button', { type: 'button', class: state.ui.split || s.split_at ? 'on' : '', text: '✂ SPLIT',
    onclick: function () { state.ui.split = !state.ui.split; state.ui.merge = false; state.ui.discard = false; renderCard(); } }));

  append(box, el('button', { type: 'button', class: state.ui.merge || s.merge_with ? 'on' : '',
    text: '⇥ MERGE' + (s.merge_with ? ' w/ ' + s.merge_with : ''),
    onclick: function () { state.ui.merge = !state.ui.merge; state.ui.split = false; state.ui.discard = false; renderCard(); } }));

  append(box, el('button', { type: 'button', class: 'danger', text: s.verdict === 'discarded' ? '✗ DISCARDED' : '✗ DISCARD',
    onclick: function () { state.ui.discard = !state.ui.discard; state.ui.split = false; state.ui.merge = false; renderCard(); } }));

  if (s.verdict !== 'pending') {
    append(box, el('button', { type: 'button', text: '↩ reopen', title: 'set this stop back to pending',
      onclick: function () { setVerdict('pending'); } }));
  }

  append(box, el('span', { class: 'spacer' }));
  append(box, el('button', { type: 'button', text: '◀ K prev', onclick: prevStop }));
  append(box, el('button', { type: 'button', text: 'J next ▶', onclick: nextStop }));

  var extra = el('div');

  if (state.ui.split) {
    var bar = el('div', { class: 'inline' }, el('span', { class: 'hint', text: 'split before which message? the chosen id begins the second stop:' }));
    (s.message_ids || []).forEach(function (mid, i) {
      if (i === 0) return;
      append(bar, el('button', { type: 'button', class: s.split_at === mid ? 'on' : '', text: mid,
        onclick: function () { s.split_at = mid; s.merge_with = null; saveStop({ split_at: mid, merge_with: null }, true); state.ui.split = false; renderCard(); } }));
    });
    if ((s.message_ids || []).length < 2) append(bar, el('span', { class: 'hint', text: 'only one message in this stop — nothing to split' }));
    if (s.split_at) append(bar, el('button', { type: 'button', text: 'clear split',
      onclick: function () { s.split_at = null; saveStop({ split_at: null }, true); state.ui.split = false; renderCard(); } }));
    append(extra, bar);
  }

  if (state.ui.merge) {
    var msel = el('select', { id: 'merge-sel' });
    state.stops.forEach(function (o) {
      if (o.cluster_id === s.cluster_id) return;
      append(msel, el('option', { value: o.cluster_id, text: o.cluster_id + ' · ' + hhmm(o.start_ts) }));
    });
    var nextId = state.stops[state.idx + 1] ? state.stops[state.idx + 1].cluster_id : null;
    if (s.merge_with) msel.value = s.merge_with; else if (nextId) msel.value = nextId;
    append(extra, el('div', { class: 'inline' },
      el('span', { class: 'hint', text: 'merge this stop into:' }), msel,
      el('button', { type: 'button', text: 'merge', onclick: function () {
        s.merge_with = msel.value; s.split_at = null;
        saveStop({ merge_with: msel.value, split_at: null }, true); state.ui.merge = false; renderCard();
      } }),
      s.merge_with ? el('button', { type: 'button', text: 'clear merge', onclick: function () {
        s.merge_with = null; saveStop({ merge_with: null }, true); state.ui.merge = false; renderCard();
      } }) : null));
  }

  if (state.ui.discard) {
    var din = el('input', { type: 'text', id: 'discard-reason', value: s.human_note || '',
      placeholder: 'why is this whole stop not a site? (required, ≥3 chars)' });
    var derr = el('span', { class: 'hint' });
    function doDiscard() {
      var reason = din.value.trim();
      if (reason.length < 3) { derr.textContent = 'the schema refuses a discard without a reason'; return; }
      s.human_note = reason;
      s.verdict = 'discarded';
      saveStop({ verdict: 'discarded', human_note: reason }, true);
      state.ui.discard = false;
      renderRail(); renderCard();
      loadOrphans();
      jumpToNextPending();
    }
    din.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); doDiscard(); }
      if (ev.key === 'Escape') { ev.preventDefault(); state.ui.discard = false; renderCard(); }
    });
    append(extra, el('div', { class: 'inline' }, din,
      el('button', { type: 'button', class: 'danger', text: 'discard this stop', onclick: doDiscard }),
      el('button', { type: 'button', text: 'cancel', onclick: function () { state.ui.discard = false; renderCard(); } }), derr));
    state.ui.focus = 'discard-reason';
  }

  var wrap = el('div');
  append(wrap, box);
  if (extra.childNodes.length) append(wrap, el('div', { class: 'section' }, extra));
  return wrap;
}

function approve() { setVerdict('approved'); }

function setVerdict(verdict) {
  var s = currentStop();
  if (!s) return;
  if (verdict === 'discarded') { state.ui.discard = true; renderCard(); return; }
  s.verdict = verdict;
  // Verdicts POST immediately — this is the click the reviewer trusts.
  saveStop({ verdict: verdict }, true);
  renderRail();
  renderCard();
  if (verdict === 'approved') jumpToNextPending();
}

/* ------------------------------------------------------------------- tray */

function renderTray() {
  var list = document.getElementById('orphan-list');
  var count = document.getElementById('orphan-count');
  clear(list);
  count.textContent = '(' + state.orphans.length + ')';
  if (!state.orphans.length) {
    append(list, el('p', { class: 'dim', text: 'none — every photo belongs to a stop you have not discarded.' }));
    return;
  }
  state.orphans.forEach(function (o) {
    var row = el('div', { class: 'orphan' });
    var img = el('img', { src: o.url, alt: o.filename, loading: 'lazy' });
    img.addEventListener('click', function () { lightbox(o); });
    append(row, img);
    append(row, el('div', { class: 'fname', text: o.filename }));
    append(row, el('div', { class: 'why', text: o.reason }));
    if (o.from_stop) {
      var sel = el('select');
      state.stops.forEach(function (s) {
        if (s.cluster_id === o.from_stop) return;
        append(sel, el('option', { value: s.cluster_id, text: s.cluster_id + ' · ' + hhmm(s.start_ts) }));
      });
      append(row, el('div', { class: 'row' }, sel,
        el('button', { type: 'button', text: '→', title: 'move this photo into that stop', onclick: function () {
          moveOrphan(o, sel.value);
        } })));
    } else {
      // review.json records photo decisions only inside the stop that owns the
      // photo. A photo in no stop has nowhere to be recorded, so offering the
      // move would silently drop the reviewer's instruction.
      append(row, el('div', { class: 'blocked', text: 'in no stop — s5c can only move a photo that belongs to one. Re-run s5 or add it upstream.' }));
    }
    append(list, row);
  });
}

function moveOrphan(orphan, targetId) {
  var src = null, i;
  for (i = 0; i < state.stops.length; i++) if (state.stops[i].cluster_id === orphan.from_stop) src = state.stops[i];
  if (!src) return;
  var p = null;
  for (i = 0; i < src.photos.length; i++) if (src.photos[i].filename === orphan.filename) p = src.photos[i];
  if (!p) return;
  p.move_to = targetId;
  p.excluded = false;
  saveStopFor(src.cluster_id, { photos: photoPatch(src) }, true);
  orphan.reason = orphan.reason + ' → moved to ' + targetId;
  renderTray();
  if (src === currentStop()) renderCard();
}

/* --------------------------------------------------------------- overlays */

function overlay(node) {
  var o = document.getElementById('overlay');
  clear(o);
  o.hidden = false;
  o.onclick = function (ev) { if (ev.target === o) closeOverlay(); };
  append(o, node);
}

function closeOverlay() {
  var o = document.getElementById('overlay');
  o.hidden = true;
  clear(o);
}

function lightbox(p) {
  overlay(el('div', { class: 'lightbox' },
    el('img', { src: p.url, alt: p.filename }),
    el('div', { class: 'cap', text: p.filename + '  ·  click anywhere or press Esc to close' })));
}

function showHelp() {
  var rows = [
    ['A', 'approve the current stop and jump to the next pending one'],
    ['J / K', 'next / previous stop'],
    ['1 – 9', 'toggle that photo while binding (⇄ selection mode)'],
    ['Enter', 'commit the binding'],
    ['Esc', 'cancel the binding / close this panel'],
    ['E', 'edit the focused voice note’s text'],
    ['Space', 'play / pause the focused voice note'],
    ['↑ / ↓', 'move the focus between voice notes'],
    ['?', 'this list']
  ];
  var notes = [
    'The rail shows To do by default: stops still pending, plus anything s5c would refuse. Switch to All to see finished stops again — nothing is deleted, only hidden.',
    'A ⚠ chip is a conflict, listed in red at the top of the card. FINISH reports the same list.',
    'A photo another stop is sending here appears in the grid marked “arriving from”. Bind voice notes to it now — s5c moves it in before the bindings matter.'
  ];
  var table = el('table');
  rows.forEach(function (r) {
    append(table, el('tr', null, el('td', null, el('kbd', { text: r[0] })), el('td', { text: r[1] })));
  });
  var sheet = el('div', { class: 'sheet' }, el('h2', { text: 'Keyboard' }), table,
    el('p', { class: 'dim', text: 'Shortcuts are ignored while you are typing in a text box, so a note can contain the letter A.' }),
    el('h2', { text: 'The rail' }));
  notes.forEach(function (n) { append(sheet, el('p', { class: 'dim', text: n })); });
  append(sheet, el('button', { type: 'button', text: 'close', onclick: closeOverlay }));
  overlay(sheet);
}

function finish() {
  fetch('/api/finish', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      var sheet = el('div', { class: 'sheet' }, el('h2', { text: d.ok ? 'Gate clear — every stop is reviewed' : 'Not finished yet' }));
      append(sheet, el('p', { text: d.pending + ' stop(s) still pending.' }));
      if (d.problems && d.problems.length) {
        append(sheet, el('p', { text: d.problems.length + ' problem(s) s5c would refuse or complain about:' }));
        var ul = el('ul');
        d.problems.slice(0, 200).forEach(function (p) { append(ul, el('li', { class: 'problem', text: p })); });
        append(sheet, ul);
      }
      append(sheet, el('p', { class: 'dim', text: d.ok ? 'Next: python -m field_report.stages.s5c_apply_review --config <config>' : 'Fix these, then press FINISH again.' }));
      append(sheet, el('button', { type: 'button', text: 'close', onclick: closeOverlay }));
      overlay(sheet);
    })
    .catch(function (e) { overlay(el('div', { class: 'sheet' }, el('h2', { text: 'finish check failed' }), el('p', { text: String(e) }),
      el('button', { type: 'button', text: 'close', onclick: closeOverlay }))); });
}

/* -------------------------------------------------------------- shortcuts */

function bindKeys() {
  document.addEventListener('keydown', function (ev) {
    var t = ev.target;
    var tag = t && t.tagName ? t.tagName.toLowerCase() : '';
    // Typing a note must never approve a stop.
    if (tag === 'textarea' || tag === 'input' || tag === 'select' || (t && t.isContentEditable)) return;
    if (ev.ctrlKey || ev.altKey || ev.metaKey) return;

    var overlayOpen = !document.getElementById('overlay').hidden;
    if (ev.key === 'Escape') {
      if (overlayOpen) { closeOverlay(); return; }
      if (state.mode === 'rebind') { ev.preventDefault(); cancelRebind(); }
      return;
    }
    if (overlayOpen) return;

    if (ev.key === '?') { ev.preventDefault(); showHelp(); return; }

    if (state.mode === 'rebind') {
      if (ev.key >= '1' && ev.key <= '9') { ev.preventDefault(); toggleRebindPhoto(parseInt(ev.key, 10) - 1); return; }
      if (ev.key === 'Enter') { ev.preventDefault(); commitRebind(); return; }
    }

    var s = currentStop();
    switch (ev.key) {
      case 'a': case 'A':
        ev.preventDefault(); approve(); break;
      case 'j': case 'J':
        ev.preventDefault(); nextStop(); break;
      case 'k': case 'K':
        ev.preventDefault(); prevStop(); break;
      case 'e': case 'E':
        ev.preventDefault();
        if (s && s.audios.length) { state.ui.editTxt = s.audios[state.audioIdx].file; renderCard(); }
        else { var n = document.getElementById('note'); if (n) n.focus(); }
        break;
      case ' ':
        // If the player itself has focus its own Space handling already
        // toggles playback; doing it here too would toggle twice.
        if (tag === 'audio') return;
        ev.preventDefault();
        var au = document.getElementById('aud-' + state.audioIdx);
        if (au) { if (au.paused) au.play(); else au.pause(); }
        break;
      case 'ArrowDown':
        if (s && s.audios.length) { ev.preventDefault(); state.audioIdx = Math.min(state.audioIdx + 1, s.audios.length - 1); renderCard(); }
        break;
      case 'ArrowUp':
        if (s && s.audios.length) { ev.preventDefault(); state.audioIdx = Math.max(state.audioIdx - 1, 0); renderCard(); }
        break;
      default: break;
    }
  });
}

/* ------------------------------------------------------------------- boot */

function boot() {
  document.getElementById('btn-help').addEventListener('click', showHelp);
  document.getElementById('btn-finish').addEventListener('click', finish);
  bindKeys();
  load();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
