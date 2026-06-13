/* Swarm Web UI — tabs/system module (split from app.js, shared global scope) */
'use strict';

function resetSandboxDetail() {
  selectedSandboxId = null;
  sandboxCurrentPath = '/workspace';
  sandboxSelectedFile = null;
  const empty = $('sandbox-detail-empty');
  const panel = $('sandbox-detail-panel');
  if (empty) empty.classList.remove('hidden');
  if (panel) panel.classList.add('hidden');
}

async function checkHealth() {
  const dot = $('health-dot');
  const text = $('health-text');
  try {
    const resp = await fetch('/api/health');
    const data = await resp.json();
    // 动态版本号（单一真相源 = swarm.__version__ / pyproject）
    if (data.version) {
      const vb = $('version-badge');
      if (vb) vb.textContent = 'v' + data.version;
    }
    if (data.status === 'ok' || resp.ok) {
      dot.className = 'health-dot ok';
      text.textContent = '在线';
    } else {
      dot.className = 'health-dot err';
      text.textContent = '异常';
    }
  } catch {
    dot.className = 'health-dot err';
    text.textContent = '离线';
  }
}

async function fetchStatus() {
  try {
    const resp = await fetch('/api/status');
    if (!resp.ok) return;
    const data = await resp.json();
    renderComponents(data.components || []);
  } catch { /* ignore */ }
}

async function loadSystemStats() {
  try {
    const url = selectedProjectId
      ? '/api/stats?project_id=' + encodeURIComponent(selectedProjectId)
      : '/api/stats';
    const resp = await fetch(url);
    if (!resp.ok) return;
    const data = await resp.json();
    const set = (id, val) => { const el = $(id); if (el) el.textContent = val; };
    set('stat-total', data.total_tasks ?? 0);
    set('stat-completed', data.completed ?? 0);
    set('stat-failed', data.failed ?? 0);
    set('stat-merge-rate', formatAcceptRate(data.merge_rate));
    set('stat-accept-rate', formatAcceptRate(data.accept_rate));
    set('stat-avg-duration', formatDurationSeconds(data.avg_duration_seconds));
    set('stat-total-tokens', formatTokenCount(data.total_tokens));
    set('stat-avg-tokens', formatTokenCount(data.avg_tokens));
    const trendEl = $('stat-learning-trend');
    if (trendEl) {
      const eff = data.learning_effectiveness;
      trendEl.innerHTML = eff ? learningTrendBadge(eff.trend) : learningTrendBadge('unknown');
      if (eff && eff.trend !== 'unknown') {
        trendEl.title = `近30天 ${eff.recent_mistakes} · 前30天 ${eff.prior_mistakes}`;
      }
    }
    // 项目级定制沙箱：展示当前项目专属模板 ID（系统在预处理时按真实环境构建并配置）。
    renderProjectSandboxTemplate(data.sandbox_template, data.sandbox_deps_hash);
  } catch { /* ignore */ }
}

// 渲染「当前项目专属沙箱模板」块（项目统计/关联沙箱页）。
// 一个模板可开多个沙箱，这里明确标注本项目【使用】的专属模板 ID。
function renderProjectSandboxTemplate(templateId, depsHash) {
  const el = $('project-sandbox-template');
  if (!el) return;
  if (templateId) {
    el.innerHTML = `
      <div class="psbtpl-row">
        <span class="psbtpl-label">本项目专属模板</span>
        <code class="psbtpl-id" title="一个模板可开多个沙箱，下方列表为基于此模板的活跃实例">${escapeHtml(templateId)}</code>
        <button class="btn btn-ghost btn-xs" onclick="navigator.clipboard.writeText('${escapeHtml(templateId)}').then(()=>showToast('已复制模板 ID','success'))" title="复制模板 ID">复制</button>
        ${depsHash ? `<span class="psbtpl-hash hint" title="依赖指纹：依赖变化时重建模板">deps:${escapeHtml(depsHash)}</span>` : ''}
      </div>`;
  } else {
    el.innerHTML = `
      <div class="psbtpl-row psbtpl-empty">
        <span class="psbtpl-label">本项目专属模板</span>
        <span class="hint">未配置（使用通用语言模板；启用项目级定制沙箱后预处理时自动构建）</span>
      </div>`;
  }
}

