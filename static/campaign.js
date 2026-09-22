/* ============================================================
   campaign.js — drives one campaign view
   ============================================================ */
const CID = window.CAMPAIGN_ID;
const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

/* dimension metadata: colour + short label, mirrors correlations.PAIR_DIMS */
const DIM = {
  partner:                {c: '#b98bff', l: 'owner / partner',     low: false},
  partner_fp:             {c: '#b98bff', l: 'partner ~name',       low: false},
  address_full:           {c: '#4fc3dc', l: 'address',             low: false},
  street_num_cep:         {c: '#4fc3dc', l: 'street+nº+CEP',       low: false},
  cep:                    {c: '#3a7d8c', l: 'CEP',                 low: true},
  street_name:            {c: '#2f8fa8', l: 'same street',         low: true},
  email:                  {c: '#e8b84b', l: 'e-mail',              low: false},
  email_domain:           {c: '#e8b84b', l: 'e-mail domain',       low: false},
  accountant:             {c: '#d98b2b', l: 'accountant',          low: false},
  phone:                  {c: '#3ddc84', l: 'phone',               low: false},
  phone_prefix:           {c: '#2a9d6a', l: 'phone prefix',        low: true},
  regday:                 {c: '#ff8a4b', l: 'reg-day',             low: false},
  regday_muni:            {c: '#ff8a4b', l: 'reg-day+city',        low: false},
  regday_muni_cnae:       {c: '#ff5d57', l: 'reg-day+city+CNAE',   low: false},
  juridical_cnae_capital: {c: '#888888', l: 'nature+CNAE+capital', low: true},
  cnae_secondary:         {c: '#c9a34e', l: 'secondary CNAE',      low: true},
  root8:                  {c: '#9a86ff', l: 'CNPJ root',           low: false},
  geo:                    {c: '#3ddc84', l: 'geo-proximity',       low: false},
  capital:                {c: '#888888', l: 'capital',             low: true},
  closure_batch:          {c: '#ff5d57', l: 'batch closure',       low: false},
  adjacent:               {c: '#9a86ff', l: 'adjacent CNPJ',       low: false},
};
const dimColor = d => (DIM[d] || {}).c || '#888';
const dimLabel = d => (DIM[d] || {}).l || d;

const COLORS = { risk: { low: '#5a6066', mid: '#e8b84b', high: '#ff5d57' } };
const riskClass = r => r >= 60 ? 'high' : r >= 30 ? 'mid' : 'low';
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmtBRL = v => v == null ? '—' :
  'R$ ' + Number(v).toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

let network = null, map = null, markerLayer = null, lastData = null;
let graphSig = '', mapSig = '';
let activeDims = null;          // Set of enabled dims (null = all)
let focusRing = null;           // array of cnpjs to isolate, or null

/* ---------------------------------------------------------------- tabs --- */
$$('#tabs .tab').forEach(t => t.addEventListener('click', () => {
  $$('#tabs .tab').forEach(x => x.classList.remove('active'));
  $$('.tabpane').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  $(`[data-pane="${t.dataset.tab}"]`).classList.add('active');
  if (t.dataset.tab === 'graph' && network) network.fit();
  if (t.dataset.tab === 'geo' && map) setTimeout(() => map.invalidateSize(), 60);
  if (t.dataset.tab === 'ops') refreshQueue();
}));

/* ------------------------------------------------------------- ingest --- */
$('#btn-add').addEventListener('click', async () => {
  const fd = new FormData();
  fd.append('cnpjs', $('#cnpjs').value);
  const f = $('#file').files[0];
  if (f) fd.append('file', f);
  $('#add-status').textContent = 'adding…';
  try {
    const r = await postForm(`/campaigns/${CID}/cnpjs`, fd);
    const j = await r.json();
    $('#cnpjs').value = ''; $('#file').value = '';
    $('#add-status').textContent = `+${j.added} queued` + (j.invalid ? ` · ${j.invalid} invalid` : '');
    toast(`${j.added} CNPJ(s) queued for fetching`);
    poll(); setTimeout(loadData, 800);
  } catch (e) { $('#add-status').textContent = 'error'; toast('Add failed', true); }
});

$('#btn-refresh').addEventListener('click', async () => {
  await postForm(`/campaigns/${CID}/refresh`);
  toast('Re-fetching all CNPJs from the API'); poll();
});

