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

// 设置已升级为上层「设置」tab（原抽屉退役）。保留 no-op 兼容任何残留引用。
function toggleSettings() {
  if (typeof switchTopTab === 'function') switchTopTab('settings');
}

// 设置 tab 入口：配置数据在启动时已 loadConfig/loadRoutingTable 预填，这里刷新一次保证最新。
function loadSettingsTab() {
  if (typeof loadConfig === 'function') loadConfig();
  if (typeof loadRoutingTable === 'function') loadRoutingTable();
  if (typeof loadKbEmbedRerank === 'function') loadKbEmbedRerank();
  if (typeof syncPoolToggleState === 'function') syncPoolToggleState();
  if (typeof loadNotifyChannels === 'function') loadNotifyChannels();
}

// ─── Add Project Modal ─────────────────────────────────────

// ─── 两层导航：上层系统级 / 下层项目级 ───────────────────────
// 系统级 tab（observability/system）不依赖 selectedProjectId；
// 项目工作台（workspace）下挂项目级 tab（tasks/worker/knowledge/memory/preprocess）。

let currentTopTab = 'workspace';

function switchTopTab(topTab) {
  currentTopTab = topTab;
  document.querySelectorAll('.nav-tab-top').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.toptab === topTab);
  });
  const subNav = $('nav-tabs-sub');

  if (topTab === 'workspace') {
    // 显示下层项目 nav，激活当前项目 tab（默认 tasks）
    if (subNav) subNav.classList.remove('hidden');
    stopSystemRefresh();
    switchTab(currentTab && currentTab !== 'observability' && currentTab !== 'system' ? currentTab : 'tasks');
    return;
  }

  // 系统级：隐藏下层 nav，直接激活对应系统 panel
  if (subNav) subNav.classList.add('hidden');
  document.querySelectorAll('.tab-panel').forEach(panel => {
    panel.classList.toggle('active', panel.id === 'tab-' + topTab);
  });
  // 下层 nav 高亮清掉（避免残留）
  document.querySelectorAll('.nav-tabs-sub .nav-tab').forEach(btn => btn.classList.remove('active'));

  if (topTab === 'observability') {
    stopSystemRefresh();
    loadObservability();
  } else if (topTab === 'sandboxes') {
    stopSystemRefresh();
    if (typeof refreshGlobalSandboxes === 'function') refreshGlobalSandboxes();
    if (typeof loadSandboxTemplates === 'function') loadSandboxTemplates();
  } else if (topTab === 'system') {
    startSystemRefresh();
  } else if (topTab === 'settings') {
    stopSystemRefresh();
    if (typeof loadSettingsTab === 'function') loadSettingsTab();
  }
}

function switchTab(tabId) {
  // 下层项目级 tab 切换（tasks/worker/knowledge/memory/preprocess）
  currentTab = tabId;
  document.querySelectorAll('.nav-tabs-sub .nav-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabId);
  });
  document.querySelectorAll('.tab-panel').forEach(panel => {
    panel.classList.toggle('active', panel.id === 'tab-' + tabId);
  });
  // 从系统级 tab 点回项目 tab 时（如 learn-notice 的"查看记忆"），确保上层切到工作台
  if (currentTopTab !== 'workspace') {
    currentTopTab = 'workspace';
    document.querySelectorAll('.nav-tab-top').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.toptab === 'workspace');
    });
    const subNav = $('nav-tabs-sub');
    if (subNav) subNav.classList.remove('hidden');
  }
  stopSystemRefresh();
  if (selectedProjectId) {
    reloadCurrentProjectTab(selectedProjectId);
  }
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
