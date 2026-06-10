/* Swarm Web UI — core/ui module (split from app.js, shared global scope) */
'use strict';

function showToast(message, type = 'info') {
  const container = $('toast-container');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function togglePassword(inputId) {
  const input = $(inputId);
  if (!input) return;
  input.style.webkitTextSecurity = input.style.webkitTextSecurity === 'none' ? 'disc' : 'none';
}

function toggleSettings() {
  const drawer = $('settings-drawer');
  const overlay = $('settings-overlay');
  const open = drawer.classList.toggle('open');
  overlay.classList.toggle('open', open);
}

// ─── Add Project Modal ─────────────────────────────────────

function switchTab(tabId) {
  currentTab = tabId;
  document.querySelectorAll('.nav-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabId);
  });
  document.querySelectorAll('.tab-panel').forEach(panel => {
    panel.classList.toggle('active', panel.id === 'tab-' + tabId);
  });
  if (!selectedProjectId) {
    if (tabId === 'system') {
      const list = $('sandbox-list');
      if (list) list.innerHTML = '<p style="font-size:12px;color:var(--text-muted);padding:8px">请先在左侧选择项目</p>';
      startSystemRefresh();
    } else {
      stopSystemRefresh();
    }
    return;
  }
  reloadCurrentProjectTab(selectedProjectId);
  if (tabId === 'system') startSystemRefresh();
  else stopSystemRefresh();
}

function switchDetailTab(tabId) {
  currentDetailTab = tabId;
  document.querySelectorAll('.detail-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.detail === tabId);
  });
  ['overview', 'diff', 'plan', 'logs'].forEach(name => {
    const el = $('detail-' + name);
    if (el) el.classList.toggle('hidden', name !== tabId);
  });
}

function tryShowLearnNotice(data) {
  const el = $('learn-notice');
  const persist = data?.persist_meta || data?.persist;
  if (!persist) { el.classList.add('hidden'); return; }
  el.classList.remove('hidden');
  const parts = [];
  if (persist.success_id) parts.push('成功模式');
  if (persist.mistake_id) parts.push('错题');
  if (persist.summary_id) parts.push('任务摘要');
  el.innerHTML = `<div class="learn-notice">
    已写入记忆：${parts.join('、') || '学习摘要'}
    <a onclick="switchTab('memory');loadAllMemories(selectedProjectId)">查看记忆 →</a>
  </div>`;
  if (selectedProjectId) {
    loadAllMemories(selectedProjectId);
  }
  showToast('记忆已更新', 'success');
}

// ─── Pipeline & Logs ───────────────────────────────────────

function showNotificationBanner(count) {
  const banner = $('stats-banner');
  if (!banner) return;
  banner.classList.remove('hidden');
  banner.innerHTML = `
    <span>${count} 条新通知</span>
    <button class="btn btn-ghost btn-sm" onclick="dismissNotificationBanner()">知道了</button>`;
}

function dismissNotificationBanner() {
  const banner = $('stats-banner');
  if (banner) banner.classList.add('hidden');
}

async function maybeShowBrowserNotifications(items) {
  if (!items.length || !document.hidden) return;
  if (!('Notification' in window)) return;
  let perm = Notification.permission;
  if (perm === 'default') {
    try {
      perm = await Notification.requestPermission();
    } catch {
      return;
    }
  }
  if (perm !== 'granted') return;
  for (const n of items.slice(0, 3)) {
    try {
      new Notification('Swarm', {
        body: n.message || n.description || '任务状态更新',
        tag: n.task_id || undefined,
      });
    } catch { /* ignore */ }
  }
}