$('#btn-geocode').addEventListener('click', async () => {
  const r = await postForm(`/campaigns/${CID}/geocode`);
  const j = await r.json();
  toast(j.started ? 'Geocoding addresses (≈1/sec)…' : 'Geocoding already running');
  poll();
});

/* ----------------------------------------------------------- progress --- */
let pollTimer = null;
async function poll() {
  try {
    const j = await (await fetch(`/campaigns/${CID}/progress`)).json();
    const lbl = $('#progress-label');
    const pend = (j.counts.pending || 0) + (j.counts.fetching || 0);
    let txt = `${j.done}/${j.total} fetched`;
    if (pend) txt = `fetching ${j.done}/${j.total} …`;
    if (j.geocoding) txt += ' · geocoding ⌖';
    lbl.textContent = j.total ? txt : 'no CNPJs yet';
    await loadData();
    const busy = pend > 0 || j.geocoding;
    clearTimeout(pollTimer);
    if (busy) { pollTimer = setTimeout(poll, 1800); refreshQueue(); }
  } catch (e) { clearTimeout(pollTimer); pollTimer = setTimeout(poll, 4000); }
}

/* --------------------------------------------------------------- data --- */
async function loadData() {
  try {
    lastData = await (await fetch(`/campaigns/${CID}/data`)).json();
  } catch (e) { return; }
  renderSummary(lastData.summary);
  renderDimFilters(lastData.graph);
  renderGraph(lastData.graph, lastData.companies);
  renderMap(lastData.map_points);
  renderCompanies(lastData.companies);
  renderProblems(lastData.problems, lastData.pending);
  renderRings(lastData.rings);
  renderClusters(lastData.clusters);
  renderInsights(lastData.insights, lastData.summary.total);
  $('#geo-note').textContent = lastData.summary.mapped
    ? `${lastData.summary.mapped} located · red = recent or high risk`
    : 'no coordinates yet · run "geocode for map"';
}

function renderSummary(s) {
  for (const k in s) { const el = $(`#summary [data-s="${k}"]`); if (el) el.textContent = s[k]; }
}

/* ------------------------------------------------------ graph filters --- */
function presentDims(graph) {
  const set = new Set();
  graph.edges.forEach(e => set.add(e.dim));
  return [...set];
}
function renderDimFilters(graph) {
  const dims = presentDims(graph);
  if (activeDims === null) activeDims = new Set(dims);
  const host = $('#dim-filters');
  host.innerHTML = dims.map(d => {
    const on = activeDims.has(d);
    return `<button class="dim-chip ${on ? 'on' : ''}" data-dim="${d}"
      style="--dc:${dimColor(d)}">${esc(dimLabel(d))}</button>`;
  }).join('') || '<span class="faint mono-sm">no links yet</span>';
  $$('#dim-filters .dim-chip').forEach(b => b.addEventListener('click', () => {
    const d = b.dataset.dim;
    if (activeDims.has(d)) activeDims.delete(d); else activeDims.add(d);
    graphSig = ''; renderDimFilters(lastData.graph); renderGraph(lastData.graph, lastData.companies);
  }));
  $('#legend').innerHTML =
    '<span><span class="dot dim"></span>company</span>' +
    dims.map(d => `<span><span class="dot" style="background:${dimColor(d)}"></span>${esc(dimLabel(d))}</span>`).join('') +
    '<span class="faint">— node ring colour = risk —</span>';
}
$('#hide-low').addEventListener('change', () => { graphSig = ''; renderGraph(lastData.graph, lastData.companies); });
$('#min-cluster').addEventListener('input', () => { graphSig = ''; renderGraph(lastData.graph, lastData.companies); });
$('#edge-labels').addEventListener('change', () => { graphSig = ''; renderGraph(lastData.graph, lastData.companies); });
$('#graph-reset').addEventListener('click', () => {
  focusRing = null; activeDims = null; $('#hide-low').checked = false;
  $('#min-cluster').value = 2; $('#path-result').innerHTML = ''; $('#path-status').textContent = '';
  graphSig = ''; renderDimFilters(lastData.graph); renderGraph(lastData.graph, lastData.companies);
});

