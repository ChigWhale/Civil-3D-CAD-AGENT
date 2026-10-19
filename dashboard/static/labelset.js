/* Label set page: type a comment, drag tool-name blocks into the chain that addresses it, save.
   Depends on redline-dnd.js (window.RedlineDnd). No metrics. Blocks carry tool NAMES only;
   nothing here shows or asks for a parameter. */
(function () {
  'use strict';

  var API = 'redline/labelset';
  var DEFAULT_BINS = ['lookup', 'draw', 'modify', 'text', 'layer', 'block', 'sheet', 'civil'];
  var D = window.RedlineDnd;

  var S = {
    payload: null, labeller: '', editing: null, recent: [], libBin: 'recent', libOpen: true, libSearch: '',
    filter: '', dnd: null, badgeTimer: null
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

  async function request(url, options) {
    var r;
    try { r = await fetch(url, Object.assign({ cache: 'no-store' }, options || {})); }
    catch (e) { return { ok: false, status: 0, data: null, network: true, url: url, error: String(e && e.message || e) }; }
    var data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    return { ok: r.ok, status: r.status, data: data, url: url };
  }

  function banner(text, kind) {
    var b = $('#banner');
    clear(b);
    if (!text) { b.hidden = true; return; }
    b.className = 'banner ' + (kind || 'err');
    append(b, [h('span', { text: text }), h('button', { type: 'button', class: 'x', text: '×', title: 'dismiss', onclick: function () { b.hidden = true; } })]);
    b.hidden = false;
  }

  function showMessage(text) {
    var m = $('#message');
    clear(m);
    append(m, h('p', { text: text }));
    m.hidden = false;
    $$('.view').forEach(function (v) { v.hidden = true; });
  }

  function tools() { return obj(S.payload && S.payload.tools); }
  function binOf(name) { return sid(obj(tools().bins)[name]) || 'other'; }
  function binLabel(b) { return sid(obj(tools().labels)[b]) || b; }
  function examples() { return arr(S.payload && S.payload.examples).map(obj); }
  function tray() { return $('#tray'); }

  /* ---- start ------------------------------------------------------------ */

  function init() {
    S.labeller = sid(lsGet('redline.labeller', '')) || '';
    S.recent = arr(lsGet('labelset.recent', [])).map(sid);
    $('#labeller').value = S.labeller;
    $('#labeller').addEventListener('change', function () { S.labeller = $('#labeller').value.trim(); lsSet('redline.labeller', S.labeller); });
    $('#saveBtn').addEventListener('click', saveExample);
    $('#clearBtn').addEventListener('click', function () { clearEditor(); $('#text').focus(); });
    $('#cancelBtn').addEventListener('click', cancelEdit);
    $('#exportBtn').addEventListener('click', exportJsonl);
    $('#discardBtn').addEventListener('click', discardAll);
    $('#copyBtn').addEventListener('click', copyFolder);
    $('#filter').addEventListener('input', function (ev) { S.filter = ev.target.value.trim().toLowerCase(); renderList(); });
    $('#text').addEventListener('input', updateSaveState);
    S.dnd = D.wireDnd($('#editorPane'), { onChange: onTrayChange, onAdd: rememberRecent, targetTray: tray });
    wireKeys();
    load();
  }

  async function load() {
    var res = await request(API);
    if (res.network) { showMessage('Could not reach the dashboard at ' + location.origin + '. Start it with: python launcher.py start --no-browser'); return; }
    if (!res.ok) { showMessage('The dashboard answered ' + res.status + ' for the label set; it may be an older version. Restart it with: python launcher.py stop, then python launcher.py start --no-browser'); return; }
    S.payload = obj(res.data);
    if (!S.labeller && S.payload.whoami) { S.labeller = sid(S.payload.whoami); $('#labeller').value = S.labeller; }
    renderAll();
    $('#text').focus();
  }

  function applyPayload(data) {
    data = obj(data);
    if (Array.isArray(data.examples)) S.payload = Object.assign({}, S.payload, data);
    renderChips();
    renderList();
  }

  function renderAll() {
    renderChips();
    renderLibrary();
    renderList();
    updateSaveState();
  }

  function renderChips() {
    var p = obj(S.payload), chips = $('#chips');
    clear(chips);
    append(chips, [
      h('span', { class: 'chip', text: arr(tools().names).length + ' tools' + (p.tools_version ? ' · ' + sid(p.tools_version).slice(0, 8) : ''), title: 'the tool names the blocks come from' }),
      h('span', { class: 'chip', text: basename(p.jsonl) || 'labelset.jsonl', title: 'rewritten on every save: ' + sid(p.jsonl) }),
      h('span', { class: 'chip', text: basename(p.folder) || '', title: sid(p.folder) })
    ]);
    var n = examples().length;
    $('#countChip').textContent = n + ' example' + (n === 1 ? '' : 's');
    $('#listTitle').textContent = 'Saved examples (' + n + ')';
    $('#discardBtn').disabled = !n;
    $('#exportBtn').disabled = !n;
  }

  /* ---- the editor ------------------------------------------------------- */

  function setBadge(state, retry) {
    var b = $('#edBadge');
    clear(b);
    clearTimeout(S.badgeTimer);
    b.className = 'badge' + (state === 'saved' ? ' saved' : state === 'failed' ? ' failed' : '');
    if (state === 'saving') append(b, 'saving…');
    else if (state === 'saved') { append(b, 'saved ✓'); S.badgeTimer = setTimeout(function () { clear(b); }, 2500); }
    else if (state === 'failed') append(b, ['failed, ', h('button', { type: 'button', class: 'link', text: 'retry', onclick: retry })]);
  }

  function updateSaveState() {
    var ok = $('#text').value.trim().length > 0 && D.chainOf(tray()).length > 0;
    $('#saveBtn').disabled = !ok;
    $('#saveBtn').title = ok ? '' : 'Type the comment and put at least one block in the chain';
  }

  function onTrayChange() { updateSaveState(); }

  function setChain(names) {
    var t = tray();
    clear(t);
    names.forEach(function (nm) { t.appendChild(D.makeChip(nm, binOf(nm))); });
    updateSaveState();
  }

  function clearEditor() {
    S.editing = null;
    $('#text').value = '';
    setChain([]);
    $('#edTitle').textContent = 'New example';
    $('#edTitle').classList.remove('editing');
    $('#cancelBtn').hidden = true;
    $('#saveBtn').textContent = 'Save example';
    setBadge('');
    $$('.ex.editing').forEach(function (e) { e.classList.remove('editing'); });
  }

  function startEdit(ex) {
    S.editing = sid(ex.id);
    $('#text').value = sid(ex.text);
    setChain(arr(ex.tools).map(sid));
    $('#edTitle').textContent = 'Editing ' + S.editing;
    $('#edTitle').classList.add('editing');
    $('#cancelBtn').hidden = false;
    $('#saveBtn').textContent = 'Save changes';
    setBadge('');
    $$('.ex').forEach(function (e) { e.classList.toggle('editing', e.dataset.id === S.editing); });
    $('#text').focus();
  }

  function cancelEdit() { clearEditor(); $('#text').focus(); }

  function ensureLabeller() {
    S.labeller = $('#labeller').value.trim();
    if (S.labeller) { lsSet('redline.labeller', S.labeller); return true; }
    banner('Enter a labeller name (top right) before saving.', 'warn');
    $('#labeller').focus();
    return false;
  }

  async function saveExample() {
    var text = $('#text').value.trim(), chain = D.chainOf(tray());
    if (!text) { banner('Type the comment first.', 'warn'); $('#text').focus(); return; }
    if (!chain.length) { banner('Put at least one block in the chain: drag one from the library, or click it.', 'warn'); return; }
    if (!ensureLabeller()) return;
    banner('');
    var editing = S.editing;
    var body = { text: text, tools: chain, labeller: S.labeller, source: 'typed' };
    setBadge('saving');
    $('#saveBtn').disabled = true;
    var res = await request(editing ? API + '/examples/' + encodeURIComponent(editing) : API + '/examples', {
      method: editing ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
    if (!res.ok) {
      setBadge('failed', saveExample);
      updateSaveState();
      banner('Save failed: ' + (obj(res.data).error || res.status || res.error), 'err');
      return;
    }
    applyPayload(res.data);
    clearEditor();
    setBadge('saved');
    $('#text').focus();
  }

  async function deleteExample(id) {
    if (!window.confirm('Delete ' + id + '? This cannot be undone.')) return;
    var res = await request(API + '/examples/' + encodeURIComponent(id), { method: 'DELETE' });
    if (!res.ok) { banner('Delete failed: ' + (obj(res.data).error || res.status || res.error), 'err'); return; }
    if (S.editing === id) clearEditor();
    applyPayload(res.data);
  }

  async function discardAll() {
    var n = examples().length;
    if (!n) return;
    if (!window.confirm('Discard all ' + n + ' example' + (n === 1 ? '' : 's') + '? This removes labelset.json and labelset.jsonl and cannot be undone.')) return;
    var res = await request(API, { method: 'DELETE' });
    if (!res.ok) { banner('Discard failed: ' + (obj(res.data).error || res.status || res.error), 'err'); return; }
    clearEditor();
    applyPayload(res.data);
    banner('Discarded ' + sid(obj(res.data).discarded) + ' example(s).', 'warn');
  }

  function exportJsonl() {
    var a = h('a', { href: API + '/export', download: 'labelset.jsonl' });
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  function copyFolder() {
    var p = sid(obj(S.payload).folder);
    if (!p) return;
    var done = function () { banner('Copied: ' + p, 'warn'); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(p).then(done, function () { window.prompt('Folder path', p); });
    else window.prompt('Folder path', p);
  }

  /* ---- the saved examples ---------------------------------------------- */

  function matches(ex) {
    if (!S.filter) return true;
    var hay = (sid(ex.text) + ' ' + arr(ex.tools).join(' ') + ' ' + sid(ex.labeller) + ' ' + sid(ex.id)).toLowerCase();
    return hay.indexOf(S.filter) >= 0;
  }

  function chainEl(names) {
    var el = h('div', { class: 'ex-chain' });
    names.forEach(function (nm, i) {
      if (i) append(el, h('span', { class: 'arrow', text: '→' }));
      append(el, h('span', { class: 'blk static bin-' + binOf(nm), text: nm, title: sid(obj(tools().descriptions)[nm]) || nm }));
    });
    return el;
  }

  function exampleCard(ex) {
    var id = sid(ex.id);
    return h('div', { class: 'ex' + (S.editing === id ? ' editing' : ''), dataset: { id: id } },
      h('div', { class: 'ex-head' },
        h('span', { class: 'id', text: id }),
        h('span', { text: sid(ex.labeller) }),
        h('span', { text: fmtDate(ex.updated), title: 'created ' + fmtDate(ex.created) }),
        ex.source && ex.source !== 'typed' ? h('span', { class: 'pill', text: sid(ex.source) }) : null,
        h('span', { class: 'btns' },
          h('button', { type: 'button', class: 'ghost-btn small', text: 'Edit', onclick: function () { startEdit(ex); } }),
          h('button', { type: 'button', class: 'ghost-btn small', text: 'Delete', onclick: function () { deleteExample(id); } }))),
      h('div', { class: 'ex-text', text: sid(ex.text) }),
      chainEl(arr(ex.tools).map(sid)),
      ex.note ? h('div', { class: 'ex-note', text: sid(ex.note) }) : null);
  }

  function renderList() {
    var list = $('#examples');
    clear(list);
    var all = examples(), shown = all.filter(matches);
    if (!all.length) {
      append(list, h('p', { class: 'empty', text: 'No examples yet. Type a comment on the left, drag blocks into its chain, and press Save.' }));
      return;
    }
    if (!shown.length) { append(list, h('p', { class: 'empty', text: 'No example matches “' + S.filter + '”.' })); return; }
    shown.forEach(function (ex) { append(list, exampleCard(ex)); });
  }

  /* ---- block library (same as the results window) ----------------------- */

  function rememberRecent(name) {
    S.recent = [name].concat(S.recent.filter(function (n) { return n !== name; })).slice(0, 8);
    lsSet('labelset.recent', S.recent);
    if (S.libBin === 'recent' && !S.libSearch) renderLibraryList();
  }

  function libraryGroups() {
    var names = arr(tools().names).map(sid), groups = {};
    names.forEach(function (n) { var b = binOf(n); (groups[b] = groups[b] || []).push(n); });
    Object.keys(groups).forEach(function (b) { groups[b].sort(); });
    return groups;
  }

  function binOrder() {
    var order = arr(tools().bin_order).map(sid);
    if (!order.length) order = DEFAULT_BINS.slice();
    var groups = libraryGroups();
    Object.keys(groups).forEach(function (b) { if (order.indexOf(b) < 0) order.push(b); });
    return order;
  }

  function renderLibrary() {
    var lib = $('#library');
    clear(lib);
    lib.classList.toggle('closed', !S.libOpen);
    var head = h('div', { class: 'lib-head', onclick: function (ev) { if (ev.target.closest('input')) return; S.libOpen = !S.libOpen; lib.classList.toggle('closed', !S.libOpen); } },
      h('span', { class: 'lib-title', text: 'Block library' }),
      h('span', { class: 'muted', text: arr(tools().names).length + ' tools' }),
      h('input', { type: 'search', class: 'lib-search', id: 'libSearch', placeholder: 'search tools  ( / )', value: S.libSearch, spellcheck: 'false',
        oninput: function (ev) { S.libSearch = ev.target.value.trim().toLowerCase(); renderLibraryList(); },
        onkeydown: function (ev) { if (ev.key === 'Escape') { ev.target.value = ''; S.libSearch = ''; renderLibraryList(); ev.target.blur(); ev.stopPropagation(); } } }),
      h('span', { class: 'lib-toggle', text: S.libOpen ? '▾' : '▴' }));
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
      if (!any) append(list, h('p', { class: 'muted', text: 'No tool matches “' + S.libSearch + '”.' }));
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
      if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); if (!$('#saveBtn').disabled) saveExample(); return; }
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
      var t = ev.target, tag = t && t.tagName ? t.tagName.toLowerCase() : '';
      var typing = tag === 'input' || tag === 'textarea' || tag === 'select' || (t && t.isContentEditable);
      if (ev.key === '/' && !typing) {
        var s = $('#libSearch');
        if (s) { ev.preventDefault(); if (!S.libOpen) { S.libOpen = true; $('#library').classList.remove('closed'); } s.focus(); s.select(); }
        return;
      }
      if (ev.key === 'Escape' && S.editing && tag !== 'input') { ev.preventDefault(); cancelEdit(); }
    });
  }

  document.addEventListener('DOMContentLoaded', init);
})();