// ─── 上层系统级（全局，不依赖项目）──────────────────────────
// 全局任务统计：调 /api/stats（无 project_id = 所有项目汇总）。
async function loadGlobalStats() {
  try {
    const resp = await fetch('/api/stats');
    if (!resp.ok) return;
    const data = await resp.json();
    const set = (id, val) => { const el = $(id); if (el) el.textContent = val; };
    set('gstat-total', data.total_tasks ?? 0);
    set('gstat-completed', data.completed ?? 0);
    set('gstat-failed', data.failed ?? 0);
    set('gstat-merge-rate', formatAcceptRate(data.merge_rate));
    set('gstat-accept-rate', formatAcceptRate(data.accept_rate));
    set('gstat-avg-duration', formatDurationSeconds(data.avg_duration_seconds));
    set('gstat-total-tokens', formatTokenCount(data.total_tokens));
    const trendEl = $('gstat-learning-trend');
    if (trendEl) {
      const eff = data.learning_effectiveness;
      trendEl.innerHTML = eff ? learningTrendBadge(eff.trend) : learningTrendBadge('unknown');
    }
  } catch { /* ignore */ }
}

// 上层系统 tab 入口：全局统计 + 组件健康 + 全局沙箱运维。
function loadGlobalSystemTab() {
  loadGlobalStats();
  fetchStatus();
  if (typeof refreshOrphanCount === 'function') refreshOrphanCount();
  if (typeof refreshPoolStatus === 'function') refreshPoolStatus();
}

function startSystemRefresh() {
  stopSystemRefresh();
  loadGlobalSystemTab();
  systemStatsInterval = setInterval(async () => {
    if (typeof currentTopTab === 'undefined' || currentTopTab !== 'system') return;
    await loadGlobalStats();
    fetchStatus();
  }, 30000);
}

function stopSystemRefresh() {
  if (systemStatsInterval) {
    clearInterval(systemStatsInterval);
    systemStatsInterval = null;
  }
}

// ─── Config & Models ─────────────────────────────────────────

function renderSandboxConfig(config) {
  const el = $('sandbox-config');
  if (!el) return;
  if (!config) {
    el.innerHTML = '';
    return;
  }
  el.innerHTML = `
    <div class="sandbox-config-grid">
      <div><span class="label">API</span><code>${escapeHtml(config.api_url || '-')}</code></div>
      <div><span class="label">Proxy</span><code>${escapeHtml(config.proxy_base || '-')}</code></div>
      <div><span class="label">Worker</span><span class="pill ${config.use_for_worker ? 'pill-green' : 'pill-gray'}">${config.use_for_worker ? '沙箱执行' : '本地执行'}</span></div>
    </div>`;
}

async function refreshSandboxes(projectId) {
  const list = $('sandbox-list');
  const pid = projectId || selectedProjectId;
  if (!pid) {
    if (list) list.innerHTML = '<p style="font-size:12px;color:var(--text-muted);padding:8px">请先在左侧选择项目</p>';
    resetSandboxDetail();
    return;
  }
  try {
    const resp = await fetch('/api/sandbox/status?project_id=' + encodeURIComponent(pid));
    if (!resp.ok) throw new Error('fetch failed');
    const data = await resp.json();
    renderSandboxConfig(data.config);
    const sandboxes = data.sandboxes || data.active || data || [];
    const arr = Array.isArray(sandboxes) ? sandboxes : Object.values(sandboxes);
    if (!arr.length) {
      list.innerHTML = '<p style="font-size:12px;color:var(--text-muted);padding:8px">无活跃沙箱</p>';
      resetSandboxDetail();
      return;
    }
    const ids = arr.map(sb => sb.id || sb.sandbox_id || String(sb));
    if (!selectedSandboxId || !ids.includes(selectedSandboxId)) {
      selectedSandboxId = ids[0];
      sandboxCurrentPath = '/workspace';
      sandboxSelectedFile = null;
    }
    list.innerHTML = arr.map(sb => {
      const id = sb.id || sb.sandbox_id || String(sb);
      const selected = id === selectedSandboxId;
      const meta = [
        sb.status ? `<span class="pill ${sb.status === 'running' ? 'pill-green' : 'pill-gray'}">${escapeHtml(sb.status)}</span>` : '',
        sb.template_id && sb.template_id !== '-' ? `<span class="pill pill-gray">${escapeHtml(String(sb.template_id).substring(0, 18))}</span>` : '',
        sb.cpu_count != null ? `<span class="pill pill-gray">${sb.cpu_count} CPU</span>` : '',
        sb.memory_mb != null ? `<span class="pill pill-gray">${sb.memory_mb} MB</span>` : '',
        sb.started_at && sb.started_at !== '-' ? `<span style="font-size:11px;color:var(--text-muted)">${escapeHtml(sb.started_at)}</span>` : '',
      ].filter(Boolean).join(' ');
      return `
        <div class="sandbox-row${selected ? ' selected' : ''}" data-sandbox-id="${escapeHtml(String(id))}">
          <div class="sandbox-row-main">
            <span class="sandbox-id">${escapeHtml(String(id))}</span>
            <div class="sandbox-meta">${meta}</div>
          </div>
          <div class="sandbox-row-actions">
            <button type="button" class="btn btn-danger btn-sm" data-action="destroy">销毁</button>
          </div>
        </div>`;
    }).join('');
    list.querySelectorAll('.sandbox-row').forEach(row => {
      row.addEventListener('click', () => selectSandbox(row.dataset.sandboxId));
      const destroyBtn = row.querySelector('[data-action="destroy"]');
      if (destroyBtn) {
        destroyBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          destroySandbox(row.dataset.sandboxId);
        });
      }
    });
    if (selectedSandboxId) {
      showSandboxDetailPanel(selectedSandboxId, arr.find(sb => (sb.id || sb.sandbox_id) === selectedSandboxId));
    }
  } catch {
    if (list) list.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">加载失败 — 请检查 CubeSandbox 连接</p>';
    resetSandboxDetail();
  }
}