/* -------------------------------------------------------------- graph --- */
function renderGraph(graph, companies) {
  const el = $('#graph');
  if (!graph.nodes.length) { el.innerHTML = '<div class="empty" style="margin:24px">No companies fetched yet.</div>'; return; }
  const hideLow = $('#hide-low').checked;
  const minClust = Math.max(2, parseInt($('#min-cluster').value) || 2);
  const showLabels = $('#edge-labels').checked;

  const attrOk = {};
  graph.nodes.forEach(n => {
    if (n.type !== 'attr') return;
    let ok = (!activeDims || activeDims.has(n.dim));
    if (hideLow && n.low) ok = false;
    if (n.count < minClust) ok = false;
    attrOk[n.id] = ok;
  });
  const ringSet = focusRing ? new Set(focusRing) : null;

  const keepCnpj = new Set();
  const edges = [];
  graph.edges.forEach(e => {
    if (!attrOk[e.source]) return;
    if (ringSet && !ringSet.has(e.target)) return;
    keepCnpj.add(e.target);
    edges.push({
      from: e.source, to: e.target,
      label: showLabels ? e.label : undefined,
      color: { color: dimColor(e.dim), opacity: 0.55 },
      font: { color: dimColor(e.dim), size: 9, face: 'JetBrains Mono', strokeWidth: 3, strokeColor: '#000' },
      width: 1, smooth: { type: 'continuous' },
    });
  });

  const nodes = [];
  graph.nodes.forEach(n => {
    if (n.type === 'cnpj') {
      if (ringSet && !ringSet.has(n.id)) return;
      const rc = COLORS.risk[riskClass(n.risk)];
      nodes.push({
        id: n.id, label: n.label, shape: 'dot', size: 11,
        color: { background: '#0b0c0d', border: rc, highlight: { background: '#141618', border: rc } },
        borderWidth: 2, font: { color: '#8b9298', size: 11, face: 'JetBrains Mono' },
        title: `${esc(n.razao)}\nrisk ${n.risk}`,
      });
    } else {
      if (!attrOk[n.id]) return;
      const col = dimColor(n.dim);
      nodes.push({
        id: n.id, label: n.label, shape: 'box',
        color: { background: '#0b0c0d', border: col, highlight: { background: '#141618', border: col } },
        borderWidth: 1, margin: 6, font: { color: col, size: 10, face: 'JetBrains Mono' },
        title: `${esc(dimLabel(n.dim))}: ${esc(n.full)} · ${n.count} companies`,
      });
    }
  });

  const sig = nodes.map(n => n.id).join(',') + '|' + edges.length + '|' + showLabels + '|' + (focusRing ? 'R' : '');
  if (sig === graphSig && network) return;
  graphSig = sig;

  const data = { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) };
  const options = {
    physics: { stabilization: { iterations: 160 }, barnesHut: { gravitationalConstant: -7000, springLength: 130, springConstant: 0.03 } },
    interaction: { hover: true, tooltipDelay: 120 },
    nodes: { scaling: { min: 8, max: 26 } },
  };
  if (network) network.destroy();
  network = new vis.Network(el, data, options);
  network.on('doubleClick', p => {
    const id = p.nodes[0];
    if (id && !/^attr_/.test(id)) window.open(`/cnpj/${id}`, '_blank');
  });
}

/* ------------------------------------------------- shortest path / why --- */
function highlightPath(path) {
  if (!network || !path || path.length < 2) return;
  try { network.selectNodes(path, true); network.fit({ nodes: path, animation: true }); } catch (e) {}
}
function renderPathResult(j, mode) {
  const host = $('#path-result');
  let html = '';
  if (mode === 'why' && j.direct) {
    if (j.direct.length) {
      html += '<div class="path-direct"><b>directly connected by:</b> ' +
        j.direct.map(d => `<span class="pill" style="border-color:${dimColor(d.dim)};color:${dimColor(d.dim)}">${esc(dimLabel(d.dim))}: ${esc(d.value)}</span>`).join(' ') +
        '</div>';
    } else {
      html += '<div class="faint mono-sm">no direct shared attribute — showing shortest path</div>';
    }
    j = j.path || { found: false };
  }
  if (j.found && j.path && j.path.length) {
    const steps = [];
    for (let i = 0; i < j.path.length; i++) {
      steps.push(`<a class="pill" href="/cnpj/${j.path[i]}" target="_blank">${esc((j.path_fmt && j.path_fmt[i]) || j.path[i])}</a>`);
      if (i < j.path.length - 1 && j.links && j.links[i]) {
        const reasons = j.links[i].map(l => dimLabel(l.dim)).join(', ');
        steps.push(`<span class="path-arrow" title="${esc(reasons)}">— ${esc(reasons)} →</span>`);
      }
    }
    html += `<div class="path-chain">${steps.join(' ')}</div>`;
    highlightPath(j.path);
  } else if (mode === 'path') {
    html += `<div class="faint mono-sm">${esc(j.reason || 'no path found')}</div>`;
  }
  host.innerHTML = html;
}
async function runPath(mode) {
  const a = $('#path-a').value.trim(), b = $('#path-b').value.trim();
  if (!a || !b) { $('#path-status').textContent = 'enter two CNPJs'; return; }
  $('#path-status').textContent = 'computing…';
  try {
    const url = `/campaigns/${CID}/${mode === 'why' ? 'why' : 'path'}?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`;
    const j = await (await fetch(url)).json();
    $('#path-status').textContent = '';
    renderPathResult(j, mode);
  } catch (e) { $('#path-status').textContent = 'error'; }
}
$('#btn-path').addEventListener('click', () => runPath('path'));
$('#btn-why').addEventListener('click', () => runPath('why'));

