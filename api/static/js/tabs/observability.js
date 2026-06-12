/* Swarm Web UI — tabs/observability module (split style, shared global scope) */
'use strict';

// 可观测面板：调 /api/observability/*（OpenLIT/ClickHouse LLM/embed/rerank trace）。
// 数据源不可达时整体降级显示"未配置"，不报错。面板全局（trace 跨项目），不依赖 selectedProjectId。

function obsHours() {
  const sel = $('obs-hours');
  return sel ? parseInt(sel.value, 10) || 24 : 24;
}

function setObsUnavailable(unavailable) {
  const banner = $('obs-unavailable');
  const grid = $('obs-summary-grid');
  if (banner) banner.classList.toggle('hidden', !unavailable);
  if (grid) grid.style.opacity = unavailable ? '0.35' : '1';
}

async function loadObservability() {
  const hours = obsHours();
  // 先探活，决定是否降级
  let ping;
  try {
    const r = await fetch('/api/observability/ping');
    ping = await r.json();
  } catch {
    ping = { available: false };
  }
  if (!ping || !ping.available) {
    setObsUnavailable(true);
    renderObsSummary(null);
    renderObsLatency({ available: false, rows: [] });
    renderObsSlow({ available: false, rows: [] });
    return;
  }
  setObsUnavailable(false);

  // 并行拉三块数据
  const [summary, latency, slow] = await Promise.all([
    fetchObs(`/api/observability/summary?hours=${hours}`),
    fetchObs(`/api/observability/latency?hours=${hours}&limit=25`),
    fetchObs(`/api/observability/slow?hours=${hours}&threshold_ms=5000&limit=20`),
  ]);
  renderObsSummary(summary);
  renderObsLatency(latency);
  renderObsSlow(slow);
}

async function fetchObs(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) return { available: false, rows: [] };
    return await r.json();
  } catch {
    return { available: false, rows: [] };
  }
}

function _num(v, suffix = '') {
  if (v === null || v === undefined || v === '') return '—';
  const n = typeof v === 'number' ? v : Number(v);
  if (Number.isNaN(n)) return escapeHtml(String(v));
  return n.toLocaleString() + suffix;
}

function renderObsSummary(s) {
  const set = (id, val) => { const el = $(id); if (el) el.textContent = val; };
  if (!s || !s.available) {
    ['obs-total-calls', 'obs-total-errors', 'obs-error-rate', 'obs-embed-p95', 'obs-llm-p95', 'obs-llm-max']
      .forEach(id => set(id, '—'));
    return;
  }
  const calls = Number(s.total_calls || 0);
  const errors = Number(s.total_errors || 0);
  const rate = calls > 0 ? ((errors / calls) * 100).toFixed(2) + '%' : '0%';
  set('obs-total-calls', _num(s.total_calls));
  set('obs-total-errors', _num(s.total_errors));
  set('obs-error-rate', rate);
  set('obs-embed-p95', _num(s.embed_p95_ms, ' ms'));
  set('obs-llm-p95', _num(s.llm_p95_ms, ' ms'));
  set('obs-llm-max', _num(s.llm_max_ms, ' ms'));
}

function renderObsLatency(data) {
  const el = $('obs-latency-table');
  if (!el) return;
  const rows = (data && data.rows) || [];
  if (!data || !data.available || rows.length === 0) {
    el.innerHTML = '<p style="color:var(--text-muted);font-size:12px;padding:8px">无数据</p>';
    return;
  }
  const head = ['Span', '调用', 'p50', 'p95', 'p99', 'max', '错误'];
  let html = '<table class="obs-table"><thead><tr>' +
    head.map(h => `<th>${h}</th>`).join('') + '</tr></thead><tbody>';
  for (const r of rows) {
    const errCls = Number(r.errors) > 0 ? ' style="color:var(--red)"' : '';
    html += '<tr>' +
      `<td title="${escapeHtml(String(r.span || ''))}">${escapeHtml(String(r.span || ''))}</td>` +
      `<td>${_num(r.calls)}</td>` +
      `<td>${_num(r.p50_ms)}</td>` +
      `<td>${_num(r.p95_ms)}</td>` +
      `<td>${_num(r.p99_ms)}</td>` +
      `<td>${_num(r.max_ms)}</td>` +
      `<td${errCls}>${_num(r.errors)}</td>` +
      '</tr>';
  }
  html += '</tbody></table>';
  el.innerHTML = html;
}

function renderObsSlow(data) {
  const el = $('obs-slow-table');
  if (!el) return;
  const rows = (data && data.rows) || [];
  if (!data || !data.available || rows.length === 0) {
    el.innerHTML = '<p style="color:var(--text-muted);font-size:12px;padding:8px">无慢调用</p>';
    return;
  }
  const head = ['时间', 'Span', '耗时(ms)', '状态', '模型'];
  let html = '<table class="obs-table"><thead><tr>' +
    head.map(h => `<th>${h}</th>`).join('') + '</tr></thead><tbody>';
  for (const r of rows) {
    const isErr = String(r.status || '').includes('ERROR');
    html += '<tr>' +
      `<td>${escapeHtml(String(r.ts || ''))}</td>` +
      `<td title="${escapeHtml(String(r.span || ''))}">${escapeHtml(String(r.span || ''))}</td>` +
      `<td>${_num(r.ms)}</td>` +
      `<td${isErr ? ' style="color:var(--red)"' : ''}>${escapeHtml(String(r.status || ''))}</td>` +
      `<td>${escapeHtml(String(r.model || ''))}</td>` +
      '</tr>';
  }
  html += '</tbody></table>';
  el.innerHTML = html;
}
