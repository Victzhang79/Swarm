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
    const sfKey = flat.siliconflow_api_key || '';
    const localKey = flat.local_api_key || '';
    $('cfg-sf-api-key').value = sfKey.includes('...') ? '' : sfKey;
    $('cfg-sf-api-key').placeholder = sfKey.includes('...') ? sfKey : 'SiliconFlow API Key';
    $('cfg-sf-base-url').value = flat.siliconflow_base_url || '';
    $('cfg-local-api-key').value = localKey.includes('...') ? '' : localKey;
    $('cfg-local-api-key').placeholder = localKey.includes('...') ? localKey : '本地 API Key';
    $('cfg-local-base-url').value = flat.local_base_url || '';
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
      siliconflow_api_key: $('cfg-sf-api-key').value,
      siliconflow_base_url: $('cfg-sf-base-url').value,
      local_api_key: $('cfg-local-api-key').value,
      local_base_url: $('cfg-local-base-url').value,
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
    renderRoutingTable(await resp.json());
  } catch { /* ignore */ }
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