function showSandboxDetailPanel(sandboxId, meta) {
  const empty = $('sandbox-detail-empty');
  const panel = $('sandbox-detail-panel');
  const idEl = $('sandbox-detail-id');
  const metaEl = $('sandbox-detail-meta');
  if (empty) empty.classList.add('hidden');
  if (panel) panel.classList.remove('hidden');
  if (idEl) idEl.textContent = sandboxId;
  if (metaEl && meta) {
    const parts = [];
    if (meta.status) parts.push(meta.status);
    if (meta.task_id) parts.push('task ' + meta.task_id);
    if (meta.source) parts.push(meta.source);
    metaEl.textContent = parts.join(' · ');
  } else if (metaEl) {
    metaEl.textContent = '';
  }
  loadSandboxWorkspace(sandboxCurrentPath);
  loadSandboxLogs(sandboxId);
}

function selectSandbox(sandboxId) {
  if (!sandboxId || sandboxId === selectedSandboxId) return;
  selectedSandboxId = sandboxId;
  sandboxCurrentPath = '/workspace';
  sandboxSelectedFile = null;
  document.querySelectorAll('.sandbox-row').forEach(row => {
    row.classList.toggle('selected', row.dataset.sandboxId === sandboxId);
  });
  showSandboxDetailPanel(sandboxId);
}

function reloadSandboxWorkspace() {
  if (!selectedSandboxId) return;
  loadSandboxWorkspace(sandboxCurrentPath);
}

function renderSandboxBreadcrumb(path) {
  const el = $('sandbox-breadcrumb');
  if (!el) return;
  const norm = path || '/';
  const parts = norm.split('/').filter(Boolean);
  let html = `<a data-path="/">/</a>`;
  let acc = '';
  for (const part of parts) {
    acc += '/' + part;
    const p = acc;
    html += ` / <a data-path="${escapeHtml(p)}">${escapeHtml(part)}</a>`;
  }
  el.innerHTML = html;
  el.querySelectorAll('a[data-path]').forEach(a => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      navigateSandboxPath(a.getAttribute('data-path') || '/');
    });
  });
}

function navigateSandboxPath(path) {
  sandboxCurrentPath = path || '/workspace';
  sandboxSelectedFile = null;
  loadSandboxWorkspace(sandboxCurrentPath);
}