/* ---------------------------------------------------------------- map --- */
function ensureMap() {
  if (map) return;
  map = L.map('map', { zoomControl: true, attributionControl: true }).setView([-14.4, -51.9], 4);
  // OpenStreetMap's standard tile server: free, open-source (ODbL data / open
  // tile service), no API key or account required. It replaced the previous
  // CartoDB basemap, which now gates its tiles behind a registered API key.
  // The tiles are light by default; `.map-dark-tiles` (style.css) applies a
  // CSS filter to re-tint them so the map still matches the app's dark theme
  // — there's no free/keyless dark-styled raster source to swap in directly.
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    className: 'map-dark-tiles',
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
  }).addTo(map);
  markerLayer = L.layerGroup().addTo(map);
}
function renderMap(points) {
  ensureMap();
  const sig = points.map(p => p.cnpj + p.lat + p.risk).join(',');
  if (sig === mapSig) return;
  mapSig = sig;
  markerLayer.clearLayers();
  if (!points.length) return;
  const latlngs = [];
  points.forEach(p => {
    const rc = p.risk >= 60 ? COLORS.risk.high : p.recent ? COLORS.risk.mid : COLORS.risk.low;
    const m = L.circleMarker([p.lat, p.lon], {
      radius: 6, color: rc, weight: 2, fillColor: rc, fillOpacity: 0.35,
    }).bindPopup(
      `<b>${esc(p.name)}</b><br>${esc(p.address)}<br>` +
      `<span style="color:${rc}">risk ${p.risk}</span>` +
      ` · <a href="/cnpj/${p.cnpj}" target="_blank">detail →</a>`
    );
    markerLayer.addLayer(m); latlngs.push([p.lat, p.lon]);
  });
  try { map.fitBounds(latlngs, { padding: [30, 30], maxZoom: 14 }); } catch (e) {}
  setTimeout(() => map.invalidateSize(), 50);
}

