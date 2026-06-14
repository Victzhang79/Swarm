/* Swarm Web UI — tabs/config module (split from app.js, shared global scope) */
'use strict';

async function loadConfig() {
  try {
    const resp = await fetch('/api/config');
    if (!resp.ok) return;
    const data = await resp.json();
    const cfg = data.config || {};
    const flat = data.flat || data.config?.model || {};
    originalConfig = { ...flat, ...cfg };
    // SiliconFlow / 本地接入点已统一由「模型接入点」区(providers)承载，不再有独立扁平字段。
    const lsKey = cfg.langsmith_api_key || '';
    if ($('cfg-langsmith-tracing')) $('cfg-langsmith-tracing').checked = !!cfg.langsmith_tracing;
    if ($('cfg-langsmith-api-key')) {
      $('cfg-langsmith-api-key').value = lsKey.includes('...') ? '' : lsKey;
      $('cfg-langsmith-api-key').placeholder = lsKey.includes('...') ? lsKey : 'LangSmith API Key';
    }
    if ($('cfg-langsmith-project')) $('cfg-langsmith-project').value = cfg.langsmith_project || 'swarm-dev';
    await fetchModels();
    await loadRoutingTable();
  } catch { /* ignore */ }
}

function setModelValue(selectId, value) {
  const sel = $(selectId);
  if (!sel) return;
  const opt = Array.from(sel.options).find(o => o.value === value);
  if (opt) sel.value = value;
  else if (value) {
    const o = document.createElement('option');
    o.value = value;
    o.textContent = value + ' (当前)';
    sel.appendChild(o);
    sel.value = value;
  }
}

async function fetchModels() {
  const btn = $('btn-refresh-models');
  if (btn) btn.disabled = true;
  try {
    const resp = await fetch('/api/models');
    if (!resp.ok) return;
    const data = await resp.json();
    // 新结构 by_provider: {<id>:{label,kind,models,error}}；回退旧 siliconflow/local。
    const byProvider = data.by_provider || {};
    if (Object.keys(byProvider).length) {
      modelLists.byProvider = byProvider;
      // 汇总所有模型（供 includes 校验等），并保留 siliconflow/local 兼容引用
      modelLists.all = [];
      Object.values(byProvider).forEach(p => { modelLists.all.push(...(p.models || [])); });
      modelLists.siliconflow = (byProvider.siliconflow && byProvider.siliconflow.models) || data.siliconflow || [];
      modelLists.local = (byProvider.local && byProvider.local.models) || data.local || [];
    } else {
      // 旧结构兼容
      modelLists.byProvider = {
        siliconflow: { label: 'SiliconFlow', kind: 'cloud', models: data.siliconflow || [] },
        local: { label: '本地', kind: 'local', models: data.local || [] },
      };
      modelLists.siliconflow = data.siliconflow || [];
      modelLists.local = data.local || [];
      modelLists.all = [...modelLists.siliconflow, ...modelLists.local];
    }
    populateModelSelect('cfg-brain-model', 'cfg-brain-model-wrapper', modelLists.byProvider, originalConfig.brain_primary);
    populateModelSelect('cfg-brain-fallback', 'cfg-brain-fallback-wrapper', modelLists.byProvider, originalConfig.brain_fallback);
  } catch { /* ignore */ }
  finally { if (btn) btn.disabled = false; }
}

function populateModelSelect(selectId, wrapperId, byProvider, currentValue) {
  const sel = $(selectId);
  if (!sel) return;
  sel.innerHTML = '';
  const addGroup = (label, models) => {
    if (!models.length) return;
    const grp = document.createElement('optgroup');
    grp.label = label;
    models.forEach(m => {
      const o = document.createElement('option');
      o.value = m;
      o.textContent = m;
      grp.appendChild(o);
    });
    sel.appendChild(grp);
  };
  // 遍历所有已配接入点，每个 provider 一个分组（云端在前，本地在后，便于查找）
  const entries = Object.entries(byProvider || {});
  entries.sort((a, b) => (a[1].kind === 'local' ? 1 : 0) - (b[1].kind === 'local' ? 1 : 0));
  entries.forEach(([, p]) => addGroup(p.label || '接入点', p.models || []));
  if (!sel.options.length) {
    sel.innerHTML = '<option value="">请先配置 API Key 并刷新</option>';
  }
  if (currentValue) setModelValue(selectId, currentValue);
}

