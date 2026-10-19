/* Redline review: small SVG charts, no libraries.
   Exposes window.RedlineCharts = { el, barChart, lineChart, reliabilityChart, segmentRow, tableFor, bindTips, fmtPct }.
   Colours come from CSS classes (.c-bin-*, .c-st-*, .c-tier-*, .c-accent); the shapes only carry class names. */
(function () {
  'use strict';

  var NS = 'http://www.w3.org/2000/svg';

  function el(tag, attrs, children) {
    var e = document.createElementNS(NS, tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined) return;
        if (k === 'text') e.textContent = String(v);
        else e.setAttribute(k, String(v));
      });
    }
    if (children) {
      (Array.isArray(children) ? children : [children]).forEach(function (c) {
        if (c) e.appendChild(c);
      });
    }
    return e;
  }

  function fmtPct(v) {
    return (typeof v === 'number' && isFinite(v)) ? Math.round(v * 100) + '%' : '—';
  }

  function fmtNum(v) {
    if (typeof v !== 'number' || !isFinite(v)) return v === null || v === undefined ? '—' : String(v);
    if (Math.abs(v - Math.round(v)) < 1e-9) return String(Math.round(v));
    return v.toFixed(2);
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function widthOf(container, fallback) {
    var w = container && container.clientWidth ? container.clientWidth : (fallback || 420);
    return Math.max(240, Math.floor(w));
  }

  function shortLabel(s, n) {
    s = String(s === undefined || s === null ? '' : s);
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  /* ---- one shared tooltip --------------------------------------------- */

  function tipEl() {
    var t = document.getElementById('chart-tip');
    if (!t) {
      t = document.createElement('div');
      t.id = 'chart-tip';
      t.hidden = true;
      document.body.appendChild(t);
    }
    return t;
  }

  function placeTip(ev) {
    var t = tipEl();
    var x = ev.clientX + 12, y = ev.clientY + 14;
    var w = t.offsetWidth || 0, hh = t.offsetHeight || 0;
    if (x + w > window.innerWidth - 8) x = ev.clientX - w - 12;
    if (y + hh > window.innerHeight - 8) y = ev.clientY - hh - 12;
    t.style.left = Math.max(4, x) + 'px';
    t.style.top = Math.max(4, y) + 'px';
  }

  function bindTips(svg) {
    svg.addEventListener('mousemove', function (ev) {
      var target = ev.target && ev.target.closest ? ev.target.closest('[data-tip]') : null;
      var t = tipEl();
      if (!target) { t.hidden = true; return; }
      t.textContent = target.getAttribute('data-tip') || '';
      t.hidden = false;
      placeTip(ev);
    });
    svg.addEventListener('mouseleave', function () { tipEl().hidden = true; });
    return svg;
  }

  /* ---- bars ------------------------------------------------------------ */

  function barTotal(b) {
    if (b && Array.isArray(b.segments)) {
      return b.segments.reduce(function (a, s) { return a + (Number(s.value) || 0); }, 0);
    }
    return Number(b && b.value) || 0;
  }

  function barChart(container, opts) {
    var o = opts || {};
    return o.orientation === 'v' ? vBars(container, o) : hBars(container, o);
  }

  function hBars(container, o) {
    var bars = Array.isArray(o.bars) ? o.bars : [];
    var W = widthOf(container, o.width);
    var rowH = o.rowH || 26, labelW = o.labelWidth || 120, valueW = o.valueWidth || 70, pad = 8;
    var H = Math.max(rowH, bars.length * rowH) + pad * 2;
    var max = o.max || Math.max(1e-9, Math.max.apply(null, bars.map(barTotal).concat([0])));
    var x0 = labelW, x1 = W - valueW;
    var scale = function (v) { return x0 + clamp(v / max, 0, 1) * (x1 - x0); };
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, class: 'chart chart-h' });
    svg.appendChild(el('line', { x1: x0, y1: pad, x2: x0, y2: H - pad, class: 'axis' }));

    bars.forEach(function (b, i) {
      var y = pad + i * rowH, bh = rowH - 10, cy = y + bh / 2;
      var g = el('g', { class: 'row' });
      var lx = x0 - 8;
      if (b.swatchCls) {
        g.appendChild(el('rect', { x: 2, y: cy - 5, width: 10, height: 10, rx: 2, class: 'swatch ' + b.swatchCls }));
      }
      g.appendChild(el('text', {
        x: lx, y: cy + 4, 'text-anchor': 'end', class: 'lbl', text: shortLabel(b.label, 20),
        'data-tip': String(b.label)
      }));
      if (Array.isArray(b.segments)) {
        var acc = 0;
        b.segments.forEach(function (s) {
          var v = Number(s.value) || 0;
          if (v <= 0) return;
          var sx = scale(acc), ex = scale(acc + v);
          g.appendChild(el('rect', {
            x: sx, y: y, width: Math.max(1, ex - sx), height: bh, rx: 2,
            class: 'bar ' + (s.cls || 'c-accent'),
            'data-tip': b.label + ' · ' + (s.label || '') + ': ' + fmtNum(v)
          }));
          acc += v;
        });
      } else {
        var v = Number(b.value) || 0;
        g.appendChild(el('rect', {
          x: scale(0), y: y, width: Math.max(v > 0 ? 2 : 0, scale(v) - scale(0)), height: bh, rx: 3,
          class: 'bar ' + (b.cls || 'c-accent'),
          'data-tip': b.tip || (b.label + ': ' + (b.valueText || fmtNum(v)))
        }));
      }
      if (typeof b.low === 'number' && typeof b.high === 'number') {
        var wl = scale(b.low), wh = scale(b.high);
        g.appendChild(el('line', { x1: wl, y1: cy, x2: wh, y2: cy, class: 'whisker' }));
        g.appendChild(el('line', { x1: wl, y1: cy - 5, x2: wl, y2: cy + 5, class: 'whisker' }));
        g.appendChild(el('line', { x1: wh, y1: cy - 5, x2: wh, y2: cy + 5, class: 'whisker' }));
      }
      var vt = b.valueText !== undefined ? b.valueText : (o.format ? o.format(barTotal(b)) : fmtNum(barTotal(b)));
      g.appendChild(el('text', { x: x1 + 8, y: cy + 4, class: 'val', text: String(vt) }));
      svg.appendChild(g);
    });

    bindTips(svg);
    container.appendChild(svg);
    return svg;
  }

  function vBars(container, o) {
    var bars = Array.isArray(o.bars) ? o.bars : [];
    var W = widthOf(container, o.width);
    var H = o.height || 200;
    var padL = 28, padR = 10, padT = 22, padB = o.subRow ? 44 : 30;
    var plotW = W - padL - padR, plotH = H - padT - padB;
    var n = Math.max(1, bars.length);
    var colW = plotW / n;
    var bw = Math.min(48, colW * 0.6);
    var max = o.max || Math.max(1e-9, Math.max.apply(null, bars.map(barTotal).concat([0])));
    var sy = function (v) { return padT + plotH - clamp(v / max, 0, 1) * plotH; };
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, class: 'chart chart-v' });
    svg.appendChild(el('line', { x1: padL, y1: padT + plotH, x2: W - padR, y2: padT + plotH, class: 'axis' }));

    bars.forEach(function (b, i) {
      var cx = padL + colW * i + colW / 2;
      var v = Number(b.value) || 0;
      var top = sy(v), base = padT + plotH;
      var g = el('g', { class: 'col' });
      g.appendChild(el('rect', {
        x: cx - bw / 2, y: Math.min(top, base - (v > 0 ? 2 : 0)), width: bw, height: Math.max(v > 0 ? 2 : 0, base - top), rx: 3,
        class: 'bar ' + (b.cls || 'c-accent'),
        'data-tip': b.tip || (b.label + ': ' + (b.valueText || fmtNum(v)))
      }));
      if (typeof b.low === 'number' && typeof b.high === 'number') {
        var yl = sy(b.low), yh = sy(b.high);
        g.appendChild(el('line', { x1: cx, y1: yl, x2: cx, y2: yh, class: 'whisker' }));
        g.appendChild(el('line', { x1: cx - 5, y1: yl, x2: cx + 5, y2: yl, class: 'whisker' }));
        g.appendChild(el('line', { x1: cx - 5, y1: yh, x2: cx + 5, y2: yh, class: 'whisker' }));
      }
      var vt = b.valueText !== undefined ? b.valueText : (o.format ? o.format(v) : fmtNum(v));
      g.appendChild(el('text', { x: cx, y: Math.min(top, sy(typeof b.high === 'number' ? b.high : v)) - 6, 'text-anchor': 'middle', class: 'val', text: String(vt) }));
      g.appendChild(el('text', { x: cx, y: base + 14, 'text-anchor': 'middle', class: 'lbl', text: shortLabel(b.label, 10), 'data-tip': String(b.label) }));
      if (b.sub !== undefined && b.sub !== null) {
        g.appendChild(el('text', { x: cx, y: base + 28, 'text-anchor': 'middle', class: 'sub', text: String(b.sub) }));
      }
      svg.appendChild(g);
    });

    bindTips(svg);
    container.appendChild(svg);
    return svg;
  }

  /* ---- line chart (x and y both 0..1 by default) ----------------------- */

  function lineChart(container, opts) {
    var o = opts || {};
    var pts = (Array.isArray(o.points) ? o.points : []).filter(function (p) {
      return typeof p.x === 'number' && typeof p.y === 'number' && isFinite(p.x) && isFinite(p.y);
    }).slice().sort(function (a, b) { return a.x - b.x; });
    var W = widthOf(container, o.width), H = o.height || 240;
    var m = { l: 40, r: 20, t: 16, b: 36 };
    var plotW = W - m.l - m.r, plotH = H - m.t - m.b;
    var xMax = o.xMax || 1, yMax = o.yMax || 1;
    var sx = function (v) { return m.l + clamp(v / xMax, 0, 1) * plotW; };
    var sy = function (v) { return m.t + plotH - clamp(v / yMax, 0, 1) * plotH; };
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, class: 'chart chart-line' });

    svg.appendChild(el('line', { x1: m.l, y1: m.t, x2: m.l, y2: m.t + plotH, class: 'axis' }));
    svg.appendChild(el('line', { x1: m.l, y1: m.t + plotH, x2: m.l + plotW, y2: m.t + plotH, class: 'axis' }));
    [0, 0.5, 1].forEach(function (f) {
      svg.appendChild(el('text', { x: m.l - 6, y: sy(f * yMax) + 4, 'text-anchor': 'end', class: 'tick', text: fmtPct(f) }));
      svg.appendChild(el('text', { x: sx(f * xMax), y: m.t + plotH + 14, 'text-anchor': 'middle', class: 'tick', text: fmtPct(f) }));
    });
    if (o.xLabel) svg.appendChild(el('text', { x: m.l + plotW / 2, y: H - 4, 'text-anchor': 'middle', class: 'sub', text: o.xLabel }));
    if (o.yLabel) {
      svg.appendChild(el('text', { x: 10, y: m.t + plotH / 2, 'text-anchor': 'middle', class: 'sub', transform: 'rotate(-90 10 ' + (m.t + plotH / 2) + ')', text: o.yLabel }));
    }
    if (pts.length > 1) {
      svg.appendChild(el('polyline', {
        points: pts.map(function (p) { return sx(p.x) + ',' + sy(p.y); }).join(' '),
        class: 'line ' + (o.cls || 'c-accent')
      }));
    }
    pts.forEach(function (p) {
      var x = sx(p.x), y = sy(p.y);
      svg.appendChild(el('circle', {
        cx: x, cy: y, r: 4.5, class: 'pt ' + (o.cls || 'c-accent'),
        'data-tip': (p.label ? p.label + ' · ' : '') + 'x ' + fmtPct(p.x) + ', y ' + fmtPct(p.y) + (p.sub ? ' · ' + p.sub : '')
      }));
      if (p.label) {
        var tx = x + 8 > m.l + plotW - 40 ? x - 8 : x + 8;
        svg.appendChild(el('text', { x: tx, y: y - 7, 'text-anchor': x + 8 > m.l + plotW - 40 ? 'end' : 'start', class: 'lbl', text: p.label + (p.sub ? ' (' + p.sub + ')' : '') }));
      }
    });

    bindTips(svg);
    container.appendChild(svg);
    return svg;
  }

  /* ---- reliability diagram -------------------------------------------- */

  function reliabilityChart(container, opts) {
    var o = opts || {};
    var buckets = (Array.isArray(o.buckets) ? o.buckets : []).filter(function (b) { return b && typeof b.score === 'number'; });
    var W = widthOf(container, o.width), H = o.height || 240;
    var m = { l: 40, r: 16, t: 16, b: 40 };
    var plotW = W - m.l - m.r, plotH = H - m.t - m.b;
    var sx = function (v) { return m.l + clamp(v, 0, 1) * plotW; };
    var sy = function (v) { return m.t + plotH - clamp(v, 0, 1) * plotH; };
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, class: 'chart chart-rel' });
    var bw = Math.max(10, Math.min(40, plotW / Math.max(1, buckets.length + 1) * 0.8));

    svg.appendChild(el('line', { x1: m.l, y1: m.t, x2: m.l, y2: m.t + plotH, class: 'axis' }));
    svg.appendChild(el('line', { x1: m.l, y1: m.t + plotH, x2: m.l + plotW, y2: m.t + plotH, class: 'axis' }));
    svg.appendChild(el('line', { x1: sx(0), y1: sy(0), x2: sx(1), y2: sy(1), class: 'diag' }));
    [0, 0.5, 1].forEach(function (f) {
      svg.appendChild(el('text', { x: m.l - 6, y: sy(f) + 4, 'text-anchor': 'end', class: 'tick', text: fmtPct(f) }));
      svg.appendChild(el('text', { x: sx(f), y: m.t + plotH + 14, 'text-anchor': 'middle', class: 'tick', text: fmtPct(f) }));
    });
    svg.appendChild(el('text', { x: m.l + plotW / 2, y: H - 4, 'text-anchor': 'middle', class: 'sub', text: 'doability score' }));

    buckets.forEach(function (b) {
      var acc = b.accuracy || {};
      var rate = typeof acc.rate === 'number' ? acc.rate : null;
      var cx = sx(b.score), base = sy(0);
      var g = el('g', { class: 'col' });
      var top = sy(rate === null ? 0 : rate);
      g.appendChild(el('rect', {
        x: cx - bw / 2, y: Math.min(top, base - (rate ? 2 : 0)), width: bw, height: Math.max(rate ? 2 : 0, base - top), rx: 3,
        class: 'bar ' + (rate === null ? 'c-muted' : 'c-accent'),
        'data-tip': 'score ' + fmtPct(b.score) + ' · accuracy ' + fmtPct(rate) + ' · n ' + (b.n || 0)
      }));
      if (typeof acc.low === 'number' && typeof acc.high === 'number' && rate !== null) {
        g.appendChild(el('line', { x1: cx, y1: sy(acc.low), x2: cx, y2: sy(acc.high), class: 'whisker' }));
      }
      g.appendChild(el('text', { x: cx, y: base + 26, 'text-anchor': 'middle', class: 'sub', text: 'n ' + (b.n || 0) }));
      svg.appendChild(g);
    });

    bindTips(svg);
    container.appendChild(svg);
    return svg;
  }

  /* ---- one 100% segmented row ----------------------------------------- */

  function segmentRow(container, opts) {
    var o = opts || {};
    var segs = (Array.isArray(o.segments) ? o.segments : []).map(function (s) {
      return { label: String(s.label || ''), value: Number(s.value) || 0, cls: s.cls || 'c-accent' };
    });
    var total = segs.reduce(function (a, s) { return a + s.value; }, 0);
    var W = widthOf(container, o.width), H = o.height || 30;
    var wrap = document.createElement('div');
    wrap.className = 'segrow';
    var svg = el('svg', { viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, class: 'chart chart-seg' });
    var x = 0;
    segs.forEach(function (s) {
      if (s.value <= 0 || total <= 0) return;
      var w = (s.value / total) * W;
      svg.appendChild(el('rect', {
        x: x, y: 2, width: Math.max(1, w - 1), height: H - 4, rx: 3, class: 'bar ' + s.cls,
        'data-tip': s.label + ': ' + fmtNum(s.value) + ' (' + fmtPct(s.value / total) + ')'
      }));
      if (w > 56) {
        svg.appendChild(el('text', { x: x + w / 2, y: H / 2 + 4, 'text-anchor': 'middle', class: 'onbar', text: s.label + ' ' + fmtNum(s.value) }));
      }
      x += w;
    });
    if (total <= 0) {
      svg.appendChild(el('rect', { x: 0, y: 2, width: W, height: H - 4, rx: 3, class: 'bar c-muted' }));
    }
    bindTips(svg);
    wrap.appendChild(svg);
    var legend = document.createElement('ul');
    legend.className = 'legend';
    segs.forEach(function (s) {
      var li = document.createElement('li');
      var sw = document.createElement('span');
      sw.className = 'sw ' + s.cls;
      li.appendChild(sw);
      li.appendChild(document.createTextNode(s.label + ' ' + fmtNum(s.value)));
      legend.appendChild(li);
    });
    wrap.appendChild(legend);
    container.appendChild(wrap);
    return wrap;
  }

  /* ---- numbers table ---------------------------------------------------- */

  function tableFor(rows, columns) {
    var list = Array.isArray(rows) ? rows : [];
    var table = document.createElement('table');
    table.className = 'numbers';
    if (!list.length) {
      var cap = document.createElement('caption');
      cap.textContent = 'No numbers yet.';
      table.appendChild(cap);
      return table;
    }
    var cols = Array.isArray(columns) && columns.length ? columns : Object.keys(list[0] || {});
    var thead = document.createElement('thead');
    var trh = document.createElement('tr');
    cols.forEach(function (c) {
      var th = document.createElement('th');
      th.textContent = String(c);
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    table.appendChild(thead);
    var tbody = document.createElement('tbody');
    list.forEach(function (r) {
      var tr = document.createElement('tr');
      cols.forEach(function (c) {
        var td = document.createElement('td');
        var v = r ? r[c] : '';
        if (typeof v === 'number') { td.textContent = fmtNum(v); td.className = 'num'; }
        else if (v === null || v === undefined) td.textContent = '—';
        else if (typeof v === 'object') td.textContent = JSON.stringify(v);
        else td.textContent = String(v);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return table;
  }

  window.RedlineCharts = {
    el: el,
    barChart: barChart,
    lineChart: lineChart,
    reliabilityChart: reliabilityChart,
    segmentRow: segmentRow,
    tableFor: tableFor,
    bindTips: bindTips,
    fmtPct: fmtPct,
    fmtNum: fmtNum
  };
})();
