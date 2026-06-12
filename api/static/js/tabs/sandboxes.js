/* Swarm Web UI — tabs/sandboxes：全服务端沙箱总览（系统级全局视角）
   结构/文案/按钮风格对齐项目级「本项目关联沙箱」(stats tab)。 */
'use strict';

let _gSelectedSandbox = null;

// 拉取服务端【全部】沙箱（不带 project_id = 全局视角）。
async function refreshGlobalSandboxes() {
  const list = $('global-sandbox-list');
  if (!list) return;
  list.innerHTML = '<p class="hint" style="padding:8px">加载中…</p>';
  try {
    const [statusResp, orphanResp] = await Promise.all([
      fetch('/api/sandbox/status'),                 // 无 project_id = 全部
      fetch('/api/sandbox/orphans').catch(() => null),
    ]);
    if (!statusResp.ok) throw new Error('HTTP ' + statusResp.status);
    const data = await statusResp.json();
    const arr = data.sandboxes || [];

    // 摘要条（对齐项目级 sandbox-config 风格）
    let orphanCount = '—';
    if (orphanResp && orphanResp.ok) {
      try { const od = await orphanResp.json(); orphanCount = od.orphan_count != null ? od.orphan_count : '—'; } catch { /* ignore */ }
    }
    let poolText = '';
    try {
      const p = await fetch('/api/sandbox/pool').then(r => r.ok ? r.json() : null);
      if (p) {
        const total = p.total != null ? p.total : (p.pool_total != null ? p.pool_total : '—');
        poolText = `　热池：${p.pool_enabled ? '开' : '关'}（池内 ${total}）`;
      }
    } catch { /* ignore */ }
    const summary = $('g-sandbox-summary');
    if (summary) {
      const total = data.active_count != null ? data.active_count : arr.length;
      summary.innerHTML = `服务端共 <b>${total}</b> 个　孤儿 <b style="color:var(--orange)">${orphanCount}</b> 个${poolText}`;
    }

    if (!arr.length) {
      list.innerHTML = '<p class="hint" style="padding:8px">服务端无活跃沙箱</p>';
      _gResetDetail();
      return;
    }
    const ids = arr.map(sb => sb.id || sb.sandbox_id).filter(Boolean);
    if (!_gSelectedSandbox || !ids.includes(_gSelectedSandbox)) _gSelectedSandbox = ids[0];

    list.innerHTML = arr.map(sb => {
      const id = sb.id || sb.sandbox_id || String(sb);
      const selected = id === _gSelectedSandbox;
      const isOrphan = !sb.project_id && !sb.task_id;
      const assoc = isOrphan
        ? '<span class="pill pill-orange">孤儿</span>'
        : (sb.project_id ? `<span class="pill pill-gray" title="项目 ${escapeHtml(String(sb.project_id))}">P:${escapeHtml(String(sb.project_id).substring(0, 8))}</span>` : '');
      const meta = [
        sb.status ? `<span class="pill ${sb.status === 'running' ? 'pill-green' : 'pill-gray'}">${escapeHtml(sb.status)}</span>` : '',
        assoc,
        sb.source && sb.source !== '-' ? `<span class="pill pill-gray">${escapeHtml(String(sb.source))}</span>` : '',
        sb.cpu_count != null ? `<span class="pill pill-gray">${sb.cpu_count} CPU</span>` : '',
        sb.memory_mb != null ? `<span class="pill pill-gray">${sb.memory_mb} MB</span>` : '',
      ].filter(Boolean).join(' ');
      return `
        <div class="sandbox-row${selected ? ' selected' : ''}" data-gsb-id="${escapeHtml(String(id))}">
          <div class="sandbox-row-main">
            <span class="sandbox-id">${escapeHtml(String(id))}</span>
            <div class="sandbox-meta">${meta}</div>
          </div>
          <div class="sandbox-row-actions">
            <button type="button" class="btn btn-danger btn-sm" data-gsb-action="destroy">销毁</button>
          </div>
        </div>`;
    }).join('');

    list.querySelectorAll('.sandbox-row').forEach(row => {
      row.addEventListener('click', () => gSelectSandbox(row.dataset.gsbId));
      const db = row.querySelector('[data-gsb-action="destroy"]');
      if (db) db.addEventListener('click', (e) => { e.stopPropagation(); gDestroySandbox(row.dataset.gsbId); });
    });
    gShowDetail(arr.find(sb => (sb.id || sb.sandbox_id) === _gSelectedSandbox));
  } catch (e) {
    list.innerHTML = `<p class="hint" style="padding:8px;color:var(--orange)">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}

function gSelectSandbox(id) {
  _gSelectedSandbox = id;
  document.querySelectorAll('#global-sandbox-list .sandbox-row').forEach(r =>
    r.classList.toggle('selected', r.dataset.gsbId === id));
  // 从列表行的 pills 拿不到完整 meta，重新拉一次 status 取该沙箱
  fetch('/api/sandbox/status').then(r => r.json()).then(d => {
    const sb = (d.sandboxes || []).find(s => (s.id || s.sandbox_id) === id);
    gShowDetail(sb);
  }).catch(() => gShowDetail(null));
}

// 显示详情面板（隐藏空占位），并行加载文件 + 日志。
function gShowDetail(sb) {
  const empty = $('g-sandbox-detail-empty');
  const panel = $('g-sandbox-detail-panel');
  if (!_gSelectedSandbox) { _gResetDetail(); return; }
  if (empty) empty.classList.add('hidden');
  if (panel) panel.classList.remove('hidden');
  const idEl = $('g-sandbox-detail-id');
  if (idEl) idEl.textContent = _gSelectedSandbox;
  const metaEl = $('g-sandbox-detail-meta');
  if (metaEl && sb) {
    const isOrphan = !sb.project_id && !sb.task_id;
    metaEl.innerHTML = [
      sb.status ? `<span class="pill ${sb.status === 'running' ? 'pill-green' : 'pill-gray'}">${escapeHtml(sb.status)}</span>` : '',
      isOrphan ? '<span class="pill pill-orange">孤儿</span>' : (sb.project_id ? `<span class="pill pill-gray">P:${escapeHtml(String(sb.project_id).substring(0, 8))}</span>` : ''),
      sb.source ? `<span class="pill pill-gray">${escapeHtml(String(sb.source))}</span>` : '',
    ].filter(Boolean).join(' ');
  }
  gLoadFiles();
  gLoadLogs();
}

function _gResetDetail() {
  const empty = $('g-sandbox-detail-empty');
  const panel = $('g-sandbox-detail-panel');
  if (empty) empty.classList.remove('hidden');
  if (panel) panel.classList.add('hidden');
}

async function gLoadLogs() {
  const panel = $('g-sandbox-logs');
  if (!panel || !_gSelectedSandbox) return;
  panel.innerHTML = '<div class="log-line info">加载日志…</div>';
  try {
    const resp = await fetch('/api/sandbox/' + encodeURIComponent(_gSelectedSandbox) + '/logs?limit=200');
    if (!resp.ok) throw new Error('HTTP ' + resp.status + (resp.status === 404 ? '（日志接口未就绪，重启 API）' : ''));
    const data = await resp.json();
    const logs = data.logs || [];
    if (!logs.length) { panel.innerHTML = '<div class="log-line info">暂无执行日志 — Worker 执行或 run_code 后会在此显示</div>'; return; }
    panel.innerHTML = logs.map(e => {
      const kind = e.kind || 'info';
      const ts = e.ts ? (typeof formatLogTime === 'function' ? formatLogTime(e.ts) : e.ts) : '';
      return `<div class="log-line ${escapeHtml(kind)}"><span class="log-ts">${escapeHtml(String(ts))}</span> ${escapeHtml(e.message || '')}</div>`;
    }).join('');
  } catch (e) {
    panel.innerHTML = `<div class="log-line error">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

async function gLoadFiles() {
  const panel = $('g-sandbox-files');
  if (!panel || !_gSelectedSandbox) return;
  panel.innerHTML = '<p class="hint">加载中…</p>';
  try {
    const resp = await fetch('/api/sandbox/' + encodeURIComponent(_gSelectedSandbox) + '/files?path=/workspace');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    const entries = data.entries || data.files || [];
    if (!entries.length) { panel.innerHTML = '<p class="hint">空目录或不可读</p>'; return; }
    panel.innerHTML = entries.map(en => {
      const name = en.name || en.path || String(en);
      const isDir = en.is_dir || en.type === 'dir';
      return `<div class="sandbox-file-row">${isDir ? '📁' : '📄'} ${escapeHtml(String(name))}</div>`;
    }).join('');
  } catch (e) {
    panel.innerHTML = `<p class="hint" style="color:var(--orange)">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}

async function gDestroySandbox(id) {
  if (!confirm(`销毁沙箱 ${id}？`)) return;
  try {
    const resp = await fetch('/api/sandbox/' + encodeURIComponent(id), { method: 'DELETE' });
    if (!resp.ok) { const d = await resp.json().catch(() => ({})); throw new Error(d.detail || 'HTTP ' + resp.status); }
    showToast('沙箱已销毁', 'success');
    if (_gSelectedSandbox === id) _gSelectedSandbox = null;
    await refreshGlobalSandboxes();
  } catch (e) {
    showToast('销毁失败: ' + e.message, 'error');
  }
}

// 销毁服务端【全部】沙箱（含正在使用的，危险操作，二次确认）。
async function destroyAllServerSandboxes() {
  if (!confirm('销毁服务端全部沙箱？包括正在使用的（会中断运行中的任务）！')) return;
  if (!confirm('再次确认：这是全服务端范围的销毁，不可恢复。继续？')) return;
  const btn = $('btn-g-destroy-all');
  if (btn) { btn.disabled = true; btn.textContent = '销毁中…'; }
  try {
    const resp = await fetch('/api/sandbox/cleanup?server=true', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'HTTP ' + resp.status);
    showToast(`已销毁 ${data.killed} 个沙箱${data.failed ? `（${data.failed} 失败）` : ''}`, 'success');
    _gSelectedSandbox = null;
    await refreshGlobalSandboxes();
  } catch (e) {
    showToast('销毁失败: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '全部销毁'; }
  }
}