async function loadSandboxWorkspace(path) {
  if (!selectedSandboxId) return;
  const listEl = $('sandbox-file-list');
  const previewEl = $('sandbox-file-preview');
  const dir = path || sandboxCurrentPath || '/workspace';
  sandboxCurrentPath = dir;
  renderSandboxBreadcrumb(dir);
  if (listEl) listEl.innerHTML = '<p style="padding:8px;font-size:12px;color:var(--text-muted)">加载中…</p>';
  try {
    const resp = await fetch(
      '/api/sandbox/' + encodeURIComponent(selectedSandboxId) +
      '/files?path=' + encodeURIComponent(dir)
    );
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || '无法列出目录');
    }
    const data = await resp.json();
    const files = data.files || [];
    if (!files.length) {
      if (listEl) listEl.innerHTML = '<p style="padding:8px;font-size:12px;color:var(--text-muted)">空目录</p>';
      if (previewEl && !sandboxSelectedFile) {
        previewEl.innerHTML = '<p class="hint">目录为空</p>';
      }
      return;
    }
    files.sort((a, b) => {
      const ad = a.is_dir ? 0 : 1;
      const bd = b.is_dir ? 0 : 1;
      if (ad !== bd) return ad - bd;
      return String(a.name || '').localeCompare(String(b.name || ''));
    });
    if (listEl) {
      listEl.innerHTML = files.map(f => {
        const name = f.name || f.path || '?';
        const fpath = f.path || (dir.replace(/\/$/, '') + '/' + name);
        const isDir = !!f.is_dir;
        const active = sandboxSelectedFile === fpath ? ' active' : '';
        const icon = isDir ? '📁' : '📄';
        const size = !isDir && f.size != null ? `<span class="size">${formatBytes(f.size)}</span>` : '';
        return `<div class="sandbox-file-row${active}" data-path="${escapeHtml(fpath)}" data-dir="${isDir ? '1' : '0'}">
          <span class="icon">${icon}</span>
          <span class="name">${escapeHtml(name)}</span>
          ${size}
        </div>`;
      }).join('');
      listEl.querySelectorAll('.sandbox-file-row').forEach(row => {
        row.addEventListener('click', () => {
          const fpath = row.getAttribute('data-path');
          const isDir = row.getAttribute('data-dir') === '1';
          if (isDir) {
            navigateSandboxPath(fpath);
          } else {
            sandboxSelectedFile = fpath;
            listEl.querySelectorAll('.sandbox-file-row').forEach(r => r.classList.remove('active'));
            row.classList.add('active');
            loadSandboxFileContent(fpath);
          }
        });
      });
    }
  } catch (e) {
    if (listEl) listEl.innerHTML = `<p style="padding:8px;font-size:12px;color:var(--red)">${escapeHtml(e.message)}</p>`;
  }
}

async function loadSandboxFileContent(path) {
  if (!selectedSandboxId || !path) return;
  const previewEl = $('sandbox-file-preview');
  if (previewEl) previewEl.innerHTML = '<p class="hint">加载文件…</p>';
  try {
    const resp = await fetch(
      '/api/sandbox/' + encodeURIComponent(selectedSandboxId) +
      '/files/content?path=' + encodeURIComponent(path)
    );
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || '无法读取文件');
    }
    const data = await resp.json();
    if (data.encoding === 'base64') {
      if (previewEl) {
        previewEl.innerHTML = `<p class="hint">${escapeHtml(path)}</p>
          <p style="color:var(--orange);font-size:12px">二进制文件（${formatBytes((data.content || '').length * 0.75)}），未解码展示。请通过 Worker 或 CLI 下载。</p>`;
      }
      return;
    }
    const text = data.content != null ? String(data.content) : '';
    if (previewEl) {
      previewEl.innerHTML = `<div style="font-size:10px;color:var(--text-muted);margin-bottom:6px">${escapeHtml(path)} · ${text.split('\n').length} 行</div>${escapeHtml(text)}`;
    }
  } catch (e) {
    if (previewEl) previewEl.innerHTML = `<p style="color:var(--red)">${escapeHtml(e.message)}</p>`;
  }
}

