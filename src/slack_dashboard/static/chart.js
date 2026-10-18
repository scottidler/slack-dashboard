// Shaping charts, shared: the app page (/static/chart.js) and bin/explore.html load this one file.
// bin/explore.py inlines it into the published explorer, so it must stay free of imports.
// chart() draws the bars of an input's distribution on the current board with the knob's
// curve on top; fn === null draws the bars alone (an input with no curve, like the score).
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function fmt(x, d) { return Number(x).toFixed(d === undefined ? 2 : d); }
function hist(values, nb, lo, hi) {
  const b = new Array(nb).fill(0);
  for (const v of values) {
    let i = Math.floor((v - lo) / (hi - lo) * nb);
    if (i >= nb) i = nb - 1; if (i < 0) i = 0;
    b[i]++;
  }
  return b;
}
const FLAT = 0.05;  // a curve that moves less than this share of its max is not a shaping panel
function chart(title, note, values, lo, hi, fn, fnMax) {
  const W = 300, H = 120, pad = 6, top = 14, nb = 30;
  const ys = [];
  if (fn) {
    for (let i = 0; i <= 60; i++) ys.push(fn(lo + (hi - lo) * i / 60) / fnMax);
    if (Math.max(...ys) - Math.min(...ys) < FLAT) return '';
  }
  const bins = hist(values, nb, lo, hi);
  const bmax = Math.max(1, ...bins);
  const plotH = H - pad - top;
  let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">`;
  const bw = (W - 2 * pad) / nb;
  bins.forEach((n, i) => {
    const h = n / bmax * plotH;
    svg += `<rect x="${pad + i * bw + 1}" y="${H - pad - h}" width="${Math.max(1, bw - 2)}" height="${h}" fill="var(--bar-soft)"><title>${esc(fmt(lo + i * (hi - lo) / nb, 1))}: ${n} threads</title></rect>`;
  });
  svg += `<text x="${pad}" y="10" fill="var(--bar)" font-size="9">bars: tallest = ${bmax} threads</text>`;
  if (fn) {
    let d = '';
    ys.forEach((yv, i) => {
      d += (i ? 'L' : 'M') + fmt(pad + (W - 2 * pad) * i / 60, 1) + ',' + fmt(H - pad - yv * plotH, 1);
    });
    svg += `<path d="${d}" stroke="var(--curve)" stroke-width="2" fill="none"/>`;
    svg += `<text x="${W - pad}" y="10" fill="var(--curve)" font-size="9" text-anchor="end">curve: top = ${esc(fmt(fnMax, 0))}</text>`;
  }
  svg += `<text x="${pad}" y="${H - 1}" fill="var(--muted)" font-size="9">${esc(fmt(lo, 0))}</text>`;
  svg += `<text x="${W - pad}" y="${H - 1}" fill="var(--muted)" font-size="9" text-anchor="end">${esc(fmt(hi, 0))}</text>`;
  svg += '</svg>';
  return `<figure><h3>${esc(title)}</h3><p>${esc(note)}</p>${svg}</figure>`;
}
