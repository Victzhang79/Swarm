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

function notificationsUrl(since) {
  const params = [];
  if (selectedProjectId) params.push('project_id=' + encodeURIComponent(selectedProjectId));
  if (since) params.push('since=' + encodeURIComponent(since));
  return params.length ? '/api/notifications?' + params.join('&') : '/api/notifications';
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
  } catch { /* ignore */ }
}

function notificationEventLabel(eventType) {
  if (eventType === 'task_failed') return '失败';
  if (eventType === 'waiting_review') return '待审';
  return '完成';
}

function notificationEventPill(eventType) {
  if (eventType === 'task_failed') return 'pill-red';
  if (eventType === 'waiting_review') return 'pill-amber';
  return 'pill-green';
}

function renderNotifications(items) {
  const el = $('notifications-list');
  if (!el) return;
  if (!items.length) {
    el.innerHTML = '<p class="hint" style="padding:8px 0;margin:0">暂无通知</p>';
    return;
  }
  el.innerHTML = items.map(n => `
    <div class="notification-item" role="button" tabindex="0"
         onclick="openNotificationTask('${escapeHtml(n.task_id)}','${escapeHtml(n.project_id || '')}')">
      <span class="pill ${notificationEventPill(n.event_type)}">${escapeHtml(notificationEventLabel(n.event_type))}</span>
      <div class="notification-body">
        <p class="notification-msg">${escapeHtml(n.message || n.description || '')}</p>
        <span class="notification-meta">${escapeHtml(formatTime(n.updated_at))}</span>
      </div>
    </div>`).join('');
}

async function loadNotifications() {
  try {
    const resp = await fetch(notificationsUrl());
    if (!resp.ok) return;
    const data = await resp.json();
    renderNotifications(data.notifications || []);
  } catch { /* ignore */ }
}

async function pollSystemNotifications() {
  if (!lastSystemPollAt) {
    await loadNotifications();
    lastSystemPollAt = new Date().toISOString();
    return;
  }
  try {
    const resp = await fetch(notificationsUrl(lastSystemPollAt));
    if (resp.ok) {
      const data = await resp.json();
      const newItems = data.notifications || [];
      if (newItems.length) {
        showNotificationBanner(newItems.length);
        await maybeShowBrowserNotifications(newItems);
      }
    }
  } catch { /* ignore */ }
  lastSystemPollAt = new Date().toISOString();
  await loadNotifications();
}

function loadSystemTab() {
  fetchStatus();
  loadSystemStats();
  loadNotifications();
}

function startSystemRefresh() {
  stopSystemRefresh();
  lastSystemPollAt = null;
  loadSystemTab();
  systemStatsInterval = setInterval(async () => {
    if (currentTab !== 'system') return;
    await loadSystemStats();
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
      <div><span class="label">模板</span><code>${escapeHtml(config.default_template || '-')}</code></div>
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

// ─── Retrieve experiment ─────────────────────────────────────