async function loadSandboxLogs(sandboxId) {
  const sid = sandboxId || selectedSandboxId;
  const panel = $('sandbox-log-panel');
  if (!sid || !panel) return;
  panel.innerHTML = '<div class="log-line info">加载日志…</div>';
  try {
    const resp = await fetch('/api/sandbox/' + encodeURIComponent(sid) + '/logs?limit=200');
    if (!resp.ok) {
      let detail = '';
      try {
        const err = await resp.json();
        detail = err.detail ? (typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail)) : '';
      } catch { /* ignore */ }
      if (resp.status === 404 && (!detail || detail === 'Not Found')) {
        throw new Error('日志接口未就绪 (404) — 请重启 API 服务以加载最新代码');
      }
      throw new Error(detail || `无法加载日志 (HTTP ${resp.status})`);
    }
    const data = await resp.json();
    const logs = data.logs || [];
    if (!logs.length) {
      panel.innerHTML = '<div class="log-line info">暂无活动日志 — Worker 执行或 run_code 后会在此显示</div>';
      return;
    }
    panel.innerHTML = logs.map(entry => {
      const kind = entry.kind || 'info';
      const ts = entry.ts ? formatLogTime(entry.ts) : '';
      const msg = entry.message || '';
      const parts = [];
      if (entry.code) {
        parts.push(`<pre>${escapeHtml(entry.code)}</pre>`);
      }
      if (entry.stdout) {
        parts.push(`<div style="color:var(--green);margin-top:4px">stdout:</div><pre>${escapeHtml(entry.stdout)}</pre>`);
      }
      if (entry.stderr) {
        parts.push(`<div style="color:var(--orange);margin-top:4px">stderr:</div><pre>${escapeHtml(entry.stderr)}</pre>`);
      }
      if (entry.error) {
        parts.push(`<div style="color:var(--red);margin-top:4px">error: ${escapeHtml(entry.error)}</div>`);
      }
      return `<div class="sandbox-log-entry ${escapeHtml(kind)}">
        <span class="log-ts">${escapeHtml(ts)}</span>
        <span class="log-kind">${escapeHtml(kind)}</span>
        <span class="log-msg">${escapeHtml(msg)}</span>
        ${parts.join('')}
      </div>`;
    }).join('');
    panel.scrollTop = panel.scrollHeight;
  } catch (e) {
    panel.innerHTML = `<div class="log-line error">${escapeHtml(e.message)}</div>`;
  }
}

async function createSandbox() {
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  try {
    const resp = await fetch('/api/sandbox/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_id: selectedProjectId }),
    });
    if (!resp.ok) throw new Error('创建失败');
    showToast('沙箱已创建', 'success');
    refreshSandboxes(selectedProjectId);
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function destroySandbox(id) {
  try {
    await fetch('/api/sandbox/' + encodeURIComponent(id), { method: 'DELETE' });
    if (selectedSandboxId === id) resetSandboxDetail();
    refreshSandboxes(selectedProjectId);
  } catch { /* ignore */ }
}

async function destroyAllSandboxes() {
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  if (!confirm('确定销毁当前项目的所有沙箱？')) return;
  try {
    const resp = await fetch('/api/sandbox/status?project_id=' + encodeURIComponent(selectedProjectId));
    const data = await resp.json();
    const sandboxes = data.sandboxes || data.active || [];
    const arr = Array.isArray(sandboxes) ? sandboxes : Object.values(sandboxes);
    for (const sb of arr) {
      const id = sb.id || sb.sandbox_id;
      if (id) await fetch('/api/sandbox/' + encodeURIComponent(id), { method: 'DELETE' });
    }
    showToast('已销毁', 'success');
    resetSandboxDetail();
    refreshSandboxes(selectedProjectId);
  } catch { /* ignore */ }
}

// ── 全局沙箱运维（系统设置抽屉，不跟项目走）──────────────
// 孤儿沙箱 = 服务端在跑但无项目/任务关联的沙箱。
async function refreshOrphanCount() {
  const cntEl = document.getElementById('orphan-count');
  const totEl = document.getElementById('orphan-total');
  if (cntEl) cntEl.textContent = '…';
  try {
    const resp = await fetch('/api/sandbox/orphans');
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'HTTP ' + resp.status);
    if (cntEl) cntEl.textContent = String(data.orphan_count ?? 0);
    if (totEl) totEl.textContent = ` / 服务端共 ${data.total ?? 0} 个`;
  } catch (e) {
    if (cntEl) cntEl.textContent = '?';
    if (totEl) totEl.textContent = ' (获取失败)';
  }
}

async function cleanupOrphanSandboxes(global) {
  if (!confirm('清理孤儿沙箱？只销毁无项目/任务关联的沙箱，不影响正在使用的。')) return;
  const btnId = global ? 'btn-g-cleanup-orphans' : 'btn-cleanup-orphans';
  const btn = document.getElementById(btnId);
  if (btn) { btn.disabled = true; btn.textContent = '清理中…'; }
  try {
    const resp = await fetch('/api/sandbox/cleanup?orphans_only=true', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '清理失败');
    showToast(`已清理 ${data.killed} 个孤儿沙箱${data.failed ? `（${data.failed} 个失败）` : ''}`, 'success');
    if (global && typeof refreshGlobalSandboxes === 'function') await refreshGlobalSandboxes();
    else await refreshOrphanCount();
  } catch (e) {
    showToast('清理失败: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🧹 清理孤儿'; }
  }
}

