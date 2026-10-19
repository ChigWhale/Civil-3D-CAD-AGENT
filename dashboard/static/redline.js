/* Redline review page: one review's sheets, comments, outcomes and labels; metrics over runs.
   Depends on redline-charts.js (window.RedlineCharts) and redline-dnd.js (window.RedlineDnd).
   Labelling never shows or asks for a parameter: labels are tool names only. */
(function () {
  'use strict';

  var API = 'redline';
  var EMPTY_CHART = 'Label a few comments in the Run tab to fill this in.';
  var STATUS_ORDER = ['done', 'dry_run', 'needs_human', 'paused', 'failed', 'not_located', 'skipped'];
  var STEP_ORDER = ['ran', 'dry', 'failed', 'blocked', 'not_filled', 'planned'];
  var DEFAULT_BINS = ['lookup', 'draw', 'modify', 'text', 'layer', 'block', 'sheet', 'civil'];
  var DEFAULT_TIERS = ['read', 'draw', 'modify', 'delete', 'design'];
  var GLYPH = { ran: '\u2713', dry: '\u25cc', failed: '\u2715', blocked: '\u2298', not_filled: '?', planned: '\u00b7' };
  var VERDICTS = [['good', 'Right \u2713'], ['bad', 'Wrong \u2715'], ['unsure', 'Can\u2019t tell ?']];
  var VGLYPH = { good: '\u2713', bad: '\u2715', unsure: '?' };
  var C = window.RedlineCharts;
  var D = window.RedlineDnd;

  var S = {
    reviews: null, reviewDir: '', id: null, payload: null, ix: null,
    selected: null, page: null, zoom: 'fit', overlay: false, sheetEl: null,
    tab: 'run', scope: 'run', aggregate: null, aggregateError: null,
    labeller: '', recent: [], libBin: 'recent', libOpen: true, libSearch: '',
    saveTimers: {}, lastBodies: {}, charts: [], dnd: null
  };

  /* ---- helpers ---------------------------------------------------------- */

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function arr(v) { return Array.isArray(v) ? v : []; }
  function obj(v) { return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; }
  function sid(v) { return v === null || v === undefined ? '' : String(v); }
  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
  function basename(p) { var s = sid(p); return s ? s.split(/[\\/]/).pop() : ''; }
  function pct(v) { return isNum(v) ? Math.round(v * 100) + '%' : '\u2014'; }
  function fix(v, d) { return isNum(v) ? v.toFixed(d) : '\u2014'; }

  function fmtRate(r) {
    r = obj(r);
    if (!r.n) return '\u2014';
    return pct(r.rate) + ' (' + pct(r.low) + '\u2013' + pct(r.high) + ')';
  }

  function fmtTime(epoch) {
    if (!isNum(epoch)) return '';
    var d = new Date(epoch * 1000);
    var hh = String(d.getHours()), mm = String(d.getMinutes());
    return (hh.length < 2 ? '0' + hh : hh) + ':' + (mm.length < 2 ? '0' + mm : mm);
  }

  function fmtDate(epoch) {
    if (!isNum(epoch)) return '';
    return new Date(epoch * 1000).toLocaleString();
  }

  function h(tag, attrs) {
    var e = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === 'class') e.className = v;
        else if (k === 'text') e.textContent = String(v);
        else if (k === 'style') e.style.cssText = v;
        else if (k === 'dataset') Object.keys(v).forEach(function (d) { e.dataset[d] = v[d]; });
        else if (k.indexOf('on') === 0 && typeof v === 'function') e.addEventListener(k.slice(2), v);
        else if (k === 'hidden') e.hidden = !!v;
        else if (k === 'disabled') e.disabled = !!v;
        else if (k === 'checked') e.checked = !!v;
        else if (k === 'value') e.value = v;
        else e.setAttribute(k, String(v));
      });
    }
    for (var i = 2; i < arguments.length; i++) append(e, arguments[i]);
    return e;
  }

  function append(parent, kid) {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach(function (k) { append(parent, k); }); return; }
    if (kid instanceof Node) { parent.appendChild(kid); return; }
    parent.appendChild(document.createTextNode(String(kid)));
  }

  function lsGet(k, d) {
    try { var v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; }
  }
  function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* storage blocked */ } }

  function debounce(fn, ms) {
    var t = null;
    return function () { clearTimeout(t); t = setTimeout(fn, ms); };
  }

  function truncate(s, n) { s = sid(s); return s.length > n ? s.slice(0, n - 1) + '\u2026' : s; }

  function jsonShort(v, n) {
    var s;
    try { s = JSON.stringify(v); } catch (e) { s = String(v); }
    if (s === undefined) s = '';
    return { short: truncate(s, n), full: s };
  }

  /* ---- network ---------------------------------------------------------- */

  async function request(url, options) {
    var r;
    try { r = await fetch(url, Object.assign({ cache: 'no-store' }, options || {})); }
    catch (e) { return { ok: false, status: 0, data: null, network: true, url: url, error: String(e && e.message || e) }; }
    var data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    return { ok: r.ok, status: r.status, data: data, url: url };
  }

  function reviewPath(id) {
    return API + '/reviews/' + sid(id).split('/').map(encodeURIComponent).join('/');
  }

  /* ---- banners and messages -------------------------------------------- */

  function banner(text, kind) {
    var b = $('#banner');
    clear(b);
    if (!text) { b.hidden = true; return; }
    b.className = 'banner ' + (kind || 'err');
    append(b, [h('span', { text: text }), h('button', { type: 'button', class: 'x', text: '\u00d7', title: 'dismiss', onclick: function () { b.hidden = true; } })]);
    b.hidden = false;
  }

  function showMessage(text) {
    var m = $('#message');
    clear(m);
    append(m, h('p', { text: text }));
    m.hidden = false;
    $$('.view').forEach(function (v) { v.hidden = true; });
  }

  function hideMessage() { $('#message').hidden = true; }

  /* ---- indexing the payload ------------------------------------------- */

  function indexPayload(p) {
    var report = obj(p.report);
    var comments = arr(report.comments).map(function (c, i) { return { c: obj(c), i: i }; });
    comments.sort(function (a, b) {
      var pa = Number(a.c.page_no) || 0, pb = Number(b.c.page_no) || 0;
      return pa !== pb ? pa - pb : a.i - b.i;
    });
    var ix = { report: report, comments: [], byId: {}, num: {}, order: [], actionsBy: {}, locatedBy: {}, seqBy: {}, sheetsBy: {}, unmatchedBy: {}, pages: [], labelsIn: {} };
    comments.forEach(function (w, n) {
      var id = sid(w.c.id);
      ix.comments.push(w.c);
      ix.byId[id] = w.c;
      ix.num[id] = n + 1;
      ix.order.push(id);
    });
    arr(report.actions).forEach(function (a) {
      a = obj(a);
      var id = sid(a.comment_id);
      (ix.actionsBy[id] = ix.actionsBy[id] || []).push(a);
    });
    arr(report.located).forEach(function (l) { l = obj(l); ix.locatedBy[sid(l.key)] = l; });
    arr(report.sequences).forEach(function (s) { s = obj(s); if (!ix.seqBy[sid(s.comment_id)]) ix.seqBy[sid(s.comment_id)] = s; });
    arr(report.sheets).forEach(function (s) { s = obj(s); ix.sheetsBy[Number(s.page_no)] = s; });
    arr(report.unmatched_pages).forEach(function (u) { u = obj(u); ix.unmatchedBy[Number(u.page_no)] = sid(u.reason) || 'no match'; });
    var pageSet = {};
    Object.keys(ix.sheetsBy).forEach(function (k) { pageSet[k] = true; });
    Object.keys(obj(obj(p.images).pages)).forEach(function (k) { pageSet[k] = true; });
    ix.comments.forEach(function (c) { pageSet[String(Number(c.page_no) || 0)] = true; });
    ix.pages = Object.keys(pageSet).map(Number).filter(function (n) { return isFinite(n); }).sort(function (a, b) { return a - b; });
    return ix;
  }

  function tools() { return obj(S.payload && S.payload.tools); }
  function binOf(name) { return sid(obj(tools().bins)[name]) || 'other'; }
  function riskOf(name) { return sid(obj(tools().risks)[name]) || 'other'; }
  function isOcr() { return !!S.payload && S.payload.kind === 'ocr'; }

  function modelChain(id) {
    var seq = S.ix.seqBy[id];
    if (seq && Array.isArray(seq.tools)) return seq.tools.map(sid);
    var acts = S.ix.actionsBy[id];
    if (acts && acts.length) return arr(acts[0].steps).map(function (s) { return sid(obj(s).tool); });
    return [];
  }

  function primaryAction(id) { var a = S.ix.actionsBy[id]; return a && a.length ? a[0] : null; }

  function locatedFor(id) {
    var l = S.ix.locatedBy[id] || S.ix.locatedBy[id + '#1'];
    return l || null;
  }

  function statusClass(id) {
    var a = primaryAction(id);
    return a && a.status ? 'st-' + sid(a.status) : 'muted';
  }

  function labelRecords(id) { return obj(obj(S.payload && S.payload.labels)[id]); }

  function latestLabel(id) {
    var recs = labelRecords(id), best = null;
    Object.keys(recs).forEach(function (who) {
      var r = obj(recs[who]);
      if (!best || (Number(r.updated) || 0) > (Number(best.updated) || 0)) best = Object.assign({ labeller: who }, r);
    });
    return best;
  }

  function myLabel(id) {
    var recs = labelRecords(id);
    return S.labeller && recs[S.labeller] ? Object.assign({ labeller: S.labeller }, obj(recs[S.labeller])) : null;
  }

  function shownLabel(id) { return myLabel(id) || latestLabel(id); }

  function trayChainFor(id) {
    var lab = shownLabel(id);
    if (lab && Array.isArray(lab.tools)) return lab.tools.map(sid);
    var any = latestLabel(id);
    if (any && Array.isArray(any.tools)) return any.tools.map(sid);
    return modelChain(id);
  }

  function sameChain(a, b) {
    if (a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }

  /* ---- boot ------------------------------------------------------------ */

  async function boot() {
    S.labeller = sid(lsGet('redline.labeller', '')) || '';
    S.recent = arr(lsGet('redline.recent', [])).map(sid);
    $('#labeller').value = S.labeller;
    wireHeader();
    wireTabs();
    wireKeys();
    S.dnd = D.wireDnd($('#cardsPane'), { onChange: onTrayChange, onAdd: rememberRecent, targetTray: selectedTray });
    window.addEventListener('resize', debounce(function () {
      if (S.tab === 'metrics') S.charts.forEach(function (d) { d(); });
    }, 200));
    var q = new URLSearchParams(location.search);
    S.id = q.get('review');
    await loadReviews();
    if (!S.id && S.reviews && S.reviews.length) { S.id = S.reviews[0].id; setUrl(); }
    if (S.id) await loadReview(S.id);
    else showMessage('No reviews under ' + (S.reviewDir || 'the review folder') + ' yet.');
  }

  function setUrl() {
    var u = new URL(location.href);
    if (S.id) u.searchParams.set('review', S.id); else u.searchParams.delete('review');
    history.replaceState(null, '', u.toString());
  }

  async function loadReviews() {
    var res = await request(API + '/reviews');
    if (res.network) { banner('Could not reach ' + new URL(res.url, location.href).href + ': ' + res.error); return; }
    if (res.status === 404) {
      banner('This dashboard is an older version without the review page. Run: python launcher.py stop && python launcher.py start --no-browser');
      return;
    }
    if (!res.ok) { banner('GET ' + res.url + ' failed with ' + res.status); return; }
    var data = obj(res.data);
    S.reviews = arr(data.reviews).map(obj);
    S.reviewDir = sid(data.review_dir);
    if (!S.labeller && data.whoami) { S.labeller = sid(data.whoami); $('#labeller').value = S.labeller; }
    fillSelect();
  }

  function modeLabel(mode, kind) {
    if (kind === 'ocr') return 'transcribe';
    if (mode === 'dry') return 'dry run';
    if (mode === 'run') return 'run';
    return 'text-only';
  }

  function fillSelect() {
    var sel = $('#reviewSel');
    clear(sel);
    arr(S.reviews).forEach(function (r) {
      var n = Number(r.comments) || 0;
      var label = sid(r.id) + ' \u00b7 ' + (arr(r.pdfs).map(basename).join(', ') || 'typed') + ' \u00b7 ' + modeLabel(r.mode, r.kind) + ' \u00b7 ' + n + ' comment' + (n === 1 ? '' : 's');
      sel.appendChild(h('option', { value: sid(r.id), text: label }));
    });
    if (S.id && !arr(S.reviews).some(function (r) { return sid(r.id) === S.id; })) {
      sel.appendChild(h('option', { value: S.id, text: S.id }));
    }
    sel.value = S.id || '';
  }

  async function loadReview(id) {
    S.id = id;
    $('#reviewSel').value = id;
    var res = await request(reviewPath(id));
    if (res.network) { banner('Could not reach ' + new URL(res.url, location.href).href + ': ' + res.error); showMessage('Could not load review ' + id); return; }
    if (res.status === 404) {
      var err = sid(obj(res.data).error);
      var m = /under (.+)$/.exec(err);
      showMessage('No review ' + id + ' under ' + (m ? m[1] : (S.reviewDir || 'the review folder')));
      S.payload = null;
      return;
    }
    if (!res.ok) { showMessage('GET ' + res.url + ' failed with ' + res.status); return; }
    S.payload = obj(res.data);
    S.ix = indexPayload(S.payload);
    S.aggregate = null;
    S.charts = [];
    if (!S.labeller && S.payload.whoami) { S.labeller = sid(S.payload.whoami); $('#labeller').value = S.labeller; }
    S.selected = S.ix.order[0] || null;
    var first = S.selected ? Number(S.ix.byId[S.selected].page_no) || 0 : (S.ix.pages[0] || 0);
    S.page = S.ix.pages.indexOf(first) >= 0 ? first : (S.ix.pages[0] !== undefined ? S.ix.pages[0] : 0);
    S.zoom = 'fit';
    if (S.tab === 'ocr' && !isOcr()) S.tab = 'run';
    renderAll();
  }

  function renderAll() {
    hideMessage();
    banner('');
    renderHeader();
    renderTiles();
    renderIssues();
    renderLibrary();
    renderCards();
    $('#tabs .tab[data-tab="ocr"]').hidden = !isOcr();
    showTab(S.tab);
  }

  /* ---- header ----------------------------------------------------------- */

  function wireHeader() {
    $('#reviewSel').addEventListener('change', function (ev) {
      S.id = ev.target.value;
      setUrl();
      loadReview(S.id);
    });
    var inp = $('#labeller');
    inp.addEventListener('change', function () {
      S.labeller = inp.value.trim();
      lsSet('redline.labeller', S.labeller);
      if (S.payload) renderCards();
    });
    $('#discardBtn').addEventListener('click', discardLabels);
    $('#copyBtn').addEventListener('click', function () {
      var folder = sid(S.payload && (S.payload.folder || S.payload.review_dir));
      if (!folder) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(folder).then(function () { flashButton($('#copyBtn'), 'Copied'); }, function () { flashButton($('#copyBtn'), 'Copy failed'); });
      } else { window.prompt('Folder path', folder); }
    });
  }

  function flashButton(btn, text) {
    var old = btn.textContent;
    btn.textContent = text;
    setTimeout(function () { btn.textContent = old; }, 1200);
  }

  function chip(text, cls, title) {
    return h('span', { class: 'chip ' + (cls || ''), text: text, title: title || null });
  }

  function reviewEntry() {
    return arr(S.reviews).filter(function (r) { return sid(r.id) === S.id; })[0] || null;
  }

  function renderHeader() {
    var p = S.payload, rep = S.ix.report, chips = $('#chips');
    clear(chips);
    var entry = reviewEntry();
    var source = entry && entry.source ? sid(entry.source) : (isOcr() ? 'transcription' : (obj(rep.settings).source === 'markups' ? 'markups' : (arr(rep.pdfs).length ? 'handwriting' : 'typed')));
    append(chips, chip(source, 'c-source', 'source'));
    append(chips, chip(modeLabel(rep.mode, p.kind), 'c-mode', 'mode'));
    var seq0 = arr(rep.sequences)[0];
    if (seq0 && seq0.model) append(chips, chip(sid(seq0.model), 'c-model', 'model'));
    var vocab = obj(rep.vocabulary);
    if (vocab.count !== undefined) append(chips, chip(vocab.count + ' tools', 'c-vocab', 'vocabulary ' + sid(vocab.version)));
    if (isNum(rep.created)) append(chips, chip(fmtDate(rep.created), 'c-date', 'created'));
    renderLabelledCount();
    $('#copyBtn').title = sid(p.folder || p.review_dir) || 'folder';
  }

  function renderLabelledCount() {
    var c = obj(S.payload && S.payload.counts);
    var labelled = isNum(c.labelled) ? c.labelled : countLabelled();
    var total = isNum(c.total) ? c.total : S.ix.order.length;
    $('#labelledCount').textContent = labelled + ' / ' + total + ' labelled';
  }

  function countLabelled() {
    var labels = obj(S.payload && S.payload.labels), n = 0;
    Object.keys(labels).forEach(function (id) {
      var recs = obj(labels[id]);
      if (Object.keys(recs).some(function (w) { return obj(recs[w]).verdict; })) n++;
    });
    return n;
  }

  async function discardLabels() {
    if (!S.payload) return;
    if (!window.confirm('Discard every label of review ' + S.id + '? This cannot be undone.')) return;
    var res = await request(reviewPath(S.id) + '/labels', { method: 'DELETE' });
    if (!res.ok) { banner('Discard failed: ' + (obj(res.data).error || res.status || res.error)); return; }
    applyLabelResponse(res.data);
    renderCards();
  }

  /* ---- tabs ------------------------------------------------------------- */

  function wireTabs() {
    $$('#tabs .tab').forEach(function (b) {
      b.addEventListener('click', function () { showTab(b.dataset.tab); });
    });
  }

  function showTab(tab) {
    if (tab === 'ocr' && !isOcr()) tab = 'run';
    S.tab = tab;
    $$('#tabs .tab').forEach(function (b) { b.classList.toggle('on', b.dataset.tab === tab); });
    $('#view-run').hidden = tab !== 'run';
    $('#view-metrics').hidden = tab !== 'metrics';
    $('#view-ocr').hidden = tab !== 'ocr';
    if (!S.payload) return;
    if (tab === 'run') renderSheet($('#sheetPane'), false);
    if (tab === 'ocr') { renderSheet($('#ocrSheet'), true); renderOcr(); }
    if (tab === 'metrics') renderMetrics();
  }

  /* ---- tiles and issues ----------------------------------------------- */

  function tile(label, value, title) {
    return h('div', { class: 'tile', title: title || null }, h('div', { class: 'tv', text: value }), h('div', { class: 'tl', text: label }));
  }

  function countActions(statuses) {
    var n = 0;
    arr(S.ix.report.actions).forEach(function (a) { if (statuses.indexOf(sid(obj(a).status)) >= 0) n++; });
    return n;
  }

  function renderTiles() {
    var rep = S.ix.report, m = obj(S.payload.metrics), mn = obj(m.n), counts = obj(rep.counts), t = $('#tiles');
    clear(t);
    var located = isNum(mn.located) ? mn.located : arr(rep.located).filter(function (l) { return obj(l).space && obj(l).space !== 'none'; }).length;
    var acted = isNum(mn.acted) ? mn.acted : (isNum(counts.acted) ? counts.acted : arr(rep.actions).length);
    append(t, [
      tile('Comments', isNum(counts.comments) ? counts.comments : S.ix.order.length),
      tile('Sequenced', isNum(counts.sequenced) ? counts.sequenced : arr(rep.sequences).length),
      tile('Located', located),
      tile('Acted', acted),
      tile('Done / dry', countActions(['done', 'dry_run'])),
      tile('Needs a person', countActions(['needs_human'])),
      tile('Paused / failed', countActions(['paused', 'failed'])),
      tile('Labelled', (isNum(obj(S.payload.counts).labelled) ? S.payload.counts.labelled : countLabelled()) + ' / ' + S.ix.order.length),
      tile('Coverage', fmtRate(m.coverage), 'attempted comments over all comments, Wilson 95%'),
      tile('Precision', fmtRate(m.precision), 'labelled right over labelled right or wrong'),
      tile('Mean doability', fix(obj(m.doability).mean, 2), 'mean doability score of the first action per comment')
    ]);
  }

  function issueList() {
    var rep = S.ix.report, items = [];
    arr(rep.errors).forEach(function (e) { items.push({ kind: 'error', text: sid(e) }); });
    arr(rep.unmatched_pages).forEach(function (u) { u = obj(u); items.push({ kind: 'unmatched', text: 'Page ' + sid(u.page_no) + ' not matched: ' + sid(u.reason)}); });
    arr(rep.recall_warnings).forEach(function (w) { w = obj(w); items.push({ kind: 'warning', text: 'Page ' + sid(w.page_no) + ' ' + sid(w.kind) + ': ' + sid(w.detail) }); });
    arr(rep.sequences).forEach(function (s) { s = obj(s); if (s.error) items.push({ kind: 'model', text: 'Comment ' + sid(s.comment_id) + ': ' + sid(s.error) }); });
    return items;
  }

  function renderIssues() {
    var strip = $('#issues'), drawer = $('#drawer'), items = issueList();
    clear(strip); clear(drawer);
    drawer.hidden = true;
    if (!items.length) { append(strip, h('span', { class: 'ok', text: 'No issues' })); return; }
    var counts = {};
    items.forEach(function (i) { counts[i.kind] = (counts[i.kind] || 0) + 1; });
    var parts = [];
    if (counts.error) parts.push(counts.error + ' error' + (counts.error > 1 ? 's' : ''));
    if (counts.unmatched) parts.push(counts.unmatched + ' unmatched page' + (counts.unmatched > 1 ? 's' : ''));
    if (counts.warning) parts.push(counts.warning + ' recall warning' + (counts.warning > 1 ? 's' : ''));
    if (counts.model) parts.push(counts.model + ' model error' + (counts.model > 1 ? 's' : ''));
    var btn = h('button', { type: 'button', class: 'issues-btn', text: parts.join(' \u00b7 ') + ' \u25be', onclick: function () { drawer.hidden = !drawer.hidden; } });
    append(strip, btn);
    append(drawer, h('ul', null, items.map(function (i) { return h('li', { class: 'is-' + i.kind }, h('span', { class: 'ik', text: i.kind }), ' ', i.text); })));
  }

  /* ---- sheet pane -------------------------------------------------------- */

  function pageInfo(n) {
    var pg = obj(obj(obj(S.payload.images).pages)[String(n)]);
    return { url: sid(pg.url), W: Number(pg.width_px) || 0, H: Number(pg.height_px) || 0 };
  }

  function regionUrl(n) { return sid(obj(obj(S.payload.images).regions)[String(n)]); }

  function pageTabLabel(n) {
    if (n === 0) return 'Typed comments';
    var sh = S.ix.sheetsBy[n], um = S.ix.unmatchedBy[n], tb = obj(sh && sh.title_block);
    var s = 'Page ' + n;
    if (tb.sheet_no) s += ' \u00b7 sheet ' + sid(tb.sheet_no);
    if (sh && sh.dwg_path) s += ' \u00b7 ' + basename(sh.dwg_path) + (sh.layout_name ? ' / ' + sid(sh.layout_name) : '');
    else if (um) s += ' \u00b7 not matched: ' + um;
    else if (sh && sh.reason) s += ' \u00b7 not matched: ' + sid(sh.reason);
    return s;
  }

  function commentsOnPage(n) {
    return S.ix.comments.filter(function (c) { return (Number(c.page_no) || 0) === n; });
  }

  function bbox(c) {
    var b = arr(c && c.bbox_px);
    if (b.length < 4 || !b.every(isNum)) return null;
    return { x0: Math.min(b[0], b[2]), y0: Math.min(b[1], b[3]), x1: Math.max(b[0], b[2]), y1: Math.max(b[1], b[3]) };
  }

  function applyZoom(sheet, pg) {
    if (!sheet) return;
    sheet.style.width = (S.zoom === 'fit' || !pg.W) ? '100%' : Math.round(pg.W * S.zoom) + 'px';
  }

  function renderSheet(pane, withRegions) {
    clear(pane);
    var n = S.page, pg = pageInfo(n);
    var tabs = h('div', { class: 'page-tabs' }, S.ix.pages.map(function (p) {
      return h('button', { type: 'button', class: 'ptab' + (p === S.page ? ' on' : ''), text: pageTabLabel(p), onclick: function () { S.page = p; renderSheet(pane, withRegions); } });
    }));
    var zoomBtn = function (label, z) {
      return h('button', { type: 'button', class: 'zbtn' + (S.zoom === z ? ' on' : ''), text: label, onclick: function () { S.zoom = z; applyZoom(S.sheetEl, pg); $$('.zbtn', pane).forEach(function (b) { b.classList.toggle('on', b.textContent === label); }); } });
    };
    var overlayCb = h('input', { type: 'checkbox', checked: S.overlay, disabled: !regionUrl(n), onchange: function (ev) { S.overlay = ev.target.checked; renderSheet(pane, withRegions); } });
    var bar = h('div', { class: 'sheet-bar' }, zoomBtn('Fit', 'fit'), zoomBtn('100%', 1), zoomBtn('200%', 2),
      h('label', { class: 'ovl' }, overlayCb, ' Detector overlay'),
      h('span', { class: 'hint', text: 'ctrl+wheel zooms' }));
    var view = h('div', { class: 'sheet-view' });
    if (n === 0 || !pg.url) {
      S.sheetEl = null;
      append(view, h('div', { class: 'nosheet', text: n === 0 ? 'Typed comments have no sheet image.' : 'No page image for page ' + n + '.' }));
    } else {
      var sheet = h('div', { class: 'sheet' });
      applyZoom(sheet, pg);
      append(sheet, h('img', { src: pg.url, decoding: 'async', alt: 'page ' + n, class: 'page-img' }));
      if (S.overlay && regionUrl(n)) append(sheet, h('img', { src: regionUrl(n), decoding: 'async', alt: 'detector regions', class: 'region-img' }));
      append(sheet, buildMarks(n, pg, withRegions));
      append(sheet, buildPins(n, pg));
      append(view, sheet);
      S.sheetEl = sheet;
    }
    append(pane, [tabs, bar, view, sheetInfo(n)]);
    if (!pane.dataset.wheel) {
      pane.dataset.wheel = '1';
      pane.addEventListener('wheel', function (ev) {
        if (!ev.ctrlKey || !S.sheetEl) return;
        ev.preventDefault();
        var info = pageInfo(S.page);
        if (!info.W) return;
        var cur = S.zoom === 'fit' ? S.sheetEl.clientWidth / info.W : S.zoom;
        S.zoom = Math.max(0.1, Math.min(6, cur * (ev.deltaY < 0 ? 1.1 : 0.9)));
        applyZoom(S.sheetEl, info);
        $$('.zbtn', pane).forEach(function (b) { b.classList.remove('on'); });
      }, { passive: false });
    }
  }

  function svgRect(b, cls, extra) {
    return C.el('rect', Object.assign({ x: b.x0, y: b.y0, width: Math.max(1, b.x1 - b.x0), height: Math.max(1, b.y1 - b.y0), class: cls }, extra || {}));
  }

  function buildMarks(n, pg, withRegions) {
    var svg = C.el('svg', { viewBox: '0 0 ' + pg.W + ' ' + pg.H, preserveAspectRatio: 'none', class: 'marks' });
    var r = Math.max(5, pg.W / 300);
    commentsOnPage(n).forEach(function (c) {
      var id = sid(c.id), b = bbox(c);
      var g = C.el('g', { class: 'mark ' + statusClass(id) + (id === S.selected ? ' sel' : ''), 'data-id': id });
      if (b) {
        g.appendChild(svgRect(b, 'bb'));
        var cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2;
        arr(c.targets_px).forEach(function (t) {
          t = arr(t);
          if (t.length < 2 || !isNum(t[0]) || !isNum(t[1])) return;
          g.appendChild(C.el('line', { x1: cx, y1: cy, x2: t[0], y2: t[1], class: 'tl' }));
          g.appendChild(C.el('circle', { cx: t[0], cy: t[1], r: r, class: 'tp' }));
        });
      }
      var tb = bbox({ bbox_px: c.target_bbox_px });
      if (tb) g.appendChild(svgRect(tb, 'tgt'));
      g.addEventListener('click', function () { selectCard(id, true); });
      svg.appendChild(g);
    });
    arr(S.ix.report.recall_warnings).forEach(function (w) {
      w = obj(w);
      if (Number(w.page_no) !== n) return;
      var b = bbox(w);
      if (b) svg.appendChild(svgRect(b, 'warn', { 'data-tip': sid(w.kind) + ': ' + sid(w.detail) }));
    });
    if (withRegions) {
      arr(obj(S.ix.sheetsBy[n]).regions).forEach(function (rg) {
        var b = Array.isArray(rg) ? bbox({ bbox_px: rg }) : bbox({ bbox_px: obj(rg).bbox_px || obj(rg).bbox });
        if (b) svg.appendChild(svgRect(b, 'region'));
      });
    }
    C.bindTips(svg);
    return svg;
  }

  function buildPins(n, pg) {
    var pins = [];
    commentsOnPage(n).forEach(function (c) {
      var id = sid(c.id), b = bbox(c);
      if (!b || !pg.W || !pg.H) return;
      var x = (b.x0 + b.x1) / 2 / pg.W * 100, y = b.y0 / pg.H * 100;
      var pin = h('button', {
        type: 'button', class: 'pin ' + statusClass(id) + (id === S.selected ? ' sel' : ''), text: String(S.ix.num[id]),
        title: truncate(sid(c.text), 80), dataset: { id: id },
        style: 'left:' + x.toFixed(3) + '%;top:' + y.toFixed(3) + '%',
        onclick: function () { selectCard(id, true); },
        onmouseenter: function () { highlight(id, true); },
        onmouseleave: function () { highlight(id, false); }
      });
      pins.push(pin);
    });
    return pins;
  }

  function highlight(id, on) {
    $$('[data-id]').forEach(function (e) {
      if (e.dataset.id === id && (e.classList.contains('pin') || e.classList.contains('card') || e.classList.contains('mark'))) e.classList.toggle('hl', on);
    });
  }

  function sheetInfo(n) {
    var sh = S.ix.sheetsBy[n], info = h('div', { class: 'sheet-info' });
    if (!sh) { append(info, h('span', { class: 'muted', text: n === 0 ? 'Comments typed by hand, no sheet.' : 'No sheet record for this page.' })); return info; }
    var tb = obj(sh.title_block);
    var parts = [];
    if (tb.sheet_no) parts.push('sheet ' + sid(tb.sheet_no));
    if (tb.project_no) parts.push('project ' + sid(tb.project_no));
    if (tb.printed_file) parts.push('printed from ' + sid(tb.printed_file));
    if (tb.how) parts.push('read by ' + sid(tb.how));
    append(info, h('div', { class: 'tb', text: 'Title block: ' + (parts.join(' \u00b7 ') || 'nothing read') }));
    var line;
    if (sh.dwg_path) {
      line = 'Matched ' + sid(sh.dwg_path) + (sh.layout_name ? ' / ' + sid(sh.layout_name) : '') + (sh.how ? ' (' + sid(sh.how) + (isNum(sh.score) ? ', score ' + fix(sh.score, 2) : '') + ')' : '') + (sh.opened_by_pipeline ? ' \u00b7 opened by the pipeline' : '');
    } else {
      line = 'Not matched' + (sh.reason ? ': ' + sid(sh.reason) : (S.ix.unmatchedBy[n] ? ': ' + S.ix.unmatchedBy[n] : ''));
    }
    append(info, h('div', { class: 'dwg', text: line }));
    var vps = arr(sh.viewports);
    if (vps.length) append(info, h('div', { class: 'muted', text: vps.length + ' viewport' + (vps.length > 1 ? 's' : '') }));
    var mk = obj(sh.markups);
    if (mk.textboxes !== undefined) append(info, h('div', { class: 'muted', text: 'Markups: ' + sid(mk.textboxes) + ' text boxes, ' + sid(mk.other || 0) + ' other' }));
    return info;
  }

  /* ---- comment cards ---------------------------------------------------- */

  function renderCards() {
    var host = $('#cards');
    clear(host);
    if (!S.ix.order.length) { append(host, h('p', { class: 'empty', text: 'This review has no comments.' })); return; }
    S.ix.order.forEach(function (id) { host.appendChild(cardEl(id)); });
  }

  function replaceCard(id) {
    var old = $('#cards .card[data-id="' + CSS.escape(id) + '"]');
    if (!old) return null;
    var fresh = cardEl(id);
    old.replaceWith(fresh);
    return fresh;
  }

  function selectedTray() {
    return S.selected ? $('#cards .card[data-id="' + CSS.escape(S.selected) + '"] .tray') : null;
  }

  function selectCard(id, scroll) {
    if (!S.ix.byId[id]) return;
    var prev = S.selected;
    S.selected = id;
    if (prev && prev !== id) replaceCard(prev);
    var card = replaceCard(id);
    var page = Number(S.ix.byId[id].page_no) || 0;
    var pane = S.tab === 'ocr' ? $('#ocrSheet') : $('#sheetPane');
    if (page !== S.page && S.ix.pages.indexOf(page) >= 0) { S.page = page; renderSheet(pane, S.tab === 'ocr'); }
    else {
      $$('.pin, .mark', pane).forEach(function (e) { e.classList.toggle('sel', e.dataset.id === id); });
    }
    if (scroll && card) card.scrollIntoView({ block: 'nearest' });
    $$('#ocrPane tr[data-id]').forEach(function (tr) { tr.classList.toggle('sel', tr.dataset.id === id); });
  }

  function whereLine(c) {
    var id = sid(c.id);
    if (c.scope === 'sheet') return 'whole sheet';
    var loc = locatedFor(id);
    var act = primaryAction(id);
    if (!loc) {
      if (act && act.status === 'not_located') return 'not located' + (act.reason ? ': ' + sid(act.reason) : '');
      return isOcr() ? '' : 'not located';
    }
    var ctx = obj(loc.context);
    if (loc.space === 'model' && arr(loc.model_xy).length >= 2) {
      var xy = loc.model_xy, parts = ['model (' + Math.round(xy[0]) + ', ' + Math.round(xy[1]) + ')'];
      var vp = obj(ctx.viewport);
      if (vp.scale_text) parts.push(sid(vp.scale_text));
      var pick = obj(ctx.pick), o = pick.object;
      if (o && typeof o === 'object') {
        parts.push('pick: ' + sid(o.type) + ' ' + sid(o.handle) + (isNum(o.distance) ? ' ' + fix(o.distance, 1) + ' ft' : ''));
      } else {
        var cand = arr(pick.candidates)[0] || ctx.nearest;
        var tol = isNum(ctx.pick_tolerance_ft) ? ctx.pick_tolerance_ft : null;
        var s = 'nothing' + (tol !== null ? ' within ' + tol + ' ft' : ' picked');
        if (cand && typeof cand === 'object') s += '; nearest ' + sid(cand.type) + ' ' + sid(cand.handle) + (isNum(cand.distance) ? ' at ' + fix(cand.distance, 0) + ' ft' : '');
        parts.push(s);
      }
      var enc = arr(pick.enclosing)[0];
      if (enc && typeof enc === 'object') parts.push('inside ' + sid(enc.type) + ' ' + sid(enc.handle));
      if (loc.target_count > 1) parts.push('target 1 of ' + loc.target_count);
      return parts.join(' \u00b7 ');
    }
    if (loc.space === 'paper' && arr(loc.paper_xy).length >= 2) {
      return 'paper (' + fix(loc.paper_xy[0], 2) + ', ' + fix(loc.paper_xy[1], 2) + ') in' + (loc.reason ? ' \u00b7 ' + sid(loc.reason) : '');
    }
    return 'not located' + (loc.reason ? ': ' + sid(loc.reason) : '');
  }

  function cropEl(c) {
    var id = sid(c.id), url = sid(obj(obj(S.payload.images).crops)[id]);
    if (url) return h('img', { class: 'crop', src: url, decoding: 'async', alt: 'crop of comment ' + id });
    var pg = pageInfo(Number(c.page_no) || 0), b = bbox(c);
    if (!pg.url || !pg.W || !pg.H || !b) return h('div', { class: 'crop none', text: c.scope === 'sheet' ? 'whole sheet' : 'no crop' });
    var padX = (b.x1 - b.x0) * 0.2 + 8, padY = (b.y1 - b.y0) * 0.2 + 8;
    var rx0 = Math.max(0, b.x0 - padX), ry0 = Math.max(0, b.y0 - padY);
    var rx1 = Math.min(pg.W, b.x1 + padX), ry1 = Math.min(pg.H, b.y1 + padY);
    var rw = Math.max(1, rx1 - rx0), rh = Math.max(1, ry1 - ry0);
    var posX = pg.W > rw ? rx0 / (pg.W - rw) * 100 : 0, posY = pg.H > rh ? ry0 / (pg.H - rh) * 100 : 0;
    var box = h('div', { class: 'crop bg', title: 'markup region on page ' + sid(c.page_no) });
    box.style.backgroundImage = 'url("' + url2css(pg.url) + '")';
    box.style.backgroundSize = (pg.W / rw * 100).toFixed(2) + '% ' + (pg.H / rh * 100).toFixed(2) + '%';
    box.style.backgroundPosition = posX.toFixed(2) + '% ' + posY.toFixed(2) + '%';
    box.style.aspectRatio = rw + ' / ' + rh;
    return box;
  }

  function url2css(u) { return sid(u).replace(/["\\]/g, function (m) { return '\\' + m; }); }

  function stepStatusFor(id, name, index) {
    var a = primaryAction(id);
    if (!a) return '';
    var steps = arr(a.steps).map(obj);
    if (steps[index] && sid(steps[index].tool) === name) return sid(steps[index].status);
    for (var i = 0; i < steps.length; i++) if (sid(steps[i].tool) === name) return sid(steps[i].status);
    return '';
  }

  function toolChip(name, extraCls, glyphStatus) {
    var e = h('span', { class: 'blk static bin-' + binOf(name) + (extraCls ? ' ' + extraCls : ''), dataset: { tool: name }, title: sid(obj(tools().descriptions)[name]) || name }, name);
    if (glyphStatus !== undefined) {
      var g = glyphStatus ? (GLYPH[glyphStatus] || '\u00b7') : '\u00b7';
      e.appendChild(h('span', { class: 'glyph st-' + (glyphStatus || 'planned'), text: g, title: glyphStatus || 'no step' }));
    }
    return e;
  }

  function chainEl(id) {
    var seq = S.ix.seqBy[id], names = modelChain(id), row = h('div', { class: 'chain' });
    if (!names.length && !(seq && arr(seq.unknown_tools).length)) {
      append(row, h('span', { class: 'muted', text: seq && seq.error ? 'model error: ' + truncate(seq.error, 80) : 'no chain' }));
      return row;
    }
    names.forEach(function (nm, i) { append(row, toolChip(nm, '', stepStatusFor(id, nm, i))); });
    arr(seq && seq.unknown_tools).forEach(function (u) { append(row, h('span', { class: 'blk static unknown', text: sid(u), title: 'not in the vocabulary' })); });
    return row;
  }

  function outcomeEl(id) {
    var acts = arr(S.ix.actionsBy[id]), row = h('div', { class: 'outcome' });
    if (!acts.length) { append(row, h('span', { class: 'chip st-none', text: 'no action' })); return row; }
    acts.forEach(function (a, i) {
      if (i > 2) return;
      a = obj(a);
      append(row, h('span', { class: 'chip st-' + sid(a.status), text: sid(a.status).replace('_', ' ') || 'unknown', title: sid(a.key) }));
      if (a.reason) append(row, h('span', { class: 'reason', text: truncate(a.reason, 140), title: sid(a.reason) }));
    });
    if (acts.length > 3) append(row, h('span', { class: 'muted', text: '+' + (acts.length - 3) + ' more' }));
    return row;
  }

  function doabilityEl(id) {
    var a = primaryAction(id), d = a ? a.doability : null, row = h('div', { class: 'doab' });
    if (!d || typeof d !== 'object') { append(row, h('span', { class: 'muted', text: 'doability not recorded' })); return row; }
    var keys = [['grounded', 'G'], ['action_covered', 'A'], ['params_determinable', 'P'], ['unambiguous', 'U']];
    var reasons = arr(d.reasons), lit = 0;
    keys.forEach(function (k, i) {
      var on = !!d[k[0]];
      if (on) lit++;
      append(row, h('span', { class: 'sq' + (on ? ' on' : ''), text: k[1], title: k[0] + ': ' + (on ? 'yes' : 'no') + (reasons[i] ? ' \u2014 ' + sid(reasons[i]) : '') }));
    });
    var passes = isNum(d.passes) ? d.passes : lit;
    append(row, h('span', { class: 'kof', text: passes + '/4' + (isNum(d.score) ? ' \u00b7 ' + fix(d.score, 2) : '') }));
    return row;
  }

  function stepsTable(id) {
    var acts = arr(S.ix.actionsBy[id]), rows = [];
    acts.forEach(function (a) {
      arr(obj(a).steps).forEach(function (s, i) {
        s = obj(s);
        var pj = jsonShort(s.params === undefined ? {} : s.params, 160);
        rows.push(h('tr', null,
          h('td', { class: 'num', text: String(i + 1) }),
          h('td', null, toolChip(sid(s.tool))),
          h('td', { class: 'mono params', text: pj.short, title: pj.full }),
          h('td', null, h('span', { class: 'pill st-' + sid(s.status), text: sid(s.status).replace('_', ' ') || '\u2014' })),
          h('td', { class: 'num', text: isNum(s.latency_s) ? fix(s.latency_s, 1) + ' s' : '\u2014' }),
          h('td', { class: 'mono', text: truncate(sid(s.result_preview), 120), title: sid(s.result_preview) }),
          h('td', { class: 'err', text: truncate(sid(s.error), 120), title: sid(s.error) }),
          h('td', { class: 'mono', text: arr(s.created).join(' ') })
        ));
      });
    });
    if (!rows.length) return h('p', { class: 'muted', text: 'No steps.' });
    return h('table', { class: 'steps' },
      h('thead', null, h('tr', null, ['#', 'tool', 'params (read only)', 'status', 'latency', 'result', 'error', 'created'].map(function (t) { return h('th', { text: t }); }))),
      h('tbody', null, rows));
  }

  function detailsEl(id) {
    var seq = S.ix.seqBy[id], acts = arr(S.ix.actionsBy[id]);
    var meta = [];
    if (seq && seq.model) meta.push('model ' + sid(seq.model));
    acts.forEach(function (a) { a = obj(a); if (a.dashboard) meta.push('dashboard ' + sid(a.dashboard)); if (a.dwg_path) meta.push(basename(a.dwg_path)); });
    var raw = seq && seq.raw_reply ? sid(seq.raw_reply) : '';
    var stepRaw = acts.map(function (a) { return arr(obj(a).steps).map(function (s) { return sid(obj(s).raw_reply); }).filter(Boolean).join('\n'); }).filter(Boolean).join('\n');
    return h('details', { class: 'details' },
      h('summary', null, 'Details', meta.length ? h('span', { class: 'muted', text: ' \u00b7 ' + meta.join(' \u00b7 ') }) : null),
      stepsTable(id),
      raw ? h('div', { class: 'rawhead', text: 'Model reply' }) : null,
      raw ? h('pre', { class: 'raw', text: raw }) : null,
      stepRaw ? h('div', { class: 'rawhead', text: 'Step replies' }) : null,
      stepRaw ? h('pre', { class: 'raw', text: stepRaw }) : null);
  }

  function labelChipEl(id) {
    var lab = shownLabel(id);
    if (!lab || (!lab.verdict && !Array.isArray(lab.tools))) return h('span', { class: 'label-chip none', text: 'unlabelled' });
    var v = sid(lab.verdict);
    var txt = (VGLYPH[v] || '\u2013') + ' ' + (v || 'chain only') + ' \u00b7 ' + sid(lab.labeller) + (lab.updated ? ' \u00b7 ' + fmtTime(lab.updated) : '');
    var others = Object.keys(labelRecords(id)).filter(function (w) { return w !== lab.labeller; });
    return h('span', { class: 'label-chip v-' + (v || 'none'), text: txt, title: others.length ? 'also labelled by ' + others.join(', ') : null });
  }

  function labelStrip(id) {
    var lab = shownLabel(id), cur = lab ? sid(lab.verdict) : '';
    var seg = h('div', { class: 'seg', role: 'group', 'aria-label': 'verdict' }, VERDICTS.map(function (v) {
      return h('button', { type: 'button', class: 'v v-' + v[0] + (cur === v[0] ? ' on' : ''), dataset: { v: v[0] }, text: v[1], onclick: function () { setVerdict(id, v[0]); } });
    }));
    var strip = h('div', { class: 'strip' }, seg);
    if (!isOcr()) {
      var tray = h('div', { class: 'tray', role: 'list', tabindex: '-1', 'data-placeholder': 'Drag blocks here, or click one in the library' });
      trayChainFor(id).forEach(function (nm) { tray.appendChild(D.makeChip(nm, binOf(nm))); });
      append(strip, tray);
      append(strip, h('div', { class: 'strip-btns' },
        h('button', { type: 'button', class: 'ghost-btn', text: 'Use model\u2019s chain', onclick: function () { fillTray(id, modelChain(id)); } }),
        h('button', { type: 'button', class: 'ghost-btn', text: 'Clear', onclick: function () { fillTray(id, []); } }),
        h('span', { class: 'badge', dataset: { badge: id } })));
    } else {
      append(strip, h('span', { class: 'badge', dataset: { badge: id } }));
    }
    return strip;
  }

  function cardEl(id) {
    var c = S.ix.byId[id], sel = id === S.selected;
    var conf = isNum(c.confidence) ? Math.max(0, Math.min(1, c.confidence)) : null;
    var card = h('article', { class: 'card ' + statusClass(id) + (sel ? ' sel' : ''), dataset: { id: id }, tabindex: '-1',
      onclick: function (ev) { if (!sel && !ev.target.closest('button, .tray, details, input')) selectCard(id, false); },
      onmouseenter: function () { highlight(id, true); }, onmouseleave: function () { highlight(id, false); } });
    var head = h('header', { class: 'card-head' },
      h('span', { class: 'pin-no', text: '#' + S.ix.num[id] }),
      h('span', { class: 'cid mono', text: id }),
      c.kind ? h('span', { class: 'kind', text: sid(c.kind) }) : null,
      c.author ? h('span', { class: 'author', text: sid(c.author) }) : null,
      c.subject ? h('span', { class: 'author', text: sid(c.subject) }) : null,
      c.page_no !== undefined ? h('span', { class: 'muted', text: (Number(c.page_no) || 0) === 0 ? 'typed' : 'p.' + sid(c.page_no) }) : null,
      h('span', { class: 'conf', title: conf === null ? 'no confidence' : 'confidence ' + fix(conf, 2) }, h('span', { class: 'conf-fill', style: 'width:' + (conf === null ? 0 : conf * 100) + '%' })),
      sel ? null : labelChipEl(id));
    var where = whereLine(c);
    append(card, [
      head,
      h('p', { class: 'ctext', text: sid(c.text) || '(no text)' }),
      where ? h('div', { class: 'where', text: where }) : null,
      h('div', { class: 'card-mid' }, cropEl(c), isOcr() ? null : h('div', { class: 'card-right' }, chainEl(id), outcomeEl(id), doabilityEl(id))),
      isOcr() ? null : detailsEl(id),
      sel ? labelStrip(id) : null
    ]);
    return card;
  }

  /* ---- labels ----------------------------------------------------------- */

  function requireLabeller() {
    S.labeller = $('#labeller').value.trim();
    if (S.labeller) { lsSet('redline.labeller', S.labeller); return true; }
    banner('Enter a labeller name (top right) before saving a label.', 'warn');
    $('#labeller').focus();
    return false;
  }

  function fillTray(id, names) {
    var tray = selectedTray();
    if (!tray || S.selected !== id) return;
    clear(tray);
    names.forEach(function (nm) { tray.appendChild(D.makeChip(nm, binOf(nm))); });
    onTrayChange(tray);
  }

  function setVerdict(id, verdict) {
    if (!requireLabeller()) return;
    var cur = shownLabel(id) ? sid(shownLabel(id).verdict) : '';
    var v = cur === verdict ? null : verdict;
    var tray = selectedTray(), chain = null;
    if (!isOcr()) {
      if (tray && v === 'good' && !D.chainOf(tray).length) modelChain(id).forEach(function (nm) { tray.appendChild(D.makeChip(nm, binOf(nm))); });
      chain = tray ? D.chainOf(tray) : (shownLabel(id) && Array.isArray(shownLabel(id).tools) ? shownLabel(id).tools : null);
    }
    var card = $('#cards .card[data-id="' + CSS.escape(id) + '"]');
    if (card) $$('.seg .v', card).forEach(function (b) { b.classList.toggle('on', b.dataset.v === v); });
    scheduleSave(id, { labeller: S.labeller, verdict: v, tools: chain, note: '' });
  }

  function onTrayChange(tray) {
    var card = tray.closest('.card');
    if (!card) return;
    var id = card.dataset.id;
    if (!requireLabeller()) return;
    var chain = D.chainOf(tray);
    var lab = shownLabel(id), v = lab && lab.verdict ? sid(lab.verdict) : null;
    if (!v) v = sameChain(chain, modelChain(id)) ? 'good' : 'bad';
    $$('.seg .v', card).forEach(function (b) { b.classList.toggle('on', b.dataset.v === v); });
    scheduleSave(id, { labeller: S.labeller, verdict: v, tools: chain, note: '' });
  }

  function setBadge(id, state, retry) {
    var b = $('[data-badge="' + CSS.escape(id) + '"]');
    if (!b) return;
    clear(b);
    b.className = 'badge ' + state;
    if (state === 'saving') append(b, 'saving\u2026');
    else if (state === 'saved') append(b, 'saved \u2713');
    else if (state === 'failed') append(b, ['failed, ', h('button', { type: 'button', class: 'link', text: 'retry', onclick: retry })]);
  }

  function scheduleSave(id, body) {
    S.lastBodies[id] = body;
    clearTimeout(S.saveTimers[id]);
    setBadge(id, 'saving');
    S.saveTimers[id] = setTimeout(function () { doSave(id); }, 300);
  }

  async function doSave(id) {
    var body = S.lastBodies[id];
    if (!body) return;
    setBadge(id, 'saving');
    var res = await request(reviewPath(S.id) + '/labels/' + encodeURIComponent(id), {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
    if (!res.ok) {
      setBadge(id, 'failed', function () { doSave(id); });
      banner('Label save failed: ' + (obj(res.data).error || res.status || res.error), 'err');
      return;
    }
    applyLabelResponse(res.data);
    setBadge(id, 'saved');
    refreshCardLabelUi(id);
  }

  function applyLabelResponse(data) {
    data = obj(data);
    if (data.labels && typeof data.labels === 'object') S.payload.labels = data.labels;
    if (data.counts) S.payload.counts = data.counts;
    if (data.metrics) S.payload.metrics = data.metrics;
    renderLabelledCount();
    renderTiles();
    S.aggregate = null;
    if (S.tab === 'metrics') renderMetrics();
    $$('#ocrPane tr[data-id]').forEach(function (tr) { refreshOcrRow(tr); });
  }

  function refreshCardLabelUi(id) {
    var card = $('#cards .card[data-id="' + CSS.escape(id) + '"]');
    if (!card) return;
    var lab = shownLabel(id), v = lab ? sid(lab.verdict) : '';
    $$('.seg .v', card).forEach(function (b) { b.classList.toggle('on', b.dataset.v === v); });
    var old = $('.card-head .label-chip', card);
    if (old) old.replaceWith(labelChipEl(id));
  }

  /* ---- block library ----------------------------------------------------- */

  function rememberRecent(name) {
    S.recent = [name].concat(S.recent.filter(function (n) { return n !== name; })).slice(0, 8);
    lsSet('redline.recent', S.recent);
    if (S.libBin === 'recent' && !S.libSearch) renderLibraryList();
  }

  function libraryGroups() {
    var t = tools(), names = arr(t.names).map(sid), groups = {};
    names.forEach(function (n) { var b = binOf(n); (groups[b] = groups[b] || []).push(n); });
    Object.keys(groups).forEach(function (b) { groups[b].sort(); });
    return groups;
  }

  function binOrder() {
    var t = tools(), order = arr(t.bin_order).map(sid);
    if (!order.length) order = DEFAULT_BINS.slice();
    var groups = libraryGroups();
    if (groups.other && order.indexOf('other') < 0) order.push('other');
    Object.keys(groups).forEach(function (b) { if (order.indexOf(b) < 0) order.push(b); });
    return order;
  }

  function binLabel(b) { return sid(obj(tools().labels)[b]) || b; }

  function renderLibrary() {
    var lib = $('#library');
    clear(lib);
    lib.classList.toggle('closed', !S.libOpen);
    if (isOcr()) { lib.hidden = true; return; }
    lib.hidden = false;
    var head = h('div', { class: 'lib-head', onclick: function (ev) { if (ev.target.closest('input')) return; S.libOpen = !S.libOpen; lib.classList.toggle('closed', !S.libOpen); } },
      h('span', { class: 'lib-title', text: 'Block library' }),
      h('span', { class: 'muted', text: arr(tools().names).length + ' tools' }),
      h('input', { type: 'search', class: 'lib-search', id: 'libSearch', placeholder: 'search tools  ( / )', value: S.libSearch, spellcheck: 'false',
        oninput: function (ev) { S.libSearch = ev.target.value.trim().toLowerCase(); renderLibraryList(); },
        onkeydown: function (ev) { if (ev.key === 'Escape') { ev.target.value = ''; S.libSearch = ''; renderLibraryList(); ev.target.blur(); } } }),
      h('span', { class: 'lib-toggle', text: S.libOpen ? '\u25be' : '\u25b4' }));
    var groups = libraryGroups();
    var tabs = h('div', { class: 'lib-tabs' },
      h('button', { type: 'button', class: 'ltab' + (S.libBin === 'recent' ? ' on' : ''), dataset: { bin: 'recent' }, text: 'Recent', onclick: function () { S.libBin = 'recent'; renderLibraryList(); } }),
      binOrder().map(function (b) {
        var n = arr(groups[b]).length;
        return h('button', { type: 'button', class: 'ltab bin-' + b + (S.libBin === b ? ' on' : ''), dataset: { bin: b }, text: binLabel(b) + ' ' + n, disabled: !n, onclick: function () { S.libBin = b; renderLibraryList(); } });
      }));
    append(lib, [head, tabs, h('div', { class: 'lib-list', id: 'libList' })]);
    renderLibraryList();
  }

  function libBlock(name) {
    return h('button', { type: 'button', class: 'blk bin-' + binOf(name), dataset: { tool: name }, title: sid(obj(tools().descriptions)[name]) || name, text: name });
  }

  function renderLibraryList() {
    var list = $('#libList');
    if (!list) return;
    clear(list);
    $$('.ltab').forEach(function (b) { b.classList.toggle('on', b.dataset.bin === S.libBin && !S.libSearch); });
    var groups = libraryGroups();
    if (S.libSearch) {
      var any = false;
      binOrder().forEach(function (b) {
        var hits = arr(groups[b]).filter(function (n) { return n.toLowerCase().indexOf(S.libSearch) >= 0 || sid(obj(tools().descriptions)[n]).toLowerCase().indexOf(S.libSearch) >= 0; });
        if (!hits.length) return;
        any = true;
        append(list, h('div', { class: 'lib-group' }, h('span', { class: 'lib-gname bin-' + b, text: binLabel(b) }), hits.map(libBlock)));
      });
      if (!any) append(list, h('p', { class: 'muted', text: 'No tool matches \u201c' + S.libSearch + '\u201d.' }));
      return;
    }
    if (S.libBin === 'recent') {
      var known = arr(tools().names).map(sid);
      var rec = S.recent.filter(function (n) { return known.indexOf(n) >= 0; });
      if (!rec.length) append(list, h('p', { class: 'muted', text: 'Blocks you add to a chain show up here.' }));
      else append(list, h('div', { class: 'lib-group' }, rec.map(libBlock)));
      return;
    }
    var names = arr(groups[S.libBin]);
    if (!names.length) append(list, h('p', { class: 'muted', text: 'No tools in this bin.' }));
    else append(list, h('div', { class: 'lib-group' }, names.map(libBlock)));
  }

  /* ---- keyboard ---------------------------------------------------------- */

  function wireKeys() {
    document.addEventListener('keydown', function (ev) {
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
      var t = ev.target, tag = t && t.tagName ? t.tagName.toLowerCase() : '';
      var typing = tag === 'input' || tag === 'textarea' || tag === 'select' || (t && t.isContentEditable);
      if (ev.key === '/' && !typing) {
        var s = $('#libSearch');
        if (s) { ev.preventDefault(); if (!S.libOpen) { S.libOpen = true; $('#library').classList.remove('closed'); } s.focus(); s.select(); }
        return;
      }
      if (typing || !S.payload || (S.tab !== 'run' && S.tab !== 'ocr')) return;
      if (ev.key === 'j' || ev.key === 'k') {
        var i = S.ix.order.indexOf(S.selected);
        var next = ev.key === 'j' ? Math.min(S.ix.order.length - 1, i + 1) : Math.max(0, i - 1);
        if (S.ix.order[next] && next !== i) { selectCard(S.ix.order[next], true); ev.preventDefault(); }
      } else if (ev.key === '1' || ev.key === '2' || ev.key === '3') {
        if (S.selected) { setVerdict(S.selected, ['good', 'bad', 'unsure'][Number(ev.key) - 1]); ev.preventDefault(); }
      }
    });
  }

  /* ---- metrics view ----------------------------------------------------- */

  function renderMetrics() {
    var view = $('#view-metrics');
    clear(view);
    S.charts = [];
    var toggle = h('div', { class: 'scope' },
      h('button', { type: 'button', class: 'tab' + (S.scope === 'run' ? ' on' : ''), text: 'This run', onclick: function () { S.scope = 'run'; renderMetrics(); } }),
      h('button', { type: 'button', class: 'tab' + (S.scope === 'all' ? ' on' : ''), text: 'All runs', onclick: function () { S.scope = 'all'; renderMetrics(); } }),
      h('span', { class: 'scope-note', id: 'scopeNote' }));
    append(view, toggle);
    var grid = h('div', { class: 'chart-grid' });
    append(view, grid);
    if (S.scope === 'run') {
      $('#scopeNote').textContent = 'review ' + S.id;
      buildMetricCards(grid, obj(S.payload.metrics), false);
      return;
    }
    if (S.aggregate) { noteAggregate(S.aggregate); buildMetricCards(grid, S.aggregate, true); return; }
    $('#scopeNote').textContent = 'loading\u2026';
    request(API + '/metrics?limit=50').then(function (res) {
      if (S.scope !== 'all') return;
      if (!res.ok) { $('#scopeNote').textContent = 'could not load ' + res.url + (res.status ? ' (' + res.status + ')' : ''); buildMetricCards(grid, {}, true); return; }
      S.aggregate = obj(res.data);
      noteAggregate(S.aggregate);
      buildMetricCards(grid, S.aggregate, true);
    });
  }

  function noteAggregate(agg) {
    var revs = arr(agg.reviews), labelled = revs.reduce(function (a, r) { return a + (Number(obj(r).labelled) || 0); }, 0);
    var note = $('#scopeNote');
    if (note) note.textContent = revs.length + ' review' + (revs.length === 1 ? '' : 's') + ', ' + labelled + ' labelled';
  }

  function chartCard(grid, title, n, draw, rows, opts) {
    opts = opts || {};
    var body = h('div', { class: 'chart-body' });
    var tbl = h('div', { class: 'chart-table', hidden: true });
    var btn = h('button', { type: 'button', class: 'ghost-btn small', text: 'Table', onclick: function () {
      tbl.hidden = !tbl.hidden;
      if (!tbl.hidden && !tbl.firstChild) tbl.appendChild(C.tableFor(typeof rows === 'function' ? rows() : rows));
    } });
    var card = h('section', { class: 'chart-card' + (opts.wide ? ' wide' : '') },
      h('header', null, h('h3', { text: title }), h('span', { class: 'n', text: n === null || n === undefined ? '' : 'n = ' + n }), btn),
      body, tbl);
    var redraw = function () {
      clear(body);
      if (!n && !opts.alwaysDraw) { append(body, h('p', { class: 'empty', text: EMPTY_CHART })); return; }
      try { draw(body); } catch (e) { append(body, h('p', { class: 'empty', text: 'Could not draw: ' + (e && e.message || e) })); }
    };
    grid.appendChild(card);
    redraw();
    S.charts.push(redraw);
    return card;
  }

  function sumVals(o) { return Object.keys(obj(o)).reduce(function (a, k) { return a + (Number(o[k]) || 0); }, 0); }

  function orderedKeys(o, order) {
    var keys = Object.keys(obj(o));
    var out = order.filter(function (k) { return keys.indexOf(k) >= 0; });
    keys.forEach(function (k) { if (out.indexOf(k) < 0) out.push(k); });
    return out;
  }

  function rateBar(label, r, cls) {
    r = obj(r);
    return { label: label, value: isNum(r.rate) ? r.rate : 0, low: r.n ? r.low : undefined, high: r.n ? r.high : undefined, cls: cls || 'c-accent', valueText: r.n ? pct(r.rate) + ' ' + r.k + '/' + r.n : '\u2014', tip: label + ': ' + fmtRate(r) };
  }

  function rateRow(name, r) { r = obj(r); return { metric: name, k: r.k, n: r.n, rate: r.rate, low: r.low, high: r.high }; }

  function hero(value, label) {
    return h('div', { class: 'hero' }, h('div', { class: 'hero-v', text: value }), h('div', { class: 'hero-l', text: label }));
  }

  function tileRow(tiles) {
    return h('div', { class: 'mtiles' }, tiles.map(function (t) { return h('div', { class: 'mtile', title: t[2] || null }, h('div', { class: 'tv', text: t[1] }), h('div', { class: 'tl', text: t[0] })); }));
  }

  function buildMetricCards(grid, m, all) {
    var out = obj(m.outcomes), n = obj(m.n);
    var actions = obj(out.actions), steps = obj(out.steps);

    var nAct = sumVals(actions);
    chartCard(grid, 'Outcomes', nAct, function (body) {
      C.barChart(body, { bars: orderedKeys(actions, STATUS_ORDER).map(function (k) { return { label: k.replace('_', ' '), value: Number(actions[k]) || 0, cls: 'c-st-' + k }; }) });
      if (sumVals(steps)) {
        append(body, h('div', { class: 'sub-title', text: 'Steps' }));
        C.barChart(body, { bars: orderedKeys(steps, STEP_ORDER).map(function (k) { return { label: k.replace('_', ' '), value: Number(steps[k]) || 0, cls: 'c-st-' + k }; }), rowH: 20 });
      }
    }, function () { return orderedKeys(actions, STATUS_ORDER).map(function (k) { return { status: k, actions: actions[k] }; }).concat(orderedKeys(steps, STEP_ORDER).map(function (k) { return { status: 'step ' + k, actions: steps[k] }; })); });

    var bins = obj(out.bins), binKeys = orderedKeys(bins, binOrder());
    var nBins = binKeys.reduce(function (a, b) { return a + (Number(obj(bins[b]).steps) || 0); }, 0);
    chartCard(grid, 'Tools by bin', nBins, function (body) {
      C.barChart(body, { bars: binKeys.map(function (b) {
        var r = obj(bins[b]);
        return { label: binLabel(b), swatchCls: 'c-bin-' + b, segments: ['ran', 'dry', 'failed', 'blocked', 'not_filled'].map(function (s) { return { label: s.replace('_', ' '), value: Number(r[s]) || 0, cls: 'c-st-' + s }; }), valueText: String(Number(r.steps) || 0) };
      }), labelWidth: 150 });
      append(body, h('ul', { class: 'legend' }, ['ran', 'dry', 'failed', 'blocked', 'not_filled'].map(function (s) { return h('li', null, h('span', { class: 'sw c-st-' + s }), s.replace('_', ' ')); })));
    }, function () { return binKeys.map(function (b) { return Object.assign({ bin: b }, obj(bins[b])); }); });

    var tiers = obj(out.tiers), tierOrder = arr(tools().tier_order).map(sid);
    if (!tierOrder.length) tierOrder = DEFAULT_TIERS.slice();
    var nTiers = sumVals(tiers);
    chartCard(grid, 'Risk tiers', nTiers, function (body) {
      C.segmentRow(body, { segments: orderedKeys(tiers, tierOrder).map(function (t) { return { label: t, value: Number(tiers[t]) || 0, cls: 'c-tier-' + t }; }) });
    }, function () { return orderedKeys(tiers, tierOrder).map(function (t) { return { tier: t, comments: tiers[t] }; }); });

    var cov = obj(m.coverage), prec = obj(m.precision), byClass = obj(m.by_class);
    chartCard(grid, 'Coverage and precision', Number(cov.n) || 0, function (body) {
      C.barChart(body, { bars: [rateBar('Coverage', cov, 'c-accent'), rateBar('Precision', prec, 'c-st-done')], max: 1, valueWidth: 90 });
      var classes = orderedKeys(byClass, ['none', 'read', 'draw', 'modify', 'delete', 'design']);
      if (classes.length) {
        append(body, h('div', { class: 'sub-title', text: 'By action class' }));
        var multi = h('div', { class: 'multiples' });
        classes.forEach(function (k) {
          var bc = obj(byClass[k]), cell = h('div', { class: 'multiple' }, h('div', { class: 'm-title', text: k + ' \u00b7 n ' + (Number(obj(bc.coverage).n) || 0) }));
          multi.appendChild(cell);
          C.barChart(cell, { bars: [rateBar('cov', bc.coverage, 'c-accent'), rateBar('prec', bc.precision, 'c-st-done')], max: 1, rowH: 20, labelWidth: 40, valueWidth: 80 });
        });
        append(body, multi);
      }
    }, function () {
      var rows = [rateRow('coverage', cov), rateRow('precision', prec)];
      Object.keys(byClass).forEach(function (k) { rows.push(rateRow(k + ' coverage', obj(byClass[k]).coverage)); rows.push(rateRow(k + ' precision', obj(byClass[k]).precision)); });
      return rows;
    });

    var doab = obj(m.doability), checks = obj(doab.checks);
    chartCard(grid, 'Doability checks', Number(doab.n) || 0, function (body) {
      append(body, hero(fix(doab.mean, 2), 'mean doability score'));
      C.barChart(body, { bars: [['grounded', 'Grounded'], ['action_covered', 'Action covered'], ['params_determinable', 'Params determinable'], ['unambiguous', 'Unambiguous']].map(function (k) { return rateBar(k[1], checks[k[0]], 'c-bin-modify'); }), max: 1, valueWidth: 90, labelWidth: 150 });
    }, function () { return Object.keys(checks).map(function (k) { return rateRow(k, checks[k]); }).concat([{ metric: 'mean', rate: doab.mean, n: doab.n }]); });

    var ch = obj(m.chains), fw = obj(ch.first_wrong);
    chartCard(grid, 'Chain accuracy', Number(ch.n) || 0, function (body) {
      var pooled = obj(ch.pooled);
      append(body, tileRow([['Precision', fix(ch.precision, 2)], ['Recall', fix(ch.recall, 2)], ['F1', fix(ch.f1, 2)], ['Exact', fmtRate(ch.exact)], ['Order', fix(ch.order, 2)], ['tp / fp / fn', sid(pooled.tp || 0) + ' / ' + sid(pooled.fp || 0) + ' / ' + sid(pooled.fn || 0)]]));
      var keys = Object.keys(fw).sort(function (a, b) { if (a === 'none') return 1; if (b === 'none') return -1; return Number(a) - Number(b); });
      if (keys.length) {
        append(body, h('div', { class: 'sub-title', text: 'First wrong step' }));
        C.barChart(body, { orientation: 'v', height: 150, bars: keys.map(function (k) { return { label: k === 'none' ? 'none' : 'step ' + (Number(k) + 1), value: Number(fw[k]) || 0, cls: k === 'none' ? 'c-st-done' : 'c-st-failed' }; }) });
      }
    }, function () { return [{ metric: 'precision', value: ch.precision }, { metric: 'recall', value: ch.recall }, { metric: 'f1', value: ch.f1 }, { metric: 'order', value: ch.order }, rateRow('exact', ch.exact)].concat(Object.keys(fw).map(function (k) { return { metric: 'first wrong ' + k, value: fw[k] }; })); });

    var byPos = arr(ch.by_position).map(obj);
    var nPos = byPos.reduce(function (a, p) { return a + (Number(p.n) || 0); }, 0);
    chartCard(grid, 'Step accuracy by position', nPos, function (body) {
      var bars = [];
      for (var i = 1; i <= 8; i++) {
        var p = byPos.filter(function (q) { return Number(q.position) === i; })[0] || { n: 0, k: 0 };
        bars.push({ label: String(i), value: isNum(p.rate) ? p.rate : 0, low: p.n ? p.low : undefined, high: p.n ? p.high : undefined, cls: 'c-accent', valueText: p.n ? pct(p.rate) : '\u2013', sub: (p.k || 0) + '/' + (p.n || 0), tip: 'position ' + i + ': ' + fmtRate(p) });
      }
      C.barChart(body, { orientation: 'v', bars: bars, max: 1, height: 220, subRow: true });
    }, function () { return byPos.map(function (p) { return rateRow('position ' + p.position, p); }); });

    var rc = arr(m.risk_coverage).map(obj);
    var nRc = rc.length ? Math.max.apply(null, rc.map(function (r) { return Number(obj(r.coverage).n) || 0; })) : 0;
    chartCard(grid, 'Risk\u2013coverage', nRc, function (body) {
      C.lineChart(body, { points: rc.map(function (r) { return { x: obj(r.coverage).rate, y: obj(r.risk).rate, label: 't\u2265' + fix(r.threshold, 2), sub: 'n ' + (Number(obj(r.coverage).n) || 0) }; }), xLabel: 'coverage', yLabel: 'risk', cls: 'c-st-failed' });
    }, function () { return rc.map(function (r) { return { threshold: r.threshold, coverage: obj(r.coverage).rate, precision: obj(r.precision).rate, risk: obj(r.risk).rate, n: obj(r.coverage).n }; }); });

    var cal = obj(m.calibration);
    chartCard(grid, 'Calibration', Number(cal.n) || 0, function (body) {
      append(body, hero(fix(cal.ece, 3), 'expected calibration error'));
      C.reliabilityChart(body, { buckets: arr(cal.buckets).map(obj) });
    }, function () { return arr(cal.buckets).map(function (b) { b = obj(b); return { score: b.score, n: b.n, accuracy: obj(b.accuracy).rate, low: obj(b.accuracy).low, high: obj(b.accuracy).high }; }); });

    var pk = obj(m.pass_k), kp = obj(m.kappa);
    var hasPk = all && ['1', '2', '3'].some(function (k) { return isNum(obj(pk[k]).value); });
    var hasKappa = isNum(kp.kappa);
    chartCard(grid, 'Consistency', (hasPk || hasKappa) ? (Number(obj(pk['1']).runs) || Number(kp.n) || 1) : 0, function (body) {
      var tiles = [];
      ['1', '2', '3'].forEach(function (k) { var p = obj(pk[k]); tiles.push(['pass^' + k, isNum(p.value) ? fix(p.value, 2) : (all ? 'needs a repeated run' : 'all runs only'), (p.groups || 0) + ' groups, ' + (p.runs || 0) + ' runs']); });
      tiles.push(['kappa', hasKappa ? fix(kp.kappa, 2) : 'needs a second labeller', hasKappa ? 'po ' + fix(kp.po, 2) + ', pe ' + fix(kp.pe, 2) + ', n ' + sid(kp.n) + ' \u00b7 ' + arr(kp.labellers).join(' vs ') : null]);
      append(body, tileRow(tiles));
      if (hasKappa) append(body, h('p', { class: 'muted', text: 'Labellers: ' + arr(kp.labellers).join(' and ') }));
      var groups = arr(pk.groups).map(obj);
      if (groups.length) append(body, h('p', { class: 'muted', text: groups.length + ' repeated comment group' + (groups.length === 1 ? '' : 's') + ' across runs.' }));
    }, function () { return ['1', '2', '3'].map(function (k) { return Object.assign({ metric: 'pass^' + k }, obj(pk[k])); }).concat([{ metric: 'kappa', value: kp.kappa, po: kp.po, pe: kp.pe, n: kp.n }]); }, { alwaysDraw: true });

    if (all && arr(m.reviews).length) {
      chartCard(grid, 'Reviews', arr(m.reviews).length, function (body) {
        C.barChart(body, { bars: arr(m.reviews).map(function (r) { r = obj(r); return rateBar(sid(r.id), r.precision, 'c-st-done'); }), max: 1, labelWidth: 160, valueWidth: 90 });
      }, function () { return arr(m.reviews).map(function (r) { r = obj(r); return { id: r.id, created: fmtDate(r.created), comments: r.comments, labelled: r.labelled, precision: obj(r.precision).rate }; }); }, { wide: true });
    }

    var pc = obj(m.per_comment);
    if (Object.keys(pc).length) {
      chartCard(grid, 'Per comment', Object.keys(pc).length, function (body) {
        body.appendChild(C.tableFor(Object.keys(pc).map(function (id) {
          var r = obj(pc[id]);
          return { id: id, verdict: r.verdict, labeller: r.labeller, status: r.status, attempted: r.attempted ? 'yes' : 'no', 'class': r.action_class, tier: r.tier, pred: arr(r.pred).join(' \u2192 '), gold: Array.isArray(r.gold) ? r.gold.join(' \u2192 ') : null, doability: obj(r.doability).score };
        })));
      }, [], { wide: true, alwaysDraw: true });
    }
  }

  /* ---- transcription view (kind ocr) ----------------------------------- */

  function renderOcr() {
    var pane = $('#ocrPane');
    clear(pane);
    var warns = arr(S.ix.report.recall_warnings).map(obj);
    if (warns.length) {
      append(pane, h('div', { class: 'ocr-warn' }, h('div', { class: 'sub-title', text: warns.length + ' recall warning' + (warns.length > 1 ? 's' : '') }),
        h('ul', null, warns.map(function (w) { return h('li', { text: 'Page ' + sid(w.page_no) + ' ' + sid(w.kind) + ': ' + sid(w.detail) }); }))));
    }
    if (!S.ix.order.length) { append(pane, h('p', { class: 'empty', text: 'No transcribed comments.' })); return; }
    var rows = S.ix.order.map(function (id) {
      var c = S.ix.byId[id];
      var tr = h('tr', { dataset: { id: id }, class: id === S.selected ? 'sel' : '', onclick: function (ev) { if (!ev.target.closest('button')) selectCard(id, false); } },
        h('td', { class: 'num', text: '#' + S.ix.num[id] + ' ' + id }),
        h('td', null, cropEl(c)),
        h('td', { class: 'otext', text: sid(c.text) }),
        h('td', { text: sid(c.kind) }),
        h('td', { class: 'num', text: fix(c.confidence, 2) }),
        h('td', { class: 'ocr-v' }));
      refreshOcrRow(tr);
      return tr;
    });
    append(pane, h('table', { class: 'ocr' },
      h('thead', null, h('tr', null, ['id', 'crop', 'text', 'kind', 'confidence', 'verdict'].map(function (t) { return h('th', { text: t }); }))),
      h('tbody', null, rows)));
  }

  function refreshOcrRow(tr) {
    var id = tr.dataset.id, cell = $('.ocr-v', tr);
    if (!cell) return;
    clear(cell);
    var lab = shownLabel(id), cur = lab ? sid(lab.verdict) : '';
    append(cell, h('div', { class: 'seg' }, VERDICTS.map(function (v) {
      return h('button', { type: 'button', class: 'v v-' + v[0] + (cur === v[0] ? ' on' : ''), dataset: { v: v[0] }, text: v[1], onclick: function () { ocrVerdict(id, v[0]); } });
    })));
    if (lab && lab.verdict) append(cell, h('div', { class: 'muted small', text: sid(lab.labeller) + ' \u00b7 ' + fmtTime(lab.updated) }));
  }

  function ocrVerdict(id, verdict) {
    if (!requireLabeller()) return;
    var cur = shownLabel(id) ? sid(shownLabel(id).verdict) : '';
    var v = cur === verdict ? null : verdict;
    scheduleSave(id, { labeller: S.labeller, verdict: v, tools: null, note: '' });
    S.lastBodies[id] = { labeller: S.labeller, verdict: v, tools: null, note: '' };
  }

  document.addEventListener('DOMContentLoaded', function () {
    boot().catch(function (e) { banner('The page failed to start: ' + (e && e.message || e)); });
  });
})();
