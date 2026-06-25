/* ============================================================
   campaign.js — drives one campaign view
   ============================================================ */
const CID = window.CAMPAIGN_ID;
const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const COLORS = {
  attr: { partner: '#b98bff', address: '#4fc3dc', email: '#e8b84b',
          phone: '#3ddc84', capital: '#888888' },
  risk: { low: '#5a6066', mid: '#e8b84b', high: '#ff5d57' },
  bg: '#000000', fg: '#e6e6e6', dim: '#8b9298', line: '#262a2e',
};
const riskClass = r => r >= 60 ? 'high' : r >= 30 ? 'mid' : 'low';
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmtBRL = v => v == null ? '—' :
  'R$ ' + Number(v).toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

let network = null, map = null, markerLayer = null, lastData = null;
let graphSig = '', mapSig = '';   // re-render heavy views only when content changes

/* ---------------------------------------------------------------- tabs --- */
$$('#tabs .tab').forEach(t => t.addEventListener('click', () => {
  $$('#tabs .tab').forEach(x => x.classList.remove('active'));
  $$('.tabpane').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  $(`[data-pane="${t.dataset.tab}"]`).classList.add('active');
  if (t.dataset.tab === 'graph' && network) network.fit();
  if (t.dataset.tab === 'geo' && map) setTimeout(() => map.invalidateSize(), 60);
}));

/* ------------------------------------------------------------- ingest --- */
$('#btn-add').addEventListener('click', async () => {
  const fd = new FormData();
  fd.append('cnpjs', $('#cnpjs').value);
  const f = $('#file').files[0];
  if (f) fd.append('file', f);
  $('#add-status').textContent = 'adding…';
  try {
    const r = await fetch(`/campaigns/${CID}/cnpjs`, { method: 'POST', body: fd });
    const j = await r.json();
    $('#cnpjs').value = ''; $('#file').value = '';
    $('#add-status').textContent = `+${j.added} queued` + (j.invalid ? ` · ${j.invalid} invalid` : '');
    toast(`${j.added} CNPJ(s) queued for fetching`);
    poll(); setTimeout(loadData, 800);
  } catch (e) { $('#add-status').textContent = 'error'; toast('Add failed', true); }
});

$('#btn-refresh').addEventListener('click', async () => {
  await fetch(`/campaigns/${CID}/refresh`, { method: 'POST' });
  toast('Re-fetching all CNPJs from the API'); poll();
});

$('#btn-geocode').addEventListener('click', async () => {
  const r = await fetch(`/campaigns/${CID}/geocode`, { method: 'POST' });
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
    if (busy) pollTimer = setTimeout(poll, 1800);
  } catch (e) { clearTimeout(pollTimer); pollTimer = setTimeout(poll, 4000); }
}

/* --------------------------------------------------------------- data --- */
async function loadData() {
  try {
    lastData = await (await fetch(`/campaigns/${CID}/data`)).json();
  } catch (e) { return; }
  renderSummary(lastData.summary);
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

/* -------------------------------------------------------------- graph --- */
function renderGraph(graph, companies) {
  const el = $('#graph');
  if (!graph.nodes.length) { el.innerHTML = '<div class="empty" style="margin:24px">No companies fetched yet.</div>'; return; }
  const sig = graph.nodes.map(n => n.id + (n.risk || '')).join(',') + '|' + graph.edges.length;
  if (sig === graphSig && network) return;   // unchanged → keep current layout/interaction
  graphSig = sig;
  const nodes = graph.nodes.map(n => {
    if (n.type === 'cnpj') {
      const rc = COLORS.risk[riskClass(n.risk)];
      return {
        id: n.id, label: n.label, shape: 'dot', size: 11,
        color: { background: '#0b0c0d', border: rc, highlight: { background: '#141618', border: rc } },
        borderWidth: 2, font: { color: COLORS.dim, size: 11, face: 'JetBrains Mono' },
        title: `${esc(n.razao)}\nrisk ${n.risk}`,
      };
    }
    const col = COLORS.attr[n.dim] || '#888';
    return {
      id: n.id, label: n.label, shape: 'box',
      color: { background: '#0b0c0d', border: col, highlight: { background: '#141618', border: col } },
      borderWidth: 1, margin: 6, font: { color: col, size: 10, face: 'JetBrains Mono' },
      title: `${esc(n.full)} · ${n.count} companies`,
    };
  });
  const edges = graph.edges.map(e => ({
    from: e.source, to: e.target,
    color: { color: COLORS.attr[e.dim] || '#444', opacity: 0.5 },
    width: 1, smooth: { type: 'continuous' },
  }));
  const data = { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) };
  const options = {
    physics: { stabilization: { iterations: 180 }, barnesHut: { gravitationalConstant: -7000, springLength: 120, springConstant: 0.03 } },
    interaction: { hover: true, tooltipDelay: 120 },
    nodes: { scaling: { min: 8, max: 26 } },
  };
  if (network) network.destroy();
  network = new vis.Network(el, data, options);
  network.on('doubleClick', p => {
    const id = p.nodes[0];
    if (id && /^\d{14}$/.test(id)) window.open(`/cnpj/${id}`, '_blank');
  });
}