// ─── 热沙箱池开关（设置 tab）────────────────────────────────
async function togglePoolEnabled(enabled) {
  const statusEl = document.getElementById('pool-toggle-status');
  const cb = document.getElementById('cfg-pool-enabled');
  if (statusEl) statusEl.textContent = '应用中…';
  if (cb) cb.disabled = true;
  try {
    const resp = await fetch('/api/sandbox/pool/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !!enabled }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'HTTP ' + resp.status);
    if (cb) cb.checked = !!data.pool_enabled;
    if (statusEl) statusEl.textContent = data.pool_enabled ? '已启用（reaper ' + (data.reaper || '') + '）' : '已关闭';
    showToast(data.pool_enabled ? '热沙箱池已启用' : '热沙箱池已关闭', 'success');
    // 同步刷新系统 tab 的池状态卡（若可见）
    if (typeof refreshPoolStatus === 'function') refreshPoolStatus();
  } catch (e) {
    if (statusEl) statusEl.textContent = '失败: ' + e.message;
    if (cb) cb.checked = !enabled;  // 回滚 UI
    showToast('切换失败: ' + e.message, 'error');
  } finally {
    if (cb) cb.disabled = false;
  }
}

// 读当前池启用状态，同步设置 tab 的 checkbox。
async function syncPoolToggleState() {
  const cb = document.getElementById('cfg-pool-enabled');
  if (!cb) return;
  try {
    const data = await fetch('/api/sandbox/pool').then(r => r.json());
    cb.checked = !!data.pool_enabled;
    const statusEl = document.getElementById('pool-toggle-status');
    if (statusEl) statusEl.textContent = data.pool_enabled ? '已启用' : '已关闭';
  } catch { /* ignore */ }
}

// ─── 全局热沙箱池状态卡 ───────────────────────────────────────
async function refreshPoolStatus() {
  const el = document.getElementById('pool-status-card');
  if (!el) return;
  el.textContent = '加载中…';
  try {
    const resp = await fetch('/api/sandbox/pool');
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'HTTP ' + resp.status);
    if (!data.pool_enabled) {
      el.innerHTML = '<span class="pill pill-gray">未启用</span> 在「设置」tab 的「热沙箱池」处可一键开启。'
        + `<div style="margin-top:6px">服务端沙箱 ${data.server_total ?? 0} 个，孤儿 ${data.orphan_count ?? 0} 个。</div>`;
      return;
    }
    const p = data.pool || {};
    const buckets = p.idle_by_template || {};
    const bucketRows = Object.keys(buckets).length
      ? Object.entries(buckets).map(([k, v]) => `<code>${escapeHtml(k.substring(0, 16) || '(default)')}</code>: ${v} 待命`).join(' · ')
      : '（暂无待命沙箱）';
    el.innerHTML = `
      <div><span class="pill pill-green">已启用</span></div>
      <div style="margin-top:6px">借出 <b>${p.borrowed ?? 0}</b> · 空闲 <b>${p.total_idle ?? 0}</b> · 总计 <b>${p.total ?? 0}</b>/${p.max_total ?? '?'} · 历史创建 ${p.created_total ?? 0}</div>
      <div style="margin-top:6px">按语言桶: ${bucketRows}</div>
      <div style="margin-top:6px;color:var(--text-muted)">服务端共 ${data.server_total ?? 0} 个 · 孤儿 ${data.orphan_count ?? 0} 个 · TTL ${p.ttl_seconds ?? '?'}s / 空闲 ${p.idle_seconds ?? '?'}s</div>`;
  } catch (e) {
    el.textContent = '获取池状态失败: ' + e.message;
  }
}

async function reapPool(global) {
  if (!confirm('回收：清理池内超时/空闲沙箱 + 服务端孤儿沙箱？不影响正在使用的。')) return;
  const btn = document.getElementById(global ? 'btn-g-pool-reap' : 'btn-pool-reap');
  if (btn) { btn.disabled = true; btn.textContent = '回收中…'; }
  try {
    const resp = await fetch('/api/sandbox/pool/reap?include_orphans=true', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '回收失败');
    const pr = data.pool_reap || {};
    const oc = data.orphan_cleanup || {};
    showToast(`回收完成：池 kill ${pr.killed ?? 0}、孤儿 kill ${oc.killed ?? 0}`, 'success');
    if (global && typeof refreshGlobalSandboxes === 'function') {
      await refreshGlobalSandboxes();
    } else {
      await refreshPoolStatus();
      await refreshOrphanCount();
    }
  } catch (e) {
    showToast('回收失败: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '♻️ 回收(池+孤儿)'; }
  }
}

// ─── Retrieve experiment ─────────────────────────────────────