/* ----------------------------------------------------------- companies -- */
function evidencePill(e) {
  const d = DIM[e.signal];
  const col = d ? d.c : (/NULA|INAPTA|SUSPENSA|BAIXADA|farm|hub|front-man|ring|flip|void|ceiling|special_situation|serial_admin/i.test(e.signal) ? '#ff5d57'
                : /recent|capital|risky|extreme|twin|status|regime_exit/i.test(e.signal) ? '#e8b84b' : '#4fc3dc');
  const conf = Math.round((e.confidence || 0) * 100);
  const tip = `${e.detail || e.signal} · weight ${e.weight} · confidence ${conf}% · source: ${e.source}` +
    (e.matched && e.matched.length ? ` · ${e.matched.length} linked` : '');
  return `<span class="ev-pill" style="--ec:${col}" title="${esc(tip)}">${esc(e.detail || e.signal)}<span class="ev-w">+${e.weight}</span></span>`;
}
let companyCache = [];
function renderCompanies(rows) { companyCache = rows; drawCompanies(rows); }
function drawCompanies(rows) {
  const tb = $('#companies-table tbody');
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="8" class="faint" style="padding:22px">No fetched companies yet.</td></tr>'; return; }
  const REVIEW = { suspicious: 'red', confirmed: 'red', cleared: 'green' };
  tb.innerHTML = rows.map(r => {
    const rc = riskClass(r.risk);
    const ev = (r.evidence || []).slice(0, 4).map(evidencePill).join('');
    const more = (r.evidence || []).length > 4 ? `<span class="faint mono-sm">+${r.evidence.length - 4}</span>` : '';
    const ring = r.ring_size >= 3 ? `<span class="pill red" title="ring of ${r.ring_size} companies">⬡ ${r.ring_size}</span>` : '';
    const review = (r.review_status && r.review_status !== 'none')
      ? `<span class="pill ${REVIEW[r.review_status] || ''}">${esc(r.review_status)}</span>` : '';
    return `<tr>
      <td><span class="risk ${rc}"><span class="bar"><i style="width:${r.risk}%"></i></span><span class="v">${r.risk}</span></span></td>
      <td><a href="/cnpj/${r.cnpj}" target="_blank">${esc(r.razao_social || r.cnpj)}</a>
        ${r.nome_fantasia ? `<div class="faint mono-sm">${esc(r.nome_fantasia)}</div>` : ''}
        ${review}</td>
      <td class="mono-sm">${esc(r.cnpj_fmt || r.cnpj)}</td>
      <td>${esc(r.uf || '')}</td>
      <td class="mono-sm">${esc(r.data_inicio || '')}</td>
      <td class="num mono-sm">${fmtBRL(r.capital)}</td>
      <td class="ev-cell">${ring}${ev || '<span class="faint">—</span>'}${more}</td>
      <td><button class="btn btn-sm btn-ghost" data-rm="${r.cnpj}" title="remove from campaign">✕</button></td>
    </tr>`;
  }).join('');
  $$('#companies-table [data-rm]').forEach(b => b.addEventListener('click', async () => {
    await postForm(`/campaigns/${CID}/remove`, { cnpj: b.dataset.rm });
    toast('Removed from campaign'); loadData();
  }));
}
$('#filter-companies').addEventListener('input', e => {
  const q = e.target.value.toLowerCase();
  drawCompanies(companyCache.filter(r => JSON.stringify(r).toLowerCase().includes(q)));
});

function renderProblems(problems, pending) {
  const el = $('#problems');
  let html = '';
  if (pending) html += `<div class="panel"><div class="bd faint">⟳ ${pending} CNPJ(s) still being fetched…</div></div>`;
  if (problems && problems.length) {
    html += `<div class="panel"><div class="hd"><h2>Fetch problems</h2><span class="count">${problems.length}</span></div>
      <div class="bd flush"><table><thead><tr><th>cnpj</th><th>status</th><th>detail</th></tr></thead><tbody>` +
      problems.map(p => `<tr><td class="mono-sm">${esc(p.cnpj)}</td>
        <td><span class="pill red">${esc(p.status)}</span></td>
        <td class="faint mono-sm">${esc(p.error || '')}</td></tr>`).join('') +
      `</tbody></table></div></div>`;
  }
  el.innerHTML = html;
}

/* -------------------------------------------------------------- rings --- */
function renderRings(rings) {
  const el = $('#rings');
  if (!rings || !rings.length) {
    el.innerHTML = '<div class="faint" style="padding:8px 0">No multi-company rings detected yet. Rings appear when 3+ companies are tied together by shared owners, addresses, contacts or sequential registration.</div>';
    return;
  }
  el.innerHTML = rings.map((rg, i) => {
    const band = rg.avg_risk >= 60 ? 'high' : rg.avg_risk >= 30 ? 'mid' : 'low';
    const dims = rg.dims.map(d => `<span class="pill" style="border-color:${dimColor(d)};color:${dimColor(d)}">${esc(dimLabel(d))}</span>`).join('');
    const members = rg.cnpjs.map(c => `<a class="pill" href="/cnpj/${c}" target="_blank">${esc(c)}</a>`).join('');
    return `<div class="cluster-item ring-item">
      <div class="head">
        <span><b>Ring #${i + 1}</b> · ${rg.size} companies
          <button class="btn btn-sm btn-ghost" data-focus="${i}">isolate on map ◎</button></span>
        <span class="risk ${band}"><span class="bar"><i style="width:${rg.avg_risk}%"></i></span><span class="v">avg ${rg.avg_risk}</span></span>
      </div>
      <div class="faint mono-sm" style="margin:4px 0 6px">linked by: ${dims || '—'} · peak risk ${rg.max_risk}</div>
      <div class="members">${members}</div></div>`;
  }).join('');
  $$('#rings [data-focus]').forEach(b => b.addEventListener('click', (ev) => {
    ev.stopPropagation();
    const rg = rings[+b.dataset.focus];
    focusRing = rg.cnpjs; graphSig = '';
    $$('#tabs .tab').forEach(x => x.classList.remove('active'));
    $$('.tabpane').forEach(x => x.classList.remove('active'));
    $('#tabs .tab[data-tab="graph"]').classList.add('active');
    $('[data-pane="graph"]').classList.add('active');
    renderGraph(lastData.graph, lastData.companies);
    toast(`Isolated ring of ${rg.size} companies — "reset view" to clear`);
  }));
}