/* ---------------------------------------------------------------- map --- */
function ensureMap() {
  if (map) return;
  map = L.map('map', { zoomControl: true, attributionControl: false }).setView([-14.4, -51.9], 4);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', { maxZoom: 19 }).addTo(map);
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
let companyCache = [];
function renderCompanies(rows) {
  companyCache = rows;
  drawCompanies(rows);
}
function drawCompanies(rows) {
  const tb = $('#companies-table tbody');
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="8" class="faint" style="padding:22px">No fetched companies yet.</td></tr>'; return; }
  const REVIEW = { suspicious: 'red', confirmed: 'red', cleared: 'green' };
  tb.innerHTML = rows.map(r => {
    const rc = riskClass(r.risk);
    const flags = r.flags.map(f => {
      let cls = 'pill';
      if (/NULA|INAPTA|front-man|ring|farm|virtual office|High capital|above legal|void/i.test(f)) cls += ' red';
      else if (/Registered|90 days|MEI|Nominal|High-risk activity|extreme|entered/i.test(f)) cls += ' amber';
      else if (/partner|owner|name twin/i.test(f)) cls += ' violet';
      else if (/Status/i.test(f)) cls += ' red';
      else cls += ' cyan';
      return `<span class="${cls}">${esc(f)}</span>`;
    }).join('');
    const ring = r.ring_size >= 3 ? `<span class="pill red" title="ring of ${r.ring_size} companies">⬡ ${r.ring_size}</span>` : '';
    const review = (r.review_status && r.review_status !== 'none')
      ? `<span class="pill ${REVIEW[r.review_status] || ''}">${esc(r.review_status)}</span>` : '';
    return `<tr>
      <td><span class="risk ${rc}"><span class="bar"><i style="width:${r.risk}%"></i></span><span class="v">${r.risk}</span></span></td>
      <td><a href="/cnpj/${r.cnpj}" target="_blank">${esc(r.razao_social || r.cnpj)}</a>
        ${r.nome_fantasia ? `<div class="faint mono-sm">${esc(r.nome_fantasia)}</div>` : ''}
        ${review}</td>
      <td class="mono-sm">${r.cnpj}</td>
      <td>${esc(r.uf || '')}</td>
      <td class="mono-sm">${esc(r.data_inicio || '')}</td>
      <td class="num mono-sm">${fmtBRL(r.capital)}</td>
      <td>${ring}${flags || '<span class="faint">—</span>'}</td>
      <td><button class="btn btn-sm btn-ghost" data-rm="${r.cnpj}" title="remove from campaign">✕</button></td>
    </tr>`;
  }).join('');
  $$('#companies-table [data-rm]').forEach(b => b.addEventListener('click', async () => {
    const fd = new FormData(); fd.append('cnpj', b.dataset.rm);
    await fetch(`/campaigns/${CID}/remove`, { method: 'POST', body: fd });
    toast('Removed from campaign'); loadData();
  }));
}
$('#filter-companies').addEventListener('input', e => {
  const q = e.target.value.toLowerCase();
  drawCompanies(companyCache.filter(r =>
    JSON.stringify(r).toLowerCase().includes(q)));
});

function renderProblems(problems, pending) {
  const el = $('#problems');
  let html = '';
  if (pending) html += `<div class="panel"><div class="bd faint">⟳ ${pending} CNPJ(s) still being fetched…</div></div>`;
  if (problems && problems.length) {
    html += `<div class="panel"><div class="hd"><h2>Fetch problems</h2><span class="count">${problems.length}</span></div>
      <div class="bd flush"><table><thead><tr><th>cnpj</th><th>status</th><th>detail</th></tr></thead><tbody>` +
      problems.map(p => `<tr><td class="mono-sm">${p.cnpj}</td>
        <td><span class="pill red">${esc(p.status)}</span></td>
        <td class="faint mono-sm">${esc(p.error || '')}</td></tr>`).join('') +
      `</tbody></table></div></div>`;
  }
  el.innerHTML = html;
}

/* -------------------------------------------------------------- rings --- */
const DIM_LABEL = { address: 'address', email: 'e-mail', phone: 'phone',
  partner: 'owner', accountant: 'accountant', regday: 'reg-day', adjacent: 'adjacent CNPJ' };
function renderRings(rings) {
  const el = $('#rings');
  if (!rings || !rings.length) {
    el.innerHTML = '<div class="faint" style="padding:8px 0">No multi-company rings detected yet. Rings appear when 3+ companies are tied together by shared owners, addresses, contacts or sequential registration.</div>';
    return;
  }
  el.innerHTML = rings.map((rg, i) => {
    const band = rg.avg_risk >= 60 ? 'high' : rg.avg_risk >= 30 ? 'mid' : 'low';
    const dims = rg.dims.map(d => `<span class="pill cyan">${esc(DIM_LABEL[d] || d)}</span>`).join('');
    const members = rg.cnpjs.map(c => `<a class="pill" href="/cnpj/${c}" target="_blank">${c}</a>`).join('');
    return `<div class="cluster-item">
      <div class="head">
        <span><b>Ring #${i + 1}</b> · ${rg.size} companies</span>
        <span class="risk ${band}"><span class="bar"><i style="width:${rg.avg_risk}%"></i></span><span class="v">avg ${rg.avg_risk}</span></span>
      </div>
      <div class="faint mono-sm" style="margin:4px 0 6px">linked by: ${dims || '—'} · peak risk ${rg.max_risk}</div>
      <div class="members">${members}</div></div>`;
  }).join('');
}


function renderClusters(clusters) {
  const defs = [
    ['partner', 'Shared owners / partners', 'violet'],
    ['address', 'Shared addresses', 'cyan'],
    ['email', 'Shared e-mails', 'amber'],
    ['phone', 'Shared phones', 'green'],
    ['accountant', 'Shared accountant e-mail', 'amber'],
    ['regday', 'Same registration day', 'red'],
    ['capital', 'Identical declared capital', ''],
  ];
  const wrap = $('#clusters');
  wrap.innerHTML = defs.map(([key, title, pill]) => {
    const list = clusters[key] || [];
    const body = list.length ? list.slice(0, 40).map(cl => {
      const val = key === 'capital' ? fmtBRL(cl.value) : esc(cl.value);
      const members = cl.cnpjs.map(c =>
        `<a class="pill ${pill}" href="/cnpj/${c}" target="_blank">${c}</a>`).join('');
      return `<div class="cluster-item">
        <div class="head"><span>${val || '<span class="faint">(blank)</span>'}</span>
          <span class="faint">${cl.count}×</span></div>
        <div class="members">${members}</div></div>`;
    }).join('') : '<div class="faint" style="padding:8px 0">none detected</div>';
    return `<div class="panel"><div class="hd"><h2>${title}</h2>
      <span class="count">${list.length} group(s)</span></div>
      <div class="bd">${body}</div></div>`;
  }).join('');
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

/* ---------------------------------------------------------------- boot --- */
poll();
