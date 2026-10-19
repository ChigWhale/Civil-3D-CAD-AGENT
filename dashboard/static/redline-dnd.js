/* Redline review: drag and drop of tool-name blocks between the library and a tray.
   Exposes window.RedlineDnd = { wireDnd, makeChip, chainOf }.

   wireDnd(root, { onChange(trayEl), onAdd(name), targetTray() }) wires one root (pointer events, clicks, keys).
   A block is any .blk element with data-tool; one that sits inside a .tray is a tray chip, any other is a
   library block. Blocks carry tool NAMES only, nothing else is ever read from them. */
(function () {
  'use strict';

  var DRAG_START_PX = 5;

  function binClassOf(elm) {
    if (!elm || !elm.classList) return 'bin-other';
    for (var i = 0; i < elm.classList.length; i++) {
      if (elm.classList[i].indexOf('bin-') === 0) return elm.classList[i];
    }
    return 'bin-other';
  }

  function makeChip(name, bin) {
    var chip = document.createElement('span');
    var binCls = bin ? (bin.indexOf('bin-') === 0 ? bin : 'bin-' + bin) : 'bin-other';
    chip.className = 'blk ' + binCls;
    chip.dataset.tool = String(name);
    chip.tabIndex = 0;
    chip.setAttribute('role', 'listitem');
    chip.appendChild(document.createTextNode(String(name)));
    var x = document.createElement('button');
    x.type = 'button';
    x.className = 'x';
    x.tabIndex = -1;
    x.setAttribute('aria-label', 'remove ' + name);
    x.title = 'remove';
    x.textContent = '×';
    chip.appendChild(x);
    return chip;
  }

  function chainOf(tray) {
    if (!tray) return [];
    return Array.prototype.map.call(tray.querySelectorAll('.blk'), function (b) { return b.dataset.tool; });
  }

  function trayChips(tray, except) {
    return Array.prototype.filter.call(tray.querySelectorAll('.blk'), function (c) { return c !== except; });
  }

  /* Insertion index: chips whose row lies above the pointer, or whose horizontal midpoint is left of it
     on the same row, come before the dropped block. */
  function insertionIndex(tray, except, x, y) {
    var chips = trayChips(tray, except);
    var idx = 0;
    for (var i = 0; i < chips.length; i++) {
      var r = chips[i].getBoundingClientRect();
      if (r.bottom <= y) { idx = i + 1; continue; }
      if (r.top > y) break;
      if ((r.left + r.right) / 2 <= x) idx = i + 1;
    }
    return idx;
  }

  function liveRegion() {
    var live = document.getElementById('dndLive');
    if (!live) {
      live = document.createElement('div');
      live.id = 'dndLive';
      live.className = 'sr-only';
      live.setAttribute('aria-live', 'polite');
      document.body.appendChild(live);
    }
    return live;
  }

  function announce(msg) {
    var live = liveRegion();
    live.textContent = '';
    setTimeout(function () { live.textContent = msg; }, 20);
  }

  function wireDnd(root, opts) {
    var o = opts || {};
    var onChange = typeof o.onChange === 'function' ? o.onChange : function () {};
    var onAdd = typeof o.onAdd === 'function' ? o.onAdd : function () {};
    var targetTray = typeof o.targetTray === 'function' ? o.targetTray : function () { return root.querySelector('.tray'); };
    var drag = null;
    var suppressClick = false;

    function caretFor(tray) {
      var c = tray.querySelector('.caret');
      if (!c) {
        c = document.createElement('span');
        c.className = 'caret';
        c.setAttribute('aria-hidden', 'true');
      }
      return c;
    }

    function removeCarets() {
      Array.prototype.forEach.call(root.querySelectorAll('.caret'), function (c) { c.remove(); });
    }

    function placeCaret(tray, except, idx) {
      var caret = caretFor(tray);
      var chips = trayChips(tray, except);
      var ref = chips[idx] || null;
      if (caret.parentNode === tray && caret.nextSibling === ref) return;
      tray.insertBefore(caret, ref);
    }

    function positionOf(name, tray) {
      var chips = tray.querySelectorAll('.blk');
      for (var i = 0; i < chips.length; i++) if (chips[i] === name) return i + 1;
      return chips.length;
    }

    function cleanup() {
      if (!drag) return;
      var d = drag;
      drag = null;
      removeCarets();
      if (d.ghost && d.ghost.parentNode) d.ghost.parentNode.removeChild(d.ghost);
      if (d.src) {
        d.src.classList.remove('placeholder');
        d.src.removeEventListener('pointermove', onMove);
        d.src.removeEventListener('pointerup', onUp);
        d.src.removeEventListener('pointercancel', onCancel);
        try { d.src.releasePointerCapture(d.pointerId); } catch (e) { /* already released */ }
      }
      delete document.body.dataset.dragging;
      document.removeEventListener('keydown', onEscape, true);
    }

    function onEscape(ev) {
      if (ev.key === 'Escape' && drag) {
        ev.preventDefault();
        var wasActive = drag.active;
        cleanup();
        if (wasActive) { suppressClick = true; announce('drag cancelled'); }
      }
    }

    function onDown(ev) {
      if (ev.button !== undefined && ev.button !== 0) return;
      if (ev.target.closest('.x')) return;
      var blk = ev.target.closest('.blk');
      if (!blk || !root.contains(blk) || !blk.dataset.tool) return;
      if (blk.classList.contains('static')) return;
      var tray = blk.closest('.tray');
      drag = {
        src: blk, fromTray: !!tray, originTray: tray, pointerId: ev.pointerId,
        startX: ev.clientX, startY: ev.clientY, active: false, ghost: null, overTray: null, index: 0
      };
      try { blk.setPointerCapture(ev.pointerId); } catch (e) { /* capture unsupported */ }
      blk.addEventListener('pointermove', onMove);
      blk.addEventListener('pointerup', onUp);
      blk.addEventListener('pointercancel', onCancel);
      document.addEventListener('keydown', onEscape, true);
    }

    function startDrag(ev) {
      var d = drag;
      var ghost = d.src.cloneNode(true);
      ghost.classList.add('ghost');
      ghost.classList.remove('placeholder');
      ghost.removeAttribute('tabindex');
      var r = d.src.getBoundingClientRect();
      d.offX = ev.clientX - r.left;
      d.offY = ev.clientY - r.top;
      ghost.style.width = r.width + 'px';
      document.body.appendChild(ghost);
      d.ghost = ghost;
      d.active = true;
      if (d.fromTray) d.src.classList.add('placeholder');
      document.body.dataset.dragging = d.fromTray ? 'tray' : 'library';
    }

    function onMove(ev) {
      var d = drag;
      if (!d) return;
      if (!d.active) {
        var dx = ev.clientX - d.startX, dy = ev.clientY - d.startY;
        if (Math.sqrt(dx * dx + dy * dy) < DRAG_START_PX) return;
        startDrag(ev);
      }
      ev.preventDefault();
      d.ghost.style.left = (ev.clientX - d.offX) + 'px';
      d.ghost.style.top = (ev.clientY - d.offY) + 'px';
      var under = document.elementFromPoint(ev.clientX, ev.clientY);
      var tray = under && under.closest ? under.closest('.tray') : null;
      if (tray && !root.contains(tray)) tray = null;
      d.overTray = tray;
      if (tray) {
        d.ghost.classList.remove('remove');
        d.index = insertionIndex(tray, d.src, ev.clientX, ev.clientY);
        placeCaret(tray, d.src, d.index);
      } else {
        removeCarets();
        if (d.fromTray) d.ghost.classList.add('remove');
      }
    }

    function onUp(ev) {
      var d = drag;
      if (!d) return;
      if (!d.active) { cleanup(); return; }
      ev.preventDefault();
      suppressClick = true;
      var tray = d.overTray, src = d.src, fromTray = d.fromTray, origin = d.originTray, idx = d.index;
      var name = src.dataset.tool;
      var binCls = binClassOf(src);
      cleanup();
      if (tray) {
        var ref = trayChips(tray, src)[idx] || null;
        if (fromTray) {
          tray.insertBefore(src, ref);
          announce(name + ' moved to step ' + positionOf(src, tray));
          if (origin && origin !== tray) onChange(origin);
          onChange(tray);
        } else {
          var chip = makeChip(name, binCls);
          tray.insertBefore(chip, ref);
          announce(name + ' added at step ' + positionOf(chip, tray));
          onAdd(name);
          onChange(tray);
        }
      } else if (fromTray && origin) {
        src.remove();
        announce(name + ' removed');
        onChange(origin);
      }
    }

    function onCancel() {
      var wasActive = drag && drag.active;
      cleanup();
      if (wasActive) suppressClick = true;
    }

    function appendTo(tray, name, binCls) {
      var chip = makeChip(name, binCls);
      tray.appendChild(chip);
      announce(name + ' added at step ' + positionOf(chip, tray));
      onAdd(name);
      onChange(tray);
      return chip;
    }

    function onClick(ev) {
      if (suppressClick) { suppressClick = false; ev.preventDefault(); ev.stopPropagation(); return; }
      var x = ev.target.closest('.x');
      if (x) {
        var chip = x.closest('.blk');
        var tray = chip && chip.closest('.tray');
        if (chip && tray) {
          var nm = chip.dataset.tool;
          var next = chip.nextElementSibling || chip.previousElementSibling;
          chip.remove();
          announce(nm + ' removed');
          if (next && next.classList.contains('blk')) next.focus();
          onChange(tray);
        }
        ev.preventDefault();
        return;
      }
      var blk = ev.target.closest('.blk');
      if (!blk || !root.contains(blk) || !blk.dataset.tool) return;
      if (blk.closest('.tray')) return;
      if (blk.classList.contains('static')) return;
      var target = targetTray();
      if (!target) { announce('select a comment first'); return; }
      appendTo(target, blk.dataset.tool, binClassOf(blk));
    }

    function onKey(ev) {
      var chip = ev.target.closest ? ev.target.closest('.blk') : null;
      if (!chip || !root.contains(chip)) return;
      var tray = chip.closest('.tray');
      if (!tray) return;
      var name = chip.dataset.tool;
      if (ev.key === 'ArrowLeft') {
        var prev = chip.previousElementSibling;
        while (prev && !prev.classList.contains('blk')) prev = prev.previousElementSibling;
        if (prev) { tray.insertBefore(chip, prev); chip.focus(); announce(name + ' moved to step ' + positionOf(chip, tray)); onChange(tray); }
        ev.preventDefault();
      } else if (ev.key === 'ArrowRight') {
        var nxt = chip.nextElementSibling;
        while (nxt && !nxt.classList.contains('blk')) nxt = nxt.nextElementSibling;
        if (nxt) { tray.insertBefore(nxt, chip); chip.focus(); announce(name + ' moved to step ' + positionOf(chip, tray)); onChange(tray); }
        ev.preventDefault();
      } else if (ev.key === 'Delete' || ev.key === 'Backspace') {
        var neighbour = chip.nextElementSibling || chip.previousElementSibling;
        chip.remove();
        announce(name + ' removed');
        if (neighbour && neighbour.classList.contains('blk')) neighbour.focus(); else tray.focus();
        onChange(tray);
        ev.preventDefault();
      }
    }

    root.addEventListener('pointerdown', onDown);
    root.addEventListener('click', onClick);
    root.addEventListener('keydown', onKey);
    liveRegion();

    return {
      append: function (name, binCls) {
        var t = targetTray();
        return t ? appendTo(t, name, binCls) : null;
      },
      cancel: cleanup
    };
  }

  window.RedlineDnd = { wireDnd: wireDnd, makeChip: makeChip, chainOf: chainOf };
})();