/* ----------------------------------------------------------- clusters --- */
function renderClusters(clusters) {
  const defs = [
    ['partner', 'Shared owners / partners'],
    ['partner_fp', 'Partner name near-matches'],
    ['address_full', 'Shared full address'],
    ['street_num_cep', 'Same street + nº + CEP'],
    ['cep', 'Same CEP'],
    ['street_name', 'Same street (any number)'],
    ['email', 'Shared e-mails'],
    ['email_domain', 'Shared e-mail domain'],
    ['accountant', 'Shared accounting contact'],
    ['phone', 'Shared phones'],
    ['phone_prefix', 'Same DDD + phone prefix'],
    ['root8', 'Same 8-char CNPJ root'],
    ['regday_muni_cnae', 'Same open-date + city + activity'],
    ['regday_muni', 'Same open-date + city'],
    ['regday', 'Same registration day'],
    ['geo', 'Geographically co-located'],
    ['closure_batch', 'Coordinated status change (batch)'],
    ['juridical_cnae_capital', 'Same nature + activity + capital band'],
    ['cnae_secondary', 'Shared secondary business activity'],
    ['capital', 'Identical declared capital'],
  ];
  const wrap = $('#clusters');
  wrap.innerHTML = defs.map(([key, title]) => {
    const list = clusters[key] || [];
    if (!list.length) return '';
    const col = dimColor(key);
    const body = list.slice(0, 40).map(cl => {
      const val = key === 'capital' ? fmtBRL(cl.value) : esc(cl.value);
      const members = cl.cnpjs.map(c =>
        `<a class="pill" style="border-color:${col}" href="/cnpj/${c}" target="_blank">${esc(c)}</a>`).join('');
      return `<div class="cluster-item">
        <div class="head"><span>${val || '<span class="faint">(blank)</span>'}</span>
          <span class="faint">${cl.count}×</span></div>
        <div class="members">${members}</div></div>`;
    }).join('');
    return `<div class="panel"><div class="hd"><h2>${title}</h2>
      <span class="count">${list.length} group(s)</span></div>
      <div class="bd">${body}</div></div>`;
  }).join('') || '<div class="panel"><div class="bd faint">No shared attributes detected yet.</div></div>';
}

/* ----------------------------------------------------------- insights --- */
function barList(items, total, color) {
  if (!items.length) return '<div class="faint">no data</div>';
  const max = Math.max(...items.map(i => i[1]));
  return '<div class="inline-list">' + items.map(([label, n]) => `
    <div class="bar-row">
      <div><div class="mono-sm">${esc(label) || '—'}</div>
        <div class="track"><i style="width:${(n / max * 100).toFixed(0)}%;background:${color}"></i></div></div>
      <div class="num mono-sm">${n}</div>
    </div>`).join('') + '</div>';
}
function renderInsights(ins, total) {
  const recent = ins.recent_list || [];
  const recentHtml = recent.length ? `<table><thead><tr><th>razão social</th><th>opened</th><th>age</th></tr></thead><tbody>` +
    recent.slice(0, 50).map(r => `<tr><td><a href="/cnpj/${r.cnpj}" target="_blank">${esc(r.name || r.cnpj)}</a></td>
      <td class="mono-sm">${esc(r.date)}</td>
      <td><span class="pill amber">${r.months} mo</span></td></tr>`).join('') +
    `</tbody></table>` : '<div class="faint" style="padding:8px">none under 1 year</div>';
  $('#insights').innerHTML = `
    <div class="panel"><div class="hd"><h2>Recently created (&lt; 1 year)</h2>
      <span class="count">${recent.length}</span></div><div class="bd flush scroll">${recentHtml}</div></div>
    <div class="panel"><div class="hd"><h2>By state (UF)</h2></div>
      <div class="bd">${barList(ins.uf, total, '#4fc3dc')}</div></div>
    <div class="panel"><div class="hd"><h2>Top business activities (CNAE)</h2></div>
      <div class="bd">${barList(ins.cnae, total, '#b98bff')}</div></div>
    <div class="panel"><div class="hd"><h2>Registration year</h2></div>
      <div class="bd">${barList(ins.year.map(([y, n]) => [String(y), n]), total, '#3ddc84')}</div></div>
    <div class="panel"><div class="hd"><h2>Registration status</h2></div>
      <div class="bd">${barList(ins.status, total, '#e8b84b')}</div></div>`;
}

