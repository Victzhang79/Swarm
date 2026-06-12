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
    modelLists.siliconflow = data.siliconflow || [];
    modelLists.local = data.local || [];
    populateModelSelect('cfg-brain-model', 'cfg-brain-model-wrapper', modelLists.siliconflow, modelLists.local, originalConfig.siliconflow_api_key, originalConfig.local_api_key, originalConfig.brain_primary);
    populateModelSelect('cfg-brain-fallback', 'cfg-brain-fallback-wrapper', modelLists.siliconflow, modelLists.local, originalConfig.siliconflow_api_key, originalConfig.local_api_key, originalConfig.brain_fallback);
  } catch { /* ignore */ }
  finally { if (btn) btn.disabled = false; }
}

function populateModelSelect(selectId, wrapperId, sfModels, localModels, hasSfKey, hasLocalKey, currentValue) {
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
  if (sfModels.length) addGroup('SiliconFlow', sfModels);
  if (localModels.length) addGroup('本地', localModels);
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
  addGroup('SiliconFlow 云端', modelLists.siliconflow);
  addGroup('本地', modelLists.local);
  if (current && !modelLists.siliconflow.includes(current) && !modelLists.local.includes(current)) {
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
    html += rows.map(({ p, i }) => _renderProviderCard(p, i)).join('');
  });
  el.innerHTML = html;
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

// ─── Sandbox ─────────────────────────────────────────────────