async function saveConfig() {
  const btn = $('btn-save-config');
  btn.disabled = true;
  try {
    const config = {
      brain_primary: $('cfg-brain-model').value,
      brain_fallback: $('cfg-brain-fallback').value,
      langsmith_tracing: $('cfg-langsmith-tracing')?.checked || false,
      langsmith_api_key: $('cfg-langsmith-api-key')?.value || '',
      langsmith_project: $('cfg-langsmith-project')?.value || 'swarm-dev',
      ...collectRoutingPayload(),
    };
    const resp = await fetch('/api/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json().catch(() => ({}));
    showToast('配置已保存', 'success');
    const ls = data.langsmith;
    if (ls) {
      if (ls.active) {
        showToast(`LangSmith 已启用 → 项目「${ls.project}」`, 'info');
      } else if (ls.configured === false && ($('cfg-langsmith-tracing')?.checked)) {
        showToast('LangSmith 未生效：请填写 API Key 并重启 API', 'warning');
      }
    }
    await loadConfig();
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

async function testConfig() {
  const btn = $('btn-test-config');
  const out = $('config-test-result');
  if (btn) btn.disabled = true;
  if (out) out.textContent = '测试中…';
  try {
    const resp = await fetch('/api/config/test', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'test failed');
    const lines = [
      formatTestLine('Brain 编排', data.brain_primary),
      formatTestLine('Worker 本地(medium)', data.worker_local_medium),
      formatTestLine('Worker 云端(complex)', data.worker_cloud_complex),
    ];
    if (out) {
      out.innerHTML = lines.join('<br>');
      out.style.color = data.all_ok ? 'var(--green)' : 'var(--amber)';
    }
    showToast(data.all_ok ? '全部模型连通' : '部分模型失败，见下方详情', data.all_ok ? 'success' : 'warning');
  } catch (e) {
    if (out) { out.textContent = '测试失败: ' + e.message; out.style.color = 'var(--red)'; }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function collectRoutingPayload() {
  const payload = {};
  document.querySelectorAll('.routing-select').forEach(sel => {
    const tier = sel.dataset.tier;
    const role = sel.dataset.role;
    if (!tier || !role || !sel.value) return;
    payload[role === 'primary' ? `routing_${tier}` : `routing_${tier}_fallback`] = sel.value;
  });
  return payload;
}

function buildModelOptions(current) {
  const opts = [];
  const addGroup = (label, models) => {
    if (!models.length) return;
    opts.push(`<optgroup label="${escapeHtml(label)}">`);
    models.forEach(m => {
      const sel = m === current ? ' selected' : '';
      opts.push(`<option value="${escapeHtml(m)}"${sel}>${escapeHtml(m)}</option>`);
    });
    opts.push('</optgroup>');
  };
  // 遍历所有已配接入点（云端在前，本地在后）
  const entries = Object.entries(modelLists.byProvider || {});
  entries.sort((a, b) => (a[1].kind === 'local' ? 1 : 0) - (b[1].kind === 'local' ? 1 : 0));
  entries.forEach(([, p]) => addGroup(p.label || '接入点', p.models || []));
  const allModels = modelLists.all || [...(modelLists.siliconflow || []), ...(modelLists.local || [])];
  if (current && !allModels.includes(current)) {
    opts.push(`<option value="${escapeHtml(current)}" selected>${escapeHtml(current)} (当前)</option>`);
  }
  if (!opts.length) {
    return '<option value="">请先配置 API Key 并刷新模型列表</option>';
  }
  return opts.join('');
}

async function loadRoutingTable() {
  try {
    const resp = await fetch('/api/routing');
    if (!resp.ok) return;
    const data = await resp.json();
    renderRoutingTable(data);
    renderProviders(data.providers || []);
    loadProviderCatalog();
  } catch { /* ignore */ }
}

// ─── 模型接入点(providers) 管理 ──────────────────────────────
let _providersState = [];
let _providerCatalog = [];

async function loadProviderCatalog() {
  if (_providerCatalog.length) return _providerCatalog;
  try {
    const data = await fetch('/api/model-providers/catalog').then(r => r.json());
    _providerCatalog = data.catalog || [];
    const sel = $('provider-catalog-select');
    if (sel) {
      sel.innerHTML = '<option value="">+ 从预置添加…</option>' +
        _providerCatalog.map(c => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.label || c.id)}</option>`).join('');
    }
  } catch { /* ignore */ }
  return _providerCatalog;
}

function addProviderFromCatalog(catalogId) {
  const sel = $('provider-catalog-select');
  if (sel) sel.value = '';  // 复位下拉
  if (!catalogId) return;
  const tpl = _providerCatalog.find(c => c.id === catalogId);
  if (!tpl) return;
  _syncProvidersFromDom();
  // 同 id 已存在则不重复加
  if (_providersState.some(p => p.id === tpl.id)) {
    showToast(`接入点 ${tpl.id} 已存在`, 'warning');
    return;
  }
  _providersState.push({
    id: tpl.id, label: tpl.label || '', kind: tpl.kind || 'cloud',
    base_url: tpl.base_url || '', has_key: false, api_key: '',
  });
  markProvidersDirty();
  drawProviders();
  showToast(`已添加 ${tpl.label || tpl.id}，填入 API Key 后保存`, 'info');
}

function renderProviders(providers) {
  _providersState = (providers || []).map(p => ({
    id: p.id || '', label: p.label || '', kind: p.kind || 'cloud',
    base_url: p.base_url || '', has_key: !!p.has_key, api_key: '',
  }));
  clearProvidersDirty();
  drawProviders();
}

// 未保存改动标记 —— 高亮保存按钮 + 显示提示。
function markProvidersDirty() {
  const hint = $('providers-dirty-hint');
  if (hint) hint.style.display = '';
  const btn = $('btn-save-providers');
  if (btn) btn.classList.add('btn-pulse');
}

function clearProvidersDirty() {
  const hint = $('providers-dirty-hint');
  if (hint) hint.style.display = 'none';
  const btn = $('btn-save-providers');
  if (btn) btn.classList.remove('btn-pulse');
}

function _isKnownProvider(id) {
  return _providerCatalog.some(c => c.id === id);
}

function drawProviders() {
  const el = $('providers-list');
  if (!el) return;
  if (!_providersState.length) {
    el.innerHTML = '<p style="font-size:11px;color:var(--text-muted);padding:6px">（暂无接入点。从下方"预置"添加，或"+ 空白接入点"自定义。）</p>';
    return;
  }
  // 按 kind 分组展示：云端 / 本地
  const groups = [
    { key: 'cloud', label: '☁️ 云端接入点' },
    { key: 'local', label: '💻 本地接入点' },
  ];
  let html = '';
  groups.forEach(g => {
    const rows = _providersState
      .map((p, i) => ({ p, i }))
      .filter(x => (x.p.kind || 'cloud') === g.key);
    if (!rows.length) return;
    html += `<div style="font-size:11px;font-weight:600;color:var(--text-muted);margin:10px 0 4px">${g.label}</div>`;
    html += rows.map(({ p, i }) => _renderProviderCard(p, i) + _renderCapabilitySection(p)).join('');
  });
  el.innerHTML = html;
  // 渲染后异步加载已存能力（不阻塞）
  _providersState.forEach(p => { if (p.id) loadCapabilities(p.id); });
}

// ─── 模型能力探测（设计 v3 A批3）──────────────────────────
// 每个接入点卡片下挂一个能力区：探测按钮 + 进度 + 能力表格。

function _renderCapabilitySection(p) {
  if (!p.id) return '';
  const pid = escapeHtml(p.id);
  const isLocal = (p.kind || 'cloud') === 'local';
  // 本地默认全探（免费），云端默认只探在用（省 token）。主按钮走 auto，副按钮提供另一选项。
  const primaryLabel = isLocal ? '🔍 探测全部模型' : '🔍 探测在用模型';
  const primaryTitle = isLocal
    ? '本地推理免费，探测该接入点下全部可用模型'
    : '云端按 token 计费，只探路由策略里实际用到的模型（省钱，推荐）';
  const altBtn = isLocal
    ? `<button class="btn btn-ghost btn-sm" onclick="probeProvider('${pid}', 'in_use')" title="只探路由策略在用的模型" style="opacity:0.7">仅在用</button>`
    : `<button class="btn btn-ghost btn-sm" onclick="probeProvider('${pid}', 'all')" title="探测该接入点下全部模型（云端会很慢且花 token，慎用）" style="opacity:0.7">全部模型</button>`;
  return `
    <div class="cap-section" data-cap-provider="${pid}" style="margin:-2px 0 10px;padding:6px 10px;border-left:2px solid var(--border);background:var(--bg-subtle, rgba(0,0,0,0.02))">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <button class="btn btn-ghost btn-sm" onclick="probeProvider('${pid}', 'auto')" title="${primaryTitle}">${primaryLabel}</button>
        ${altBtn}
        <span class="cap-status" data-cap-status="${pid}" style="font-size:11px;color:var(--text-muted)"></span>
      </div>
      <div class="cap-table" data-cap-table="${pid}" style="margin-top:6px"></div>
    </div>`;
}

const _capPollTimers = {};

async function probeProvider(providerId, scope) {
  scope = scope || 'auto';
  const statusEl = document.querySelector(`[data-cap-status="${providerId}"]`);
  if (scope === 'all' && !confirm(`确定探测「${providerId}」下的全部模型吗？\n若为云端聚合接入点，可能有几十上百个模型，会消耗较多 token 和时间。\n本地推理则无成本。`)) return;
  try {
    const resp = await fetch('/api/models/probe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider_id: providerId, scope }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (statusEl) statusEl.textContent = '⚠️ ' + (err.detail || resp.status);
      return;
    }
    const data = await resp.json();
    if (data.status === 'no_models_in_use') {
      if (statusEl) statusEl.textContent = 'ℹ️ ' + (data.message || '无在用模型');
      return;
    }
    if (statusEl) statusEl.textContent = '探测中…';
    _pollProbeStatus(providerId);
  } catch (e) {
    if (statusEl) statusEl.textContent = '⚠️ ' + e.message;
  }
}

function _pollProbeStatus(providerId) {
  if (_capPollTimers[providerId]) clearTimeout(_capPollTimers[providerId]);
  const statusEl = document.querySelector(`[data-cap-status="${providerId}"]`);
  const tick = async () => {
    try {
      const resp = await fetch(`/api/models/probe/status?provider_id=${encodeURIComponent(providerId)}`);
      const job = await resp.json();
      if (job.status === 'running') {
        if (statusEl) statusEl.textContent = `探测中… ${job.done}/${job.total} ${job.current || ''}`;
        _capPollTimers[providerId] = setTimeout(tick, 1000);
      } else if (job.status === 'done') {
        const r = job.result || {};
        if (statusEl) statusEl.textContent = `✅ 探测完成：${r.probed}/${r.total}` + (r.errors && r.errors.length ? `（${r.errors.length} 失败）` : '');
        loadCapabilities(providerId);
      } else if (job.status === 'error') {
        if (statusEl) statusEl.textContent = '⚠️ 探测失败：' + (job.error || '');
      }
    } catch (e) {
      if (statusEl) statusEl.textContent = '⚠️ ' + e.message;
    }
  };
  tick();
}

async function loadCapabilities(providerId) {
  const tableEl = document.querySelector(`[data-cap-table="${providerId}"]`);
  if (!tableEl) return;
  try {
    const resp = await fetch(`/api/models/capabilities?provider_id=${encodeURIComponent(providerId)}`);
    const data = await resp.json();
    const rows = data.capabilities || [];
    if (!rows.length) { tableEl.innerHTML = ''; return; }
    tableEl.innerHTML = _renderCapTable(rows);
  } catch (e) { /* 静默 */ }
}

function _sourceBadge(source) {
  const map = {
    probed: ['探测', '#22c55e'], parsed: ['解析', '#22c55e'],
    manual: ['人工', '#3b82f6'], default: ['默认/未探明', '#f59e0b'],
  };
  const [label, color] = map[source] || [source, 'var(--text-muted)'];
  return `<span style="font-size:9px;padding:1px 5px;border-radius:6px;background:${color}22;color:${color}">${escapeHtml(label)}</span>`;
}

function _renderCapTable(rows) {
  const head = `<tr style="font-size:10px;color:var(--text-muted)">
    <th style="text-align:left;padding:2px 6px">模型</th>
    <th style="padding:2px 6px">上下文</th>
    <th style="padding:2px 6px">多模态</th>
    <th style="padding:2px 6px">速度(tps)</th>
    <th style="padding:2px 6px">来源</th></tr>`;
  const body = rows.map(r => {
    const ctx = r.context_window ? (r.context_window >= 1000 ? (r.context_window / 1000).toFixed(0) + 'k' : r.context_window) : '—';
    const mm = r.supports_multimodal ? '🖼️' : '—';
    const tps = r.gen_speed_tps ? r.gen_speed_tps.toFixed(1) : '—';
    return `<tr style="font-size:11px;border-top:1px solid var(--border)">
      <td style="padding:3px 6px;font-family:monospace">${escapeHtml(r.model_id)}</td>
      <td style="text-align:center;padding:3px 6px">${ctx}</td>
      <td style="text-align:center;padding:3px 6px">${mm}</td>
      <td style="text-align:center;padding:3px 6px">${tps}</td>
      <td style="text-align:center;padding:3px 6px">${_sourceBadge(r.source)}</td></tr>`;
  }).join('');
  return `<table style="width:100%;border-collapse:collapse"><thead>${head}</thead><tbody>${body}</tbody></table>`;
}

// 每个接入点一行，第一列是预置下拉（10 预置 + 本地 + 自定义）：
//  - 选某预置云端 → 收起，只露 API Key（base_url 灰字展示）
//  - 选"本地推理(local)" → 露 Base URL + Key
//  - 选"自定义" → 展开 id/label/kind/base_url/key 全字段
function _providerSelectHtml(p, i) {
  const isLocal = p.id === 'local' && !p._custom;
  const isCustom = p._custom || (!_isKnownProvider(p.id) && !isLocal);
  const opts = _providerCatalog.map(c =>
    `<option value="${escapeHtml(c.id)}"${(!isCustom && !isLocal && p.id === c.id) ? ' selected' : ''}>${escapeHtml(c.label || c.id)}</option>`
  ).join('');
  return `<select class="form-select prov-preset" onchange="changeProviderPreset(${i}, this.value)" style="flex:0 0 180px">
    ${opts}
    <option value="__local__"${isLocal ? ' selected' : ''}>本地推理 (local)</option>
    <option value="__custom__"${isCustom ? ' selected' : ''}>自定义端点…</option>
  </select>`;
}

function _renderProviderCard(p, i) {
  const isLocal = p.id === 'local' && !p._custom;
  const isCustom = p._custom || (!_isKnownProvider(p.id) && !isLocal);
  const keyHint = p.has_key ? '已配置(留空不改)' : (p.kind === 'local' ? 'API Key（本地通常可留空）' : '填入 API Key');

  if (isLocal) {
    return `
      <div class="card prov-card" style="margin-bottom:6px;padding:8px 10px" data-pidx="${i}">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
          ${_providerSelectHtml(p, i)}
          <input class="form-input prov-f" data-f="base_url" style="flex:1" value="${escapeHtml(p.base_url)}" placeholder="http://ai.bit:3000/api" oninput="markProvidersDirty()">
          <button class="btn btn-ghost btn-sm" onclick="removeProviderRow(${i})" title="移除">✕</button>
        </div>
        <input class="form-input prov-f" data-f="api_key" type="password" value="" placeholder="${keyHint}" oninput="markProvidersDirty()">
        <input type="hidden" class="prov-f" data-f="id" value="local">
        <input type="hidden" class="prov-f" data-f="kind" value="local">
        <input type="hidden" class="prov-f" data-f="label" value="${escapeHtml(p.label || '本地推理')}">
      </div>`;
  }

  if (isCustom) {
    return `
      <div class="card prov-card" style="margin-bottom:8px;padding:10px" data-pidx="${i}">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
          ${_providerSelectHtml(p, i)}
          <button class="btn btn-danger btn-sm" style="margin-left:auto" onclick="removeProviderRow(${i})">删除</button>
        </div>
        <div class="form-row" style="gap:8px">
          <div class="form-group" style="flex:0 0 120px"><label class="form-label">ID</label>
            <input class="form-input prov-f" data-f="id" value="${escapeHtml(p.id)}" placeholder="my-provider" oninput="markProvidersDirty()"></div>
          <div class="form-group" style="flex:0 0 110px"><label class="form-label">类型</label>
            <select class="form-select prov-f" data-f="kind" onchange="markProvidersDirty()">
              <option value="cloud"${p.kind === 'cloud' ? ' selected' : ''}>云端 cloud</option>
              <option value="local"${p.kind === 'local' ? ' selected' : ''}>本地 local</option>
            </select></div>
          <div class="form-group" style="flex:1"><label class="form-label">展示名</label>
            <input class="form-input prov-f" data-f="label" value="${escapeHtml(p.label)}" placeholder="My Endpoint" oninput="markProvidersDirty()"></div>
        </div>
        <div class="form-row" style="gap:8px">
          <div class="form-group" style="flex:2"><label class="form-label">Base URL</label>
            <input class="form-input prov-f" data-f="base_url" value="${escapeHtml(p.base_url)}" placeholder="https://api.example.com/v1" oninput="markProvidersDirty()"></div>
          <div class="form-group" style="flex:1"><label class="form-label">API Key</label>
            <input class="form-input prov-f" data-f="api_key" type="password" placeholder="${keyHint}" oninput="markProvidersDirty()"></div>
        </div>
      </div>`;
  }

  // 预置云端：收起，只露 key
  const tpl = _providerCatalog.find(c => c.id === p.id) || {};
  return `
    <div class="card prov-card" style="margin-bottom:6px;padding:8px 10px" data-pidx="${i}">
      <div style="display:flex;gap:8px;align-items:center">
        ${_providerSelectHtml(p, i)}
        <input class="form-input prov-f" data-f="api_key" type="password" style="flex:1" placeholder="${keyHint}" oninput="markProvidersDirty()">
        <button class="btn btn-ghost btn-sm" onclick="removeProviderRow(${i})" title="移除">✕</button>
      </div>
      <div style="font-size:10px;color:var(--text-muted);margin-top:3px;padding-left:2px">${escapeHtml(p.base_url || tpl.base_url || '')}</div>
      <input type="hidden" class="prov-f" data-f="id" value="${escapeHtml(p.id)}">
      <input type="hidden" class="prov-f" data-f="kind" value="${escapeHtml(p.kind || 'cloud')}">
      <input type="hidden" class="prov-f" data-f="base_url" value="${escapeHtml(p.base_url || tpl.base_url || '')}">
      <input type="hidden" class="prov-f" data-f="label" value="${escapeHtml(p.label || tpl.label || '')}">
    </div>`;
}

// 行内切换预置：选某预置 → 重填该行 base_url/label/kind（key 重置，因为换了接入点）。
function changeProviderPreset(i, presetId) {
  _syncProvidersFromDom();
  if (!_providersState[i]) return;
  const cur = _providersState[i];
  if (presetId === '__custom__') {
    cur._custom = true;
    if (_isKnownProvider(cur.id)) { cur.id = ''; cur.label = ''; }
    cur.kind = cur.kind || 'cloud';
  } else if (presetId === '__local__') {
    cur._custom = false;
    cur.id = 'local'; cur.kind = 'local'; cur.label = '本地推理';
    if (!cur.base_url) cur.base_url = 'http://ai.bit:3000/api';
  } else {
    const tpl = _providerCatalog.find(c => c.id === presetId);
    if (!tpl) return;
    if (_providersState.some((x, idx) => idx !== i && x.id === tpl.id && !x._custom)) {
      showToast(`接入点 ${tpl.label || tpl.id} 已存在`, 'warning');
      drawProviders();
      return;
    }
    cur._custom = false;
    cur.id = tpl.id; cur.label = tpl.label || ''; cur.kind = tpl.kind || 'cloud';
    cur.base_url = tpl.base_url || '';
    cur.has_key = false;
  }
  markProvidersDirty();
  drawProviders();
}

function _syncProvidersFromDom() {
  document.querySelectorAll('#providers-list [data-pidx]').forEach(card => {
    const i = parseInt(card.dataset.pidx, 10);
    if (Number.isNaN(i) || !_providersState[i]) return;
    card.querySelectorAll('.prov-f').forEach(f => {
      _providersState[i][f.dataset.f] = f.value;
    });
  });
}

function addProviderRow() {
  _syncProvidersFromDom();
  _providersState.push({ id: '', label: '', kind: 'cloud', base_url: '', has_key: false, api_key: '', _custom: true });
  markProvidersDirty();
  drawProviders();
}

function removeProviderRow(i) {
  _syncProvidersFromDom();
  _providersState.splice(i, 1);
  markProvidersDirty();
  drawProviders();
}

async function saveProviders() {
  _syncProvidersFromDom();
  const btn = $('btn-save-providers');
  if (btn) btn.disabled = true;
  try {
    // 过滤空 id；api_key 留空时不发送(后端保留原 key)
    const providers = _providersState
      .filter(p => (p.id || '').trim())
      .map(p => {
        const o = { id: p.id.trim(), label: p.label || '', kind: p.kind || 'cloud', base_url: p.base_url || '' };
        if (p.api_key) o.api_key = p.api_key;  // 仅在用户输入了新 key 时发送
        return o;
      });
    const resp = await fetch('/api/model-providers', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ providers }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();
    renderProviders(data.providers || []);
    showToast('接入点已保存', 'success');
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderRoutingTable(data) {
  const container = $('routing-table');
  if (!container) return;
  const tiers = data.tiers || {};
  container.innerHTML = ROUTING_TIER_DEFS.map(t => {
    const cfg = tiers[t.key] || {};
    return `
      <div class="card" style="margin-bottom:8px;padding:12px">
        <div style="font-size:12px;font-weight:600;margin-bottom:4px">${escapeHtml(t.label)}</div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:8px">${escapeHtml(t.hint)}</div>
        <div class="form-row">
          <div class="form-group">
            <label class="form-label">首选模型</label>
            <select class="form-select routing-select" data-tier="${t.key}" data-role="primary">${buildModelOptions(cfg.primary || '')}</select>
          </div>
          <div class="form-group">
            <label class="form-label">备选模型</label>
            <select class="form-select routing-select" data-tier="${t.key}" data-role="fallback">${buildModelOptions(cfg.fallback || '')}</select>
          </div>
        </div>
      </div>`;
  }).join('');
}

async function saveRouting() {
  /* 已合并到 saveConfig */
}

// ─── KB Embedding / Rerank 接入点（方案 A）──────────────────────
let _kbCatalog = { embed: [], rerank: [] };

async function loadKbEmbedRerank() {
  try {
    // catalog 填充两个预置下拉
    const cat = await fetch('/api/kb/embed-rerank/catalog').then(r => r.json());
    _kbCatalog = { embed: cat.embed || [], rerank: cat.rerank || [] };
    const fill = (selId, items) => {
      const sel = $(selId);
      if (!sel) return;
      sel.innerHTML = '<option value="">选择预置自动填…</option>' +
        items.map(c => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.label || c.id)}</option>`).join('');
    };
    fill('kb-embed-catalog', _kbCatalog.embed);
    fill('kb-rerank-catalog', _kbCatalog.rerank);
    // 当前配置回填
    const cur = await fetch('/api/kb/embed-rerank').then(r => r.json());
    const e = cur.embed || {}, r = cur.rerank || {};
    const set = (id, v) => { const el = $(id); if (el) el.value = v ?? ''; };
    set('kb-embed-base-url', e.base_url); set('kb-embed-model', e.model);
    set('kb-embed-format', e.format || 'openai'); set('kb-embed-reuse', e.reuse_provider);
    set('kb-rerank-url', r.url); set('kb-rerank-model', r.model);
    set('kb-rerank-format', r.format || 'simple'); set('kb-rerank-reuse', r.reuse_provider);
    // 检索调优回填
    const rt = cur.retrieval || {};
    set('kb-retrieval-top-k', rt.retrieval_top_k);
    set('kb-rerank-top-k', rt.rerank_top_k);
    set('kb-semantic-threshold', rt.semantic_score_threshold);
    set('kb-priority-file-top-k', rt.priority_file_top_k);
    set('kb-max-priority-files', rt.max_priority_files);
    set('kb-chunk-size', rt.chunk_size);
    set('kb-chunk-overlap', rt.chunk_overlap);
    // key 已配则 placeholder 提示（不回填明文）
    const ek = $('kb-embed-api-key'), rk = $('kb-rerank-api-key');
    if (ek) { ek.value = ''; ek.placeholder = e.has_key ? '已配置（留空不改）' : 'API Key（自建可留空）'; }
    if (rk) { rk.value = ''; rk.placeholder = r.has_key ? '已配置（留空不改）' : 'API Key（自建可留空）'; }
  } catch { /* ignore */ }
}

function applyKbCatalog(kind, catalogId) {
  if (!catalogId) return;
  const item = (_kbCatalog[kind] || []).find(c => c.id === catalogId);
  if (!item) return;
  if (kind === 'embed') {
    if (item.base_url) $('kb-embed-base-url').value = item.base_url;
    if (item.model) $('kb-embed-model').value = item.model;
    $('kb-embed-format').value = item.format || 'openai';
  } else {
    if (item.base_url) $('kb-rerank-url').value = item.base_url;
    if (item.model) $('kb-rerank-model').value = item.model;
    $('kb-rerank-format').value = item.format || 'simple';
  }
  showToast(`已填入 ${item.label || item.id}，填 Key 后保存`, 'info');
}

async function saveKbEmbedRerank() {
  const btn = $('btn-save-kb-er');
  const out = $('kb-er-result');
  if (btn) btn.disabled = true;
  try {
    const v = id => ($(id)?.value || '').trim();
    const body = {
      embed: {
        base_url: v('kb-embed-base-url'), model: v('kb-embed-model'),
        format: v('kb-embed-format') || 'openai', reuse_provider: v('kb-embed-reuse'),
      },
      rerank: {
        url: v('kb-rerank-url'), model: v('kb-rerank-model'),
        format: v('kb-rerank-format') || 'simple', reuse_provider: v('kb-rerank-reuse'),
      },
    };
    // key 有填才传（空=不改，保留原）
    const ek = v('kb-embed-api-key'), rk = v('kb-rerank-api-key');
    if (ek) body.embed.api_key = ek;
    if (rk) body.rerank.api_key = rk;
    // 检索调优：只提交填了值的字段（空=不改，保留原），数值由后端做范围校验
    const retrieval = {};
    const numFields = {
      'kb-retrieval-top-k': 'retrieval_top_k',
      'kb-rerank-top-k': 'rerank_top_k',
      'kb-semantic-threshold': 'semantic_score_threshold',
      'kb-priority-file-top-k': 'priority_file_top_k',
      'kb-max-priority-files': 'max_priority_files',
      'kb-chunk-size': 'chunk_size',
      'kb-chunk-overlap': 'chunk_overlap',
    };
    for (const [elId, key] of Object.entries(numFields)) {
      const raw = v(elId);
      if (raw !== '') retrieval[key] = Number(raw);
    }
    if (Object.keys(retrieval).length) body.retrieval = retrieval;
    const resp = await fetch('/api/kb/embed-rerank', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '保存失败');
    showToast('Embedding/Rerank 已保存', 'success');
    if (data.embed_model_changed && out) {
      out.innerHTML = `<span style="color:var(--orange)">⚠ ${escapeHtml(data.reprocess_hint || 'Embedding 模型已变更，建议重新预处理所有项目')}</span>`;
    } else if (out) {
      out.textContent = '已保存并生效';
    }
    loadKbEmbedRerank();
  } catch (e) {
    showToast(e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ─── Sandbox ─────────────────────────────────────────────────