/* ------------------------------------------------------ queue & data ops -- */
async function refreshQueue() {
  try {
    const j = await (await fetch('/queue/status')).json();
    const s = j.status || {};
    const total = Object.values(s).reduce((a, b) => a + b, 0);
    $('#queue-count').textContent = `${total} in pool`;
    const order = [['ok', '#3ddc84'], ['pending', '#e8b84b'], ['fetching', '#4fc3dc'],
                   ['error', '#ff5d57'], ['not_found', '#888']];
    $('#queue-bars').innerHTML = order.filter(([k]) => s[k]).map(([k, c]) =>
      `<div class="bar-row"><div><div class="mono-sm">${k}</div>
        <div class="track"><i style="width:${total ? (s[k] / total * 100).toFixed(0) : 0}%;background:${c}"></i></div></div>
        <div class="num mono-sm">${s[k]}</div></div>`).join('') || '<div class="faint">queue empty</div>';
    const errs = j.errors || [];
    $('#queue-errors').innerHTML = errs.length
      ? `<div class="faint mono-sm" style="margin-bottom:4px">recent fetch errors (${errs.length}):</div>` +
        `<div class="scroll" style="max-height:160px"><table><tbody>` +
        errs.slice(0, 40).map(e => `<tr><td class="mono-sm">${esc(e.cnpj)}</td>
          <td><span class="pill red">${esc(e.fetch_status)}</span></td>
          <td class="faint mono-sm">${esc(e.fetch_error || '')}</td></tr>`).join('') +
        `</tbody></table></div>`
      : '<div class="faint mono-sm">no fetch errors</div>';
  } catch (e) {}
}
$('#btn-retry-errors').addEventListener('click', async () => {
  const r = await postForm('/queue/requeue', { which: 'errors', campaign_id: CID });
  const j = await r.json(); toast(`Re-queued ${j.requeued} errored CNPJ(s)`); poll(); refreshQueue();
});
$('#btn-retry-all').addEventListener('click', async () => {
  const r = await postForm('/queue/requeue', { which: 'all', campaign_id: CID });
  const j = await r.json(); toast(`Re-queued ${j.requeued} CNPJ(s)`); poll(); refreshQueue();
});
$('#btn-import').addEventListener('click', async () => {
  const f = $('#import-file').files[0];
  if (!f) { $('#import-status').textContent = 'pick a file first'; return; }
  const fd = new FormData(); fd.append('file', f);
  $('#import-status').textContent = 'importing…';
  try {
    const r = await postForm('/campaigns/import', fd);
    const j = await r.json();
    if (j.error) { $('#import-status').textContent = 'error: ' + j.error; return; }
    $('#import-status').innerHTML = `imported campaign #${j.campaign_id}: +${j.added} cnpjs` +
      (j.invalid ? ` · ${j.invalid} invalid` : '') + ` — <a href="/campaigns/${j.campaign_id}">open →</a>`;
    toast('Campaign imported');
  } catch (e) { $('#import-status').textContent = 'import failed'; }
});
$('#btn-merge').addEventListener('click', async () => {
  const src = $('#merge-src').value;
  if (!src) { $('#merge-status').textContent = 'pick a campaign'; return; }
  if (!confirm('Merge that campaign into this one? The source campaign will be deleted.')) return;
  const r = await postForm('/campaigns/merge', { src: src, dst: CID });
  const j = await r.json();
  if (j.error) { $('#merge-status').textContent = 'error: ' + j.error; return; }
  $('#merge-status').textContent = `merged ${j.moved} CNPJ(s) in`;
  toast('Campaigns merged'); setTimeout(() => location.reload(), 900);
});

/* ---------------------------------------------------------------- boot --- */
poll();
refreshQueue();
