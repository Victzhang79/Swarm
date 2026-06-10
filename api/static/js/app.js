/* Swarm v0.3 — Web UI Application */

'use strict';

// ─── Auth ────────────────────────────────────────────────
const AUTH_TOKEN_KEY = 'swarm_auth_token';
let currentUser = null;

function getAuthToken() {
  return localStorage.getItem(AUTH_TOKEN_KEY) || '';
}

function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  const t = getAuthToken();
  if (t) h.Authorization = 'Bearer ' + t;
  return h;
}

function installAuthFetch() {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function swarmFetch(url, opts) {
    opts = opts || {};
    const path = typeof url === 'string' ? url : (url && url.url) || '';
    if (path.startsWith('/api/') && !path.startsWith('/api/auth/login') && !path.startsWith('/api/health')) {
      opts.headers = authHeaders(opts.headers);
    }
    return nativeFetch(url, opts).then(function (resp) {
      if (resp.status === 401 && path.startsWith('/api/') && !path.startsWith('/api/auth/login')) {
        showLoginModal();
      }
      return resp;
    });
  };
}

function updateAuthUI() {
  const badge = $('user-badge');
  const btnLogin = $('btn-login');
  const btnLogout = $('btn-logout');
  const hint = $('profile-user-hint');
  if (currentUser) {
    badge.textContent = currentUser.display_name || currentUser.username;
    badge.className = 'pill pill-green';
    btnLogin.classList.add('hidden');
    btnLogout.classList.remove('hidden');
    if (hint) hint.textContent = '用户 ' + currentUser.username + ' · 项目级画像（结构化表单或 Advanced JSON）';
  } else {
    badge.textContent = '未登录';
    badge.className = 'pill pill-gray';
    btnLogin.classList.remove('hidden');
    btnLogout.classList.add('hidden');
  }
}

function showLoginModal() {
  $('login-overlay').classList.add('open');
  $('login-modal').classList.add('open');
  $('login-error').style.display = 'none';
  const userInput = $('login-username');
  if (userInput) setTimeout(function () { userInput.focus(); }, 100);
}

function hideLoginModal() {
  $('login-overlay').classList.remove('open');
  $('login-modal').classList.remove('open');
}

async function submitLogin() {
  const userEl = $('login-username');
  const passEl = $('login-password');
  const errEl = $('login-error');
  const username = ((userEl && userEl.value) || 'admin').trim();
  const password = (passEl && passEl.value) || '';
  errEl.style.display = 'none';
  if (!username) {
    errEl.textContent = '请输入用户名';
    errEl.style.display = 'block';
    if (userEl) userEl.focus();
    return;
  }
  if (!password) {
    errEl.textContent = '请输入密码';
    errEl.style.display = 'block';
    if (passEl) passEl.focus();
    return;
  }
  try {
    const resp = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(function () { return {}; });
      errEl.textContent = data.detail || ('登录失败 (HTTP ' + resp.status + ')');
      errEl.style.display = 'block';
      return;
    }
    const data = await resp.json();
    localStorage.setItem(AUTH_TOKEN_KEY, data.token);
    currentUser = data.user;
    hideLoginModal();
    updateAuthUI();
    showToast('欢迎，' + (currentUser.display_name || currentUser.username), 'success');
    await loadProjects();
    if (selectedProjectId && currentTab === 'memory') {
      loadAllMemories(selectedProjectId);
    }
  } catch (e) {
    errEl.textContent = '登录请求失败: ' + (e.message || e);
    errEl.style.display = 'block';
  }
}

function logoutUser() {
  localStorage.removeItem(AUTH_TOKEN_KEY);
  currentUser = null;
  updateAuthUI();
  showLoginModal();
}

async function refreshCurrentUser() {
  if (!getAuthToken()) {
    currentUser = null;
    updateAuthUI();
    return false;
  }
  try {
    const resp = await fetch('/api/auth/me');
    if (!resp.ok) {
      localStorage.removeItem(AUTH_TOKEN_KEY);
      currentUser = null;
      updateAuthUI();
      return false;
    }
    currentUser = await resp.json();
    updateAuthUI();
    return true;
  } catch (_) {
    return false;
  }
}

// ─── Constants ───────────────────────────────────────────
const COMPONENT_DEFS = [
  { name: 'Brain 状态机' },
  { name: 'Worker 执行器' },
  { name: '知识库' },
  { name: '记忆系统' },
  { name: '远程沙箱' },
  { name: '模型路由' },
  { name: 'PostgreSQL' },
  { name: 'Qdrant' },
];

const PIPELINE_NODES = ['analyze', 'plan', 'dispatch', 'merge', 'verify', 'deliver', 'learn'];

const NODE_MAP = {
  analyze: 'analyze',
  plan: 'plan',
  validate_plan: 'plan',
  confirm: 'plan',
  confirm_plan: 'plan',
  dispatch: 'dispatch',
  monitor: 'dispatch',
  revision: 'dispatch',
  handle_failure: 'dispatch',
  merge: 'merge',
  verify_l2: 'verify',
  deliver: 'deliver',
  learn_success: 'learn',
  learn_failure: 'learn',
};

const TASK_STATUS_PILLS = {
  SUBMITTED: 'pill-gray',
  ANALYZING: 'pill-blue',
  PLANNING: 'pill-blue',
  VALIDATING_PLAN: 'pill-blue',
  CONFIRMING: 'pill-amber',
  DISPATCHING: 'pill-blue',
  MONITORING: 'pill-blue',
  HANDLING_FAILURE: 'pill-red',
  MERGING: 'pill-purple',
  VERIFYING_L2: 'pill-purple',
  DELIVERING: 'pill-green',
  IN_REVISION: 'pill-orange',
  LEARNING_SUCCESS: 'pill-teal',
  LEARNING_FAILURE: 'pill-teal',
  FAILED: 'pill-red',
  CANCELLED: 'pill-gray',
  DONE: 'pill-green',
};

const ACTIVE_STATUSES = new Set([
  'ANALYZING', 'PLANNING', 'VALIDATING_PLAN', 'CONFIRMING', 'DISPATCHING',
  'MONITORING', 'HANDLING_FAILURE', 'MERGING', 'VERIFYING_L2', 'DELIVERING',
  'IN_REVISION', 'LEARNING_SUCCESS', 'LEARNING_FAILURE',
]);

// ─── State ───────────────────────────────────────────────
let statusInterval = null;
let taskEventSource = null;
let workerEventSource = null;
let workerRunId = null;
let preprocessSSE = null;
let eventSource = null;
let originalConfig = {};
let modelLists = { siliconflow: [], local: [] };

let projects = [];
let selectedProjectId = null;
let tasks = [];
let selectedTaskId = null;
let selectedTaskDetail = null;
let currentTab = 'tasks';
let currentDetailTab = 'overview';
let reviseTargetTaskId = null;
let logEntries = [];
let selectedSandboxId = null;
let sandboxCurrentPath = '/workspace';
let sandboxSelectedFile = null;
let workerLastDiff = '';
let systemStatsInterval = null;
let lastSystemPollAt = null;
let diffViewMode = 'unified';
let lastDiffText = '';

const PROJECT_STORAGE_KEY = 'swarm_selected_project_id';

function parseScopeInput(text) {
  if (!text || !String(text).trim()) return null;
  const paths = String(text).split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
  return paths.length ? paths : null;
}

// ─── Helpers ─────────────────────────────────────────────
function $(id) { return document.getElementById(id); }

function escapeHtml(text) {
  if (text == null) return '';
  const d = document.createElement('div');
  d.textContent = String(text);
  return d.innerHTML;
}

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

function formatTime(d) {
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function isTaskActive(status) {
  return ACTIVE_STATUSES.has(status);
}

function canRetryTask(t) {
  if (!t || !t.status) return false;
  const status = t.status;
  if (status === 'FAILED' || status === 'CANCELLED' || status === 'DONE') return true;
  if (isTaskActive(status) && status !== 'DELIVERING' && status !== 'CONFIRMING') return true;
  return false;
}

function renderTaskActions(t, compact) {
  const active = isTaskActive(t.status);
  const retryable = canRetryTask(t);
  const btnStyle = compact
    ? 'padding:2px 6px;font-size:11px'
    : '';
  const cls = compact ? 'btn btn-ghost btn-sm' : 'btn btn-secondary btn-sm';
  let html = '';
  if (active) {
    html += `<button class="${cls}" style="${btnStyle}" onclick="event.stopPropagation();cancelTask('${t.id}')" title="取消">取消</button>`;
  }
  if (retryable) {
    html += `<button class="${cls}" style="${btnStyle}" onclick="event.stopPropagation();retryTask('${t.id}')" title="重跑">重跑</button>`;
  }
  html += `<button class="${cls}" style="${btnStyle}" onclick="event.stopPropagation();deleteTask('${t.id}', ${active ? 'true' : 'false'})" title="删除">删除</button>`;
  return html;
}

function renderTaskStatusPill(status) {
  const cls = TASK_STATUS_PILLS[status] || 'pill-gray';
  return `<span class="pill ${cls}">${escapeHtml(status || 'UNKNOWN')}</span>`;
}

function projectStatusTag(status) {
  const map = {
    READY: { cls: 'pill-green', label: 'READY' },
    PREPROCESSING: { cls: 'pill-blue', label: 'PREPROCESSING' },
    ERROR: { cls: 'pill-red', label: 'ERROR' },
  };
  const s = map[status] || { cls: 'pill-gray', label: status || 'UNKNOWN' };
  return `<span class="pill ${s.cls}">${s.label}</span>`;
}

function graphStatusTag(status) {
  const map = {
    NONE: { cls: 'pill-gray', label: 'GRAPH:NONE' },
    INDEXING: { cls: 'pill-purple', label: 'INDEXING' },
    INDEXED: { cls: 'pill-green', label: 'INDEXED' },
    ERROR: { cls: 'pill-red', label: 'GRAPH:ERROR' },
  };
  const s = map[status] || map.NONE;
  return `<span class="pill ${s.cls}">${s.label}</span>`;
}

function graphStatusTagForOverview(graphStatus, indexStats) {
  if (indexStats?.skipped && (graphStatus === 'NONE' || !graphStatus)) {
    return '<span class="pill pill-amber" title="CodeGraph 未运行，预处理仍已完成">GRAPH:已跳过</span>';
  }
  return graphStatusTag(graphStatus || 'NONE');
}

// ─── Settings Drawer ─────────────────────────────────────
function toggleSettings() {
  const drawer = $('settings-drawer');
  const overlay = $('settings-overlay');
  const open = drawer.classList.toggle('open');
  overlay.classList.toggle('open', open);
}

// ─── Add Project Modal ─────────────────────────────────────
function showAddProjectModal() {
  $('add-project-overlay').classList.add('open');
  $('add-project-modal').classList.add('open');
  $('add-project-path').value = '';
  $('add-project-name').value = '';
  $('add-project-path').focus();
}

function hideAddProjectModal() {
  $('add-project-overlay').classList.remove('open');
  $('add-project-modal').classList.remove('open');
}

function submitAddProjectFromModal() {
  const path = $('add-project-path').value.trim();
  if (!path) { showToast('请输入项目路径', 'warning'); return; }
  const name = $('add-project-name').value.trim() || path.split('/').pop() || 'New Project';
  hideAddProjectModal();
  submitAddProject(name, path);
}

// ─── Revise Modal ──────────────────────────────────────────
function hideReviseModal() {
  $('revise-overlay').classList.remove('open');
  $('revise-modal').classList.remove('open');
  reviseTargetTaskId = null;
}

function submitReviseFromModal() {
  const feedback = $('revise-feedback').value.trim();
  if (!feedback || !reviseTargetTaskId) return;
  hideReviseModal();
  submitReviseTask(reviseTargetTaskId, feedback);
}

// ─── Tab Switching ─────────────────────────────────────────
function reloadCurrentProjectTab(projectId) {
  if (!projectId) return;
  if (currentTab === 'knowledge') {
    loadKnowledgeOverview(projectId);
    loadBehaviorHotspots(projectId);
    loadNorms(projectId);
  } else if (currentTab === 'worker') {
    resetWorkerPanelForProject();
  } else if (currentTab === 'preprocess') {
    loadPreprocessStatus(projectId);
  } else if (currentTab === 'memory') {
    loadAllMemories(projectId);
  } else if (currentTab === 'tasks') {
    loadTasks(projectId);
    refreshTaskReadinessHint(projectId);
  } else if (currentTab === 'system') {
    refreshSandboxes(projectId);
  }
}

function clearProjectScopedUI() {
  tasks = [];
  renderTaskList();
  selectedTaskId = null;
  selectedTaskDetail = null;
  showTaskDetailEmpty();
  const sandboxList = $('sandbox-list');
  if (sandboxList) {
    sandboxList.innerHTML = '<p style="font-size:12px;color:var(--text-muted);padding:8px">请先在左侧选择项目</p>';
  }
  resetSandboxDetail();
}

function resetSandboxDetail() {
  selectedSandboxId = null;
  sandboxCurrentPath = '/workspace';
  sandboxSelectedFile = null;
  const empty = $('sandbox-detail-empty');
  const panel = $('sandbox-detail-panel');
  if (empty) empty.classList.remove('hidden');
  if (panel) panel.classList.add('hidden');
}

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

function showProjectView(show) {
  $('no-project-view').classList.toggle('hidden', show);
  const pv = $('project-view');
  if (show) {
    pv.classList.remove('hidden');
    pv.style.display = 'flex';
  } else {
    pv.classList.add('hidden');
    pv.style.display = 'none';
  }
}

// ─── Projects ──────────────────────────────────────────────
async function loadProjects() {
  try {
    const resp = await fetch('/api/projects');
    if (!resp.ok) throw new Error('fetch failed');
    const data = await resp.json();
    projects = Array.isArray(data) ? data : (data.projects || []);
    renderProjectList();
    restoreSelectedProject();
  } catch {
    projects = [];
    renderProjectList();
  }
}

function restoreSelectedProject() {
  if (!projects.length) return;
  const savedId = localStorage.getItem(PROJECT_STORAGE_KEY);
  const target = savedId && projects.some(p => p.id === savedId)
    ? savedId
    : (projects.length === 1 ? projects[0].id : null);
  if (target && target !== selectedProjectId) {
    selectProject(target);
  }
}

function renderProjectList() {
  const list = $('project-list');
  if (projects.length === 0) {
    list.innerHTML = '<div class="empty-state" style="padding:24px"><p style="font-size:12px">暂无项目</p></div>';
    return;
  }
  list.innerHTML = projects.map(p => {
    const active = p.id === selectedProjectId;
    const spinning = p.status === 'PREPROCESSING';
    return `
      <div class="project-item ${active ? 'active' : ''}" onclick="selectProject('${p.id}')">
        ${spinning ? '<span class="spin" style="font-size:12px">⟳</span>' : '<span style="opacity:0.5">📁</span>'}
        <span class="name">${escapeHtml(p.name)}</span>
        ${projectStatusTag(p.status)}
      </div>`;
  }).join('');
}

async function submitAddProject(name, path) {
  try {
    const resp = await fetch('/api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, path }),
    });
    if (!resp.ok) {
      const err = await resp.text();
      throw new Error(err);
    }
    showToast('项目已添加，预处理启动中', 'success');
    await loadProjects();
    const data = await resp.json();
    const pid = data.project?.id || data.id;
    if (pid) selectProject(pid);
  } catch (e) {
    showToast('添加失败: ' + e.message, 'error');
  }
}

function selectProject(id) {
  if (id === selectedProjectId) return;

  closeTaskSSE();
  closeWorkerSSE();
  disconnectPreprocessSSE();

  selectedProjectId = id;
  try { localStorage.setItem(PROJECT_STORAGE_KEY, id); } catch { /* ignore */ }
  selectedTaskId = null;
  selectedTaskDetail = null;
  renderProjectList();
  showProjectView(true);
  clearProjectScopedUI();

  const project = projects.find(p => p.id === id);
  if (!project) return;

  $('project-name').textContent = project.name;
  $('project-path').textContent = project.path || '';

  let statsHtml = projectStatusTag(project.status) + ' ' + graphStatusTag(project.graph_status || 'NONE');
  if (project.file_count) statsHtml += `<span class="pill pill-gray">${project.file_count} 文件</span>`;
  if (project.symbol_count) statsHtml += `<span class="pill pill-gray">${project.symbol_count} 符号</span>`;
  $('project-stats').innerHTML = statsHtml;

  if (currentTab === 'tasks') {
    showTaskDetailEmpty();
  }

  reloadCurrentProjectTab(id);

  if (project.status === 'PREPROCESSING') {
    connectPreprocessSSE(id);
  }
}

async function deleteProject() {
  if (!selectedProjectId) return;
  if (!confirm('确定删除此项目？')) return;
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId), { method: 'DELETE' });
    if (!resp.ok) throw new Error('删除失败');
    showToast('项目已删除', 'success');
    selectedProjectId = null;
    selectedTaskId = null;
    closeTaskSSE();
    disconnectPreprocessSSE();
    showProjectView(false);
    await loadProjects();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function preprocessProject() {
  if (!selectedProjectId) return;
  const errEl = $('preprocess-error');
  if (errEl) {
    errEl.classList.add('hidden');
    errEl.textContent = '';
  }
  updatePreprocessProgress({ phase: 'idle', phase_progress: 0, message: '正在启动预处理…' });
  switchTab('preprocess');

  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/preprocess', { method: 'POST' });
    if (!resp.ok) {
      let detail = await resp.text();
      try { detail = JSON.parse(detail).detail || detail; } catch { /* raw text */ }
      throw new Error(detail || '预处理启动失败');
    }
    showToast('预处理已启动', 'success');
    connectPreprocessSSE(selectedProjectId);
    await loadProjects();
    if (selectedProjectId) selectProject(selectedProjectId);
  } catch (e) {
    showToast('预处理失败: ' + e.message, 'error');
    if (errEl) {
      errEl.textContent = e.message;
      errEl.classList.remove('hidden');
    }
  }
}

// ─── Tasks ─────────────────────────────────────────────────
async function loadTasks(projectId) {
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/tasks');
    if (!resp.ok) throw new Error('fetch failed');
    const data = await resp.json();
    tasks = Array.isArray(data) ? data : (data.tasks || []);
    renderTaskList();
  } catch {
    tasks = [];
    renderTaskList();
  }
}

function renderTaskList() {
  const list = $('task-list');
  if (!selectedProjectId) {
    list.innerHTML = '<div style="padding:16px;text-align:center;font-size:12px;color:var(--text-muted)">请先在左侧选择项目</div>';
    return;
  }
  if (tasks.length === 0) {
    list.innerHTML = '<div style="padding:16px;text-align:center;font-size:12px;color:var(--text-muted)">暂无任务</div>';
    return;
  }
  list.innerHTML = tasks.map(t => {
    const shortId = t.id ? String(t.id).substring(0, 8) : '?';
    const selected = t.id === selectedTaskId;
    const active = isTaskActive(t.status);
    return `
      <div class="task-card ${selected ? 'selected' : ''}" onclick="selectTask('${t.id}')">
        <div class="task-card-top">
          <span class="task-card-id">#${shortId}</span>
          <div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap">
            ${renderTaskStatusPill(t.status)}
            ${renderTaskActions(t, true)}
          </div>
        </div>
        <div class="task-card-desc">${escapeHtml(t.description || '')}</div>
        ${t.complexity ? `<div class="task-card-meta"><span class="pill pill-purple">${escapeHtml(t.complexity)}</span></div>` : ''}
      </div>`;
  }).join('');
}

function normalizePlan(plan) {
  if (!plan) return null;
  if (typeof plan === 'string') {
    try { return JSON.parse(plan); } catch { return null; }
  }
  return plan;
}

function showTaskDetailEmpty() {
  $('task-detail-empty').classList.remove('hidden');
  $('task-detail-empty').style.display = 'flex';
  const content = $('task-detail-content');
  content.classList.add('hidden');
  content.style.display = 'none';
}

function showTaskDetailPanel() {
  $('task-detail-empty').classList.add('hidden');
  $('task-detail-empty').style.display = 'none';
  const content = $('task-detail-content');
  content.classList.remove('hidden');
  content.style.display = 'flex';
}

async function selectTask(taskId) {
  selectedTaskId = taskId;
  renderTaskList();
  showTaskDetailPanel();

  try {
    const resp = await fetch('/api/tasks/' + encodeURIComponent(taskId));
    if (!resp.ok) {
      const errText = await resp.text().catch(() => '');
      throw new Error(resp.status === 404 ? '任务不存在' : (errText || '加载失败'));
    }
    const data = await resp.json();
    selectedTaskDetail = data.task || data;
    selectedTaskDetail.plan = normalizePlan(selectedTaskDetail.plan);
    try {
      renderTaskDetail(selectedTaskDetail);
    } catch (renderErr) {
      console.error('renderTaskDetail failed:', renderErr);
      showToast('渲染任务详情失败: ' + renderErr.message, 'error');
      return;
    }

    if (isTaskActive(selectedTaskDetail.status) && !taskEventSource) {
      startTaskSSE(taskId);
    }
  } catch (e) {
    showToast('加载任务详情失败: ' + e.message, 'error');
  }
}

function renderTaskDetail(task) {
  if (!task) return;
  $('detail-description').textContent = task.description || '';
  $('detail-id').textContent = '#' + String(task.id || '').substring(0, 8);
  $('detail-status').innerHTML = renderTaskStatusPill(task.status);

  const complexityEl = $('detail-complexity');
  if (task.complexity) {
    complexityEl.textContent = task.complexity;
    complexityEl.classList.remove('hidden');
  } else {
    complexityEl.classList.add('hidden');
  }

  updateReviewBar(task);
  renderMergeConflictBanner(task);
  setApplyDiffButtonsDisabled(taskHasMergeConflicts(task));
  renderDiff(task.merged_diff || '');
  renderPlan(task.plan);
  renderOverviewSubtasks(task);

  if (task.learn_summary) {
    tryShowLearnNotice(typeof task.learn_summary === 'string' ? JSON.parse(task.learn_summary) : task.learn_summary);
  }

  const actionsEl = $('detail-actions');
  if (actionsEl) {
    actionsEl.innerHTML = renderTaskActions(task, false);
  }
}

function taskHasMergeConflicts(task) {
  const c = task?.merge_conflicts;
  return Array.isArray(c) && c.length > 0;
}

function renderMergeConflictBanner(task) {
  let el = $('merge-conflict-banner');
  if (!el) {
    const host = $('task-detail-content');
    if (!host) return;
    el = document.createElement('div');
    el.id = 'merge-conflict-banner';
    el.className = 'merge-conflict-banner hidden';
    host.insertBefore(el, host.firstChild);
  }
  if (!taskHasMergeConflicts(task)) {
    el.classList.add('hidden');
    el.innerHTML = '';
    return;
  }
  el.classList.remove('hidden');
  const items = task.merge_conflicts.map(c =>
    `<li><code>${escapeHtml(c.file_path || '?')}</code> — ${escapeHtml(c.message || '冲突')}</li>`
  ).join('');
  el.innerHTML = `<strong>⚠ Merge 冲突</strong> — apply 已阻断，请修订子任务后重跑<ul>${items}</ul>`;
}

function setApplyDiffButtonsDisabled(disabled) {
  document.querySelectorAll('[data-action="apply-diff"], [data-action="check-diff"]').forEach(btn => {
    btn.disabled = disabled;
    btn.title = disabled ? '存在 merge 冲突，无法 apply' : '';
  });
}

function updateReviewBar(task) {
  const bar = $('review-bar');
  const needsReview = task.status === 'DELIVERING' || task.status === 'CONFIRMING';
  bar.classList.toggle('hidden', !needsReview);
  if (needsReview) {
    $('review-message').textContent = task.status === 'CONFIRMING'
      ? '架构级任务 — 请确认执行计划'
      : '执行完成 — 请审核 Diff 并决定通过/修订/拒绝';
  }
}

function renderDiff(diff) {
  lastDiffText = diff || '';
  const container = $('diff-content');
  if (!diff || !diff.trim()) {
    container.innerHTML = '<div class="diff-empty">暂无 Diff — 任务执行后将在此显示合并后的代码变更</div>';
    return;
  }
  if (diffViewMode === 'split') {
    container.innerHTML = renderSplitDiff(diff);
    container.classList.add('diff-view-split');
  } else {
    container.innerHTML = renderUnifiedDiff(diff);
    container.classList.remove('diff-view-split');
  }
}

function setDiffViewMode(mode) {
  diffViewMode = mode === 'split' ? 'split' : 'unified';
  document.querySelectorAll('[data-diff-mode]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.diffMode === diffViewMode);
  });
  renderDiff(lastDiffText);
}

function renderUnifiedDiff(diff) {
  const lines = diff.split('\n');
  return lines.map(line => {
    let cls = '';
    if (line.startsWith('+') && !line.startsWith('+++')) cls = 'diff-line-add';
    else if (line.startsWith('-') && !line.startsWith('---')) cls = 'diff-line-del';
    else if (line.startsWith('@@')) cls = 'diff-line-hunk';
    return `<div class="${cls}">${escapeHtml(line)}</div>`;
  }).join('');
}

function extractDiffFilePath(line) {
  const m = line.match(/^diff --git a\/(.+?) b\/(.+)$/);
  if (m) return m[2] || m[1];
  if (line.startsWith('+++ ')) return line.slice(4).replace(/^b\//, '');
  return line;
}

function parseUnifiedDiffSections(diff) {
  const sections = [];
  let current = null;
  let currentHunk = null;

  for (const line of diff.split('\n')) {
    if (line.startsWith('diff --git')) {
      if (current) sections.push(current);
      current = { header: line, filePath: extractDiffFilePath(line), hunks: [], fileHeaders: [] };
      currentHunk = null;
    } else if (line.startsWith('@@')) {
      currentHunk = { header: line, lines: [] };
      if (current) current.hunks.push(currentHunk);
    } else if (currentHunk) {
      currentHunk.lines.push(line);
    } else if (current && (line.startsWith('---') || line.startsWith('+++'))) {
      current.fileHeaders.push(line);
    }
  }
  if (current) sections.push(current);
  return sections;
}

function renderSplitDiff(diff) {
  const sections = parseUnifiedDiffSections(diff);
  if (!sections.length) return renderUnifiedDiff(diff);

  return sections.map(sec => {
    const label = escapeHtml(sec.filePath || sec.header || 'file');
    const headers = (sec.fileHeaders || []).map(h =>
      `<div class="diff-file-meta">${escapeHtml(h)}</div>`
    ).join('');
    const hunks = sec.hunks.map(hunk => {
      const leftLines = [];
      const rightLines = [];
      for (const line of hunk.lines) {
        if (line.startsWith('-') && !line.startsWith('---')) {
          leftLines.push({ cls: 'diff-line-del', text: line.slice(1) });
          rightLines.push({ cls: 'diff-line-empty', text: '' });
        } else if (line.startsWith('+') && !line.startsWith('+++')) {
          leftLines.push({ cls: 'diff-line-empty', text: '' });
          rightLines.push({ cls: 'diff-line-add', text: line.slice(1) });
        } else {
          const ctx = line.startsWith(' ') ? line.slice(1) : line;
          leftLines.push({ cls: 'diff-line-ctx', text: ctx });
          rightLines.push({ cls: 'diff-line-ctx', text: ctx });
        }
      }
      const renderCol = lines => lines.map(l =>
        `<div class="${l.cls}">${l.text ? escapeHtml(l.text) : '&nbsp;'}</div>`
      ).join('');
      return `
        <div class="diff-hunk-header">${escapeHtml(hunk.header)}</div>
        <div class="diff-split-row">
          <div class="diff-split-col diff-split-old" aria-label="删除">${renderCol(leftLines)}</div>
          <div class="diff-split-col diff-split-new" aria-label="新增">${renderCol(rightLines)}</div>
        </div>`;
    }).join('');
    return `<div class="diff-file-section"><div class="diff-file-header">${label}</div>${headers}${hunks}</div>`;
  }).join('');
}

function renderPlan(plan) {
  const container = $('plan-content');
  plan = normalizePlan(plan);
  if (!plan || !plan.subtasks || plan.subtasks.length === 0) {
    container.innerHTML = '<div style="color:var(--text-muted);font-size:13px;padding:12px">计划尚未生成</div>';
    return;
  }
  container.innerHTML = plan.subtasks.map(st => `
    <div class="subtask-row">
      <span class="subtask-dot ${st.status || 'pending'}"></span>
      <span class="subtask-desc">${escapeHtml(st.description || st.id)}</span>
      <span class="subtask-tag">${escapeHtml(st.difficulty || 'medium')}</span>
      ${st.model ? `<span class="subtask-tag" style="color:var(--accent)">${escapeHtml(st.model)}</span>` : ''}
    </div>`).join('');
}

function renderOverviewSubtasks(task) {
  const container = $('overview-subtasks');
  const plan = normalizePlan(task.plan);
  if (!plan || !plan.subtasks || plan.subtasks.length === 0) {
    container.innerHTML = '';
    return;
  }
  const completed = task.completed_subtasks || 0;
  const total = task.subtask_count || plan.subtasks.length;
  container.innerHTML = `
    <div style="margin-bottom:8px;font-size:12px;color:var(--text-muted)">子任务进度 ${completed}/${total}</div>
    ${plan.subtasks.map(st => `
      <div class="subtask-row">
        <span class="subtask-dot"></span>
        <span class="subtask-desc">${escapeHtml(st.description || st.id)}</span>
        <span class="subtask-tag">${escapeHtml(st.difficulty || '')}</span>
      </div>`).join('')}`;
}

function showKnowledgeBanner(stats, complexity) {
  const banner = $('knowledge-banner');
  if (!stats) { banner.classList.add('hidden'); return; }
  banner.classList.remove('hidden');
  banner.innerHTML = `
    <span style="color:var(--blue);font-weight:500">知识检索</span>
    ${complexity ? `<span class="knowledge-stat">复杂度 <strong>${escapeHtml(String(complexity))}</strong></span>` : ''}
    <span class="knowledge-stat">Harness <strong>${stats.norms_count || 0}</strong></span>
    <span class="knowledge-stat">符号 <strong>${stats.struct_count || 0}</strong></span>
    <span class="knowledge-stat">语义 <strong>${stats.semantic_count || 0}</strong></span>
    <span class="knowledge-stat">错题 <strong>${stats.mistakes_count || 0}</strong></span>
    <span class="knowledge-stat">成功模式 <strong>${stats.successes_count || 0}</strong></span>`;
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
function resetPipeline() {
  PIPELINE_NODES.forEach(n => {
    const step = document.querySelector(`.pipeline-step[data-node="${n}"]`);
    if (step) step.className = 'pipeline-step pending';
  });
  $('knowledge-banner').classList.add('hidden');
  $('learn-notice').classList.add('hidden');
}

function updateNodeStatus(nodeName, status) {
  const uiNode = NODE_MAP[nodeName];
  if (!uiNode) return;

  // 当新节点开始运行时，将之前的节点标记为完成
  if (status === 'running') {
    const idx = PIPELINE_NODES.indexOf(uiNode);
    PIPELINE_NODES.forEach((n, i) => {
      const step = document.querySelector(`.pipeline-step[data-node="${n}"]`);
      if (!step) return;
      if (i < idx) step.className = 'pipeline-step done';
      else if (i === idx) step.className = 'pipeline-step running';
    });
    return;
  }

  const step = document.querySelector(`.pipeline-step[data-node="${uiNode}"]`);
  if (!step) return;
  if (status === 'done') step.className = 'pipeline-step done';
  else if (status === 'error') step.className = 'pipeline-step error';
}

function updateSubtaskList(subtasks) {
  if (!subtasks || !subtasks.length) return;
  const container = $('overview-subtasks');
  container.innerHTML = subtasks.map(st => `
    <div class="subtask-row">
      <span class="subtask-dot ${st.status || 'pending'}"></span>
      <span class="subtask-desc">${escapeHtml(st.description || st.id)}</span>
      <span class="subtask-tag">${escapeHtml(st.difficulty || 'medium')}</span>
    </div>`).join('');
}

function appendLog(level, message) {
  const panel = $('log-panel');
  if (panel.querySelector('.log-line.info') && panel.textContent.includes('等待执行')) {
    panel.innerHTML = '';
  }
  const line = document.createElement('div');
  line.className = 'log-line ' + level;
  line.innerHTML = `<span class="log-time">${formatTime(new Date())}</span>${escapeHtml(message)}`;
  panel.appendChild(line);
  panel.scrollTop = panel.scrollHeight;
  logEntries.push({ level, message, time: new Date() });
}

function clearLogs() {
  logEntries = [];
  $('log-panel').innerHTML = '<div class="log-line info">等待执行…</div>';
}

function showTaskResult(result) {
  const el = $('overview-result');
  if (!result || typeof result !== 'object') { el.classList.add('hidden'); return; }
  el.classList.remove('hidden');
  let html = '<h4 style="margin:0 0 8px;font-size:13px">执行结果</h4>';
  if (result.merged_diff) renderDiff(result.merged_diff);
  if (result.learn_summary) {
    try {
      tryShowLearnNotice(typeof result.learn_summary === 'string' ? JSON.parse(result.learn_summary) : result.learn_summary);
    } catch { /* ignore */ }
  }
  if (result.l2_passed !== undefined) {
    html += `<p style="font-size:12px;margin:4px 0">L2 验证: ${result.l2_passed ? '✅ 通过' : '❌ 未通过'}</p>`;
  }
  if (result.l3_passed !== undefined && result.l3_passed !== null) {
    html += `<p style="font-size:12px;margin:4px 0">L3 验证: ${result.l3_passed ? '✅ 通过' : '❌ 未通过'}</p>`;
  }
  if (result.l3_message) {
    html += `<p style="font-size:11px;margin:2px 0;color:var(--text-muted)">${escapeHtml(String(result.l3_message))}</p>`;
  }
  if (result.plan_validation_issues && result.plan_validation_issues.length) {
    html += `<p style="font-size:12px;margin:4px 0;color:var(--orange)">计划校验: ${result.plan_validation_issues.map(i => escapeHtml(i)).join('; ')}</p>`;
  }
  if (result.shared_contract && Object.keys(result.shared_contract).length) {
    html += `<details style="font-size:11px;margin:4px 0"><summary>共享契约</summary><pre style="white-space:pre-wrap;margin:4px 0">${escapeHtml(JSON.stringify(result.shared_contract, null, 2))}</pre></details>`;
  }
  if (result.verification_failure) {
    html += `<p style="font-size:12px;margin:4px 0;color:var(--red)">验证阻断: ${escapeHtml(String(result.verification_failure))}</p>`;
  }
  if (result.complexity) {
    html += `<p style="font-size:12px;margin:4px 0">复杂度: ${escapeHtml(String(result.complexity))}</p>`;
  }
  el.innerHTML = html;
}

async function deleteTask(taskId, forceActive) {
  const task = tasks.find(t => t.id === taskId) || (selectedTaskDetail?.id === taskId ? selectedTaskDetail : null);
  const isActive = forceActive || (task && isTaskActive(task.status));
  const msg = isActive
    ? '任务处于活跃状态，强制删除将先取消执行。确定删除？'
    : '确定删除此任务？';
  if (!confirm(msg)) return;
  try {
    closeTaskSSE();
    const url = '/api/tasks/' + encodeURIComponent(taskId) + (isActive ? '?force=true' : '');
    const resp = await fetch(url, { method: 'DELETE' });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || '删除失败');
    }
    showToast('任务已删除', 'success');
    if (selectedTaskId === taskId) {
      selectedTaskId = null;
      selectedTaskDetail = null;
      showTaskDetailEmpty();
    }
    if (selectedProjectId) await loadTasks(selectedProjectId);
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function cancelTask(taskId) {
  if (!confirm('确定取消此任务？')) return;
  try {
    closeTaskSSE();
    const resp = await fetch('/api/tasks/' + encodeURIComponent(taskId) + '/cancel', { method: 'POST' });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || '取消失败');
    }
    showToast('任务已取消', 'success');
    if (selectedProjectId) await loadTasks(selectedProjectId);
    if (selectedTaskId === taskId) await selectTask(taskId);
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function retryTask(taskId) {
  if (!confirm('确定重跑此任务？将重置计划与 Diff 并重新执行。')) return;
  try {
    closeTaskSSE();
    clearLogs();
    resetPipeline();
    switchDetailTab('logs');
    const autoAccept = $('task-auto-accept')?.checked || false;
    const resp = await fetch('/api/tasks/' + encodeURIComponent(taskId) + '/retry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ auto_accept: autoAccept }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || '重跑失败');
    }
    showToast('已提交重跑…', 'success');
    startTaskSSE(taskId);
    if (selectedProjectId) await loadTasks(selectedProjectId);
    if (selectedTaskId === taskId) await selectTask(taskId);
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function refreshTaskReadinessHint(projectId) {
  const el = $('task-readiness-hint');
  if (!el || !projectId) return;
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/knowledge/overview');
    if (!resp.ok) { el.classList.add('hidden'); return; }
    const readiness = assessKnowledgeReadiness(await resp.json());
    if (readiness.level === 'ready' || readiness.level === 'partial') {
      el.classList.add('hidden');
      el.textContent = '';
      return;
    }
    el.classList.remove('hidden');
    el.textContent = readiness.message || '知识库未就绪';
  } catch {
    el.classList.add('hidden');
  }
}

async function ensureTaskReadiness() {
  if (!selectedProjectId) return true;
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/overview');
    if (!resp.ok) return true;
    const readiness = assessKnowledgeReadiness(await resp.json());
    if (readiness.level === 'ready' || readiness.level === 'partial') return true;
    const msg = readiness.message || '知识库尚未就绪，Brain 检索质量可能较差。';
    return confirm(msg + '\n\n仍要创建任务？');
  } catch {
    return true;
  }
}

// ─── Create & SSE ────────────────────────────────────────────
async function createTask() {
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  const description = $('new-task-input').value.trim();
  if (!description) { showToast('请输入任务描述', 'warning'); return; }
  if (!await ensureTaskReadiness()) return;

  $('btn-create-task').disabled = true;
  try {
    clearLogs();
    resetPipeline();
    showTaskDetailPanel();

    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        description,
        auto_accept: $('task-auto-accept')?.checked || false,
      }),
    });
    if (!resp.ok) throw new Error(await resp.text());

    const data = await resp.json();
    const taskId = data.task?.id;
    if (!taskId) throw new Error('未返回 task id');

    $('new-task-input').value = '';
    selectedTaskId = taskId;
    appendLog('info', '任务已创建，Brain 编排中…');
    showToast('任务已创建', 'success');
    switchDetailTab('logs');

    await loadTasks(selectedProjectId);
    await selectTask(taskId);
    startTaskSSE(taskId);
  } catch (e) {
    showToast('创建失败: ' + e.message, 'error');
    appendLog('error', e.message);
  } finally {
    $('btn-create-task').disabled = false;
  }
}

let sseRefreshTimer = null;

function startTaskSSE(taskId) {
  closeTaskSSE();
  selectedTaskId = taskId;

  try {
    const url = '/api/tasks/' + encodeURIComponent(taskId) + '/stream';
    taskEventSource = new EventSource(url);

    const handlePayload = (e, eventType) => {
      try {
        const data = JSON.parse(e.data);
        handleBrainProgressEvent(data, eventType);

        if (eventType === 'awaiting_review' || data.step === 'awaiting_review') {
          appendLog('warning', data.message || '等待人工审核');
          if (selectedProjectId) loadTasks(selectedProjectId).then(() => selectTask(taskId));
        }
        if (data.knowledge_stats) showKnowledgeBanner(data.knowledge_stats, data.complexity);
        if (eventType === 'result' || data.step === 'result') {
          showTaskResult(data.result);
          if (data.result?.merged_diff) renderDiff(data.result.merged_diff);
          if (data.result?.plan) renderPlan(data.result.plan);
        }
        if (data.step === 'complete' || eventType === 'result') {
          appendLog('success', '任务完成');
          if (selectedProjectId) loadTasks(selectedProjectId).then(() => selectTask(taskId));
          closeTaskSSE();
          refreshSandboxes(selectedProjectId);
        }
        if (eventType === 'error' || data.step === 'cancelled' || data.status === 'cancelled') {
          appendLog('warning', data.message || (data.step === 'cancelled' ? '任务已取消' : '任务失败'));
          closeTaskSSE();
          if (selectedProjectId) loadTasks(selectedProjectId).then(() => {
            if (selectedTaskId === taskId) selectTask(taskId);
          });
        }
      } catch {
        appendLog('info', e.data);
      }
    };

    taskEventSource.addEventListener('progress', e => handlePayload(e, 'progress'));
    taskEventSource.addEventListener('awaiting_review', e => handlePayload(e, 'awaiting_review'));
    taskEventSource.addEventListener('result', e => handlePayload(e, 'result'));
    taskEventSource.addEventListener('error', e => { if (e.data) handlePayload(e, 'error'); });
    taskEventSource.onmessage = e => handlePayload(e, 'progress');
    taskEventSource.onerror = () => {
      if (sseRefreshTimer) return;
      sseRefreshTimer = setTimeout(() => {
        sseRefreshTimer = null;
        if (selectedTaskId) {
          fetch('/api/tasks/' + encodeURIComponent(selectedTaskId))
            .then(r => r.ok ? r.json() : null)
            .then(data => {
              if (!data?.task) return;
              selectedTaskDetail = data.task;
              selectedTaskDetail.plan = normalizePlan(selectedTaskDetail.plan);
              renderTaskDetail(selectedTaskDetail);
            })
            .catch(() => {});
        }
      }, 3000);
    };
  } catch (e) {
    showToast('SSE 连接失败: ' + e.message, 'error');
  }
}

function closeTaskSSE() {
  if (taskEventSource) { taskEventSource.close(); taskEventSource = null; }
}

function closeWorkerSSE() {
  if (workerEventSource) { workerEventSource.close(); workerEventSource = null; }
}

function resetWorkerPanelForProject() {
  const statusEl = $('worker-run-status');
  if (statusEl && !workerRunId) statusEl.textContent = selectedProjectId ? '就绪' : '请先选择项目';
}

function appendWorkerLog(level, msg) {
  const panel = $('worker-log-panel');
  if (!panel) return;
  const line = document.createElement('div');
  line.className = 'log-line ' + (level || 'info');
  line.textContent = msg;
  panel.appendChild(line);
  panel.scrollTop = panel.scrollHeight;
}

function clearWorkerLogs() {
  const panel = $('worker-log-panel');
  if (panel) panel.innerHTML = '<div class="log-line info">日志已清空</div>';
  const block = $('worker-result-block');
  if (block) block.classList.add('hidden');
  const diffEl = $('worker-diff-content');
  if (diffEl) diffEl.innerHTML = '';
  workerLastDiff = '';
}

async function startWorkerRun() {
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  const description = ($('worker-description')?.value || '').trim();
  if (!description) { showToast('请输入子任务描述', 'warning'); return; }

  closeWorkerSSE();
  clearWorkerLogs();
  workerRunId = null;
  const btn = $('btn-worker-run');
  if (btn) btn.disabled = true;
  const statusEl = $('worker-run-status');
  if (statusEl) statusEl.textContent = '启动中…';

  const body = {
    description,
    difficulty: $('worker-difficulty')?.value || 'medium',
  };
  const writable = parseScopeInput($('worker-writable')?.value);
  const readable = parseScopeInput($('worker-readable')?.value);
  if (writable) body.writable = writable;
  if (readable) body.readable = readable;

  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/worker/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || '启动失败');
    }
    const data = await resp.json();
    workerRunId = data.run_id;
    appendWorkerLog('info', 'Worker 已启动 run_id=' + workerRunId);
    if (statusEl) statusEl.textContent = '运行中…';
    startWorkerSSE(workerRunId);
  } catch (e) {
    showToast(e.message, 'error');
    if (statusEl) statusEl.textContent = '启动失败';
  } finally {
    if (btn) btn.disabled = false;
  }
}

function startWorkerSSE(runId) {
  closeWorkerSSE();
  const url = '/api/worker/' + encodeURIComponent(runId) + '/stream';
  workerEventSource = new EventSource(url);

  const handle = (e, eventType) => {
    try {
      const data = JSON.parse(e.data);
      if (data.step === 'log' && data.message) {
        appendWorkerLog('info', '[' + (data.phase || '?') + '] ' + data.message);
      } else if (data.message) {
        appendWorkerLog(data.status === 'error' ? 'error' : 'info', data.message);
      }
      if (eventType === 'result' || data.step === 'result') {
        renderWorkerResult(data.result || data);
      }
      if (data.step === 'complete' || eventType === 'error' || data.step === 'error') {
        closeWorkerSSE();
        const statusEl = $('worker-run-status');
        if (statusEl) {
          statusEl.textContent = data.step === 'error' ? '失败' : '完成';
        }
        refreshSandboxes(selectedProjectId);
      }
    } catch {
      if (e.data) appendWorkerLog('info', e.data);
    }
  };

  workerEventSource.addEventListener('progress', ev => handle(ev, 'progress'));
  workerEventSource.addEventListener('result', ev => handle(ev, 'result'));
  workerEventSource.addEventListener('error', ev => { if (ev.data) handle(ev, 'error'); });
  workerEventSource.onmessage = ev => handle(ev, 'progress');
  workerEventSource.onerror = () => {
    /* EventSource 会自动重连；完成时由 complete 事件关闭 */
  };
}

function renderWorkerResult(result) {
  const block = $('worker-result-block');
  const summary = $('worker-result-summary');
  const diffEl = $('worker-diff-content');
  if (!block || !summary || !diffEl) return;
  block.classList.remove('hidden');
  const diff = result.diff || result.merged_diff || '';
  workerLastDiff = diff;
  const success = result.l1_passed !== false;
  summary.innerHTML = `
    <span class="pill ${success ? 'pill-green' : 'pill-red'}">${success ? '成功' : '失败'}</span>
    <span class="pill pill-gray">${escapeHtml(result.phase || 'done')}</span>
    ${result.summary ? '<p style="margin:8px 0 0">' + escapeHtml(result.summary) + '</p>' : ''}`;
  if (diff && diff.trim()) {
    const lines = diff.split('\n');
    diffEl.innerHTML = lines.map(line => {
      let cls = '';
      if (line.startsWith('+') && !line.startsWith('+++')) cls = 'diff-line-add';
      else if (line.startsWith('-') && !line.startsWith('---')) cls = 'diff-line-del';
      else if (line.startsWith('@@')) cls = 'diff-line-hunk';
      return `<div class="${cls}">${escapeHtml(line)}</div>`;
    }).join('');
  } else {
    diffEl.innerHTML = '<div class="diff-empty">无 diff 输出</div>';
  }
}

async function checkWorkerDiff() {
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  if (!workerLastDiff || !workerLastDiff.trim()) { showToast('无 Diff 可校验', 'warning'); return; }
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/apply-diff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ diff: workerLastDiff, check_only: true }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail || err));
    }
    showToast('git apply --check 通过', 'success');
  } catch (e) {
    showToast('校验失败: ' + e.message, 'error');
  }
}

async function applyWorkerDiff() {
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  if (!workerLastDiff || !workerLastDiff.trim()) { showToast('无 Diff 可应用', 'warning'); return; }
  if (!confirm('将 Worker diff 应用到项目 git 工作区？请确保已备份或已提交当前更改。')) return;
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/apply-diff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ diff: workerLastDiff, check_only: false }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail || err));
    }
    const data = await resp.json();
    showToast(data.message || 'Diff 已应用', 'success');
  } catch (e) {
    showToast('应用失败: ' + e.message, 'error');
  }
}

async function applyTaskDiff() {
  if (!selectedTaskId) { showToast('请先选择任务', 'warning'); return; }
  if (taskHasMergeConflicts(selectedTaskDetail)) {
    showToast('存在 merge 冲突，无法 apply', 'error');
    return;
  }
  if (!confirm('将 merged_diff 应用到项目 git 工作区？请确保已备份或已提交当前更改。')) return;
  try {
    const resp = await fetch('/api/tasks/' + encodeURIComponent(selectedTaskId) + '/apply-diff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ check_only: false }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail || err));
    }
    const data = await resp.json();
    showToast(data.message || 'Diff 已应用', 'success');
  } catch (e) {
    showToast('应用失败: ' + e.message, 'error');
  }
}

async function checkApplyDiff() {
  if (!selectedTaskId) { showToast('请先选择任务', 'warning'); return; }
  try {
    const resp = await fetch('/api/tasks/' + encodeURIComponent(selectedTaskId) + '/apply-diff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ check_only: true }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail || err));
    }
    showToast('git apply --check 通过', 'success');
  } catch (e) {
    showToast('校验失败: ' + e.message, 'error');
  }
}

function handleBrainProgressEvent(data) {
  const msg = (data.message || data.msg || '').trim();
  if (msg) {
    const level = data.status === 'error' ? 'error' : data.status === 'warning' ? 'warning' : 'info';
    appendLog(level, msg);
  }
  if (data.node) {
    updateNodeStatus(data.node, data.status === 'done' ? 'done' : 'running');
  }
  if (data.subtasks) updateSubtaskList(data.subtasks);
  if (data.knowledge_stats) showKnowledgeBanner(data.knowledge_stats, data.complexity);
  if (data.node === 'dispatch') refreshSandboxes(selectedProjectId);
}

// ─── Review Actions ────────────────────────────────────────
function approveCurrentTask() {
  if (selectedTaskId) approveTask(selectedTaskId);
}

function reviseCurrentTask() {
  if (!selectedTaskId) return;
  reviseTargetTaskId = selectedTaskId;
  $('revise-feedback').value = '';
  $('revise-overlay').classList.add('open');
  $('revise-modal').classList.add('open');
}

function rejectCurrentTask() {
  if (selectedTaskId) rejectTask(selectedTaskId);
}

async function approveTask(taskId) {
  try {
    clearLogs();
    resetPipeline();
    switchDetailTab('logs');
    const applyDiff = $('approve-apply-diff')?.checked || false;
    const resp = await fetch('/api/tasks/' + encodeURIComponent(taskId) + '/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ apply_diff: applyDiff }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(typeof err.detail === 'string' ? err.detail : '操作失败');
    }
    const data = await resp.json().catch(() => ({}));
    if (data.apply_diff?.ok) showToast('Diff 已应用到工作区', 'success');
    showToast('已通过，Brain 继续执行…', 'success');
    startTaskSSE(taskId);
    if (selectedProjectId) await loadTasks(selectedProjectId);
  } catch (e) {
    showToast('操作失败: ' + e.message, 'error');
  }
}

async function submitReviseTask(taskId, feedback) {
  try {
    clearLogs();
    resetPipeline();
    switchDetailTab('logs');
    const resp = await fetch('/api/tasks/' + encodeURIComponent(taskId) + '/revise', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ feedback }),
    });
    if (!resp.ok) throw new Error('操作失败');
    showToast('已提交修订', 'success');
    startTaskSSE(taskId);
    if (selectedProjectId) await loadTasks(selectedProjectId);
  } catch (e) {
    showToast('修订失败: ' + e.message, 'error');
  }
}

async function rejectTask(taskId) {
  if (!confirm('确定拒绝此任务？')) return;
  try {
    clearLogs();
    resetPipeline();
    switchDetailTab('logs');
    const resp = await fetch('/api/tasks/' + encodeURIComponent(taskId) + '/reject', { method: 'POST' });
    if (!resp.ok) throw new Error('操作失败');
    showToast('已拒绝', 'success');
    startTaskSSE(taskId);
    if (selectedProjectId) await loadTasks(selectedProjectId);
  } catch (e) {
    showToast('拒绝失败: ' + e.message, 'error');
  }
}

const PREPROCESS_PHASE_ORDER = ['scanning', 'indexing', 'embedding', 'analyzing', 'complete'];

async function loadPreprocessStatus(projectId) {
  if (!projectId) return;
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/preprocess/status');
    if (!resp.ok) return;
    const data = await resp.json();
    const progress = data.progress;
    if (progress) {
      updatePreprocessProgress(progress);
      renderPreprocessStats(progress);
    } else if (data.project_status === 'READY') {
      updatePreprocessProgress({
        phase: 'complete',
        phase_progress: 1,
        message: '预处理已完成（无进度记录，项目状态 READY）',
      });
    }
    if (data.project_status === 'PREPROCESSING' && !preprocessSSE) {
      connectPreprocessSSE(projectId);
    }
  } catch { /* ignore */ }
}

function renderPreprocessPipeline(currentPhase) {
  const pipeline = $('preprocess-pipeline');
  if (!pipeline) return;
  const phase = String(currentPhase || 'idle').toLowerCase();
  const order = PREPROCESS_PHASE_ORDER;
  const idx = order.indexOf(phase);
  pipeline.querySelectorAll('.preprocess-step').forEach(el => {
    const p = el.dataset.phase;
    el.classList.remove('active', 'done', 'error');
    if (phase === 'error') {
      el.classList.add('error');
      return;
    }
    if (phase === 'complete') {
      el.classList.add('done');
      return;
    }
    const pi = order.indexOf(p);
    if (pi < 0) return;
    if (pi < idx) el.classList.add('done');
    else if (pi === idx) el.classList.add('active');
  });
}

function renderPreprocessStats(data) {
  const el = $('preprocess-stats');
  if (!el || !data) return;
  const scan = data.scan_stats || {};
  const index = data.index_stats || {};
  const embed = data.embed_stats || {};
  const analysis = data.analysis_stats || {};
  if (!Object.keys(scan).length && !Object.keys(index).length) {
    el.innerHTML = '';
    return;
  }
  const cards = [
    ['文件', scan.files ?? '—'],
    ['符号', index.symbols ?? '—'],
    ['依赖边', index.edges ?? '—'],
    ['向量', embed.skipped ? '跳过' : (embed.vectors ?? '—')],
    ['分析', analysis.summary_tokens ? analysis.summary_tokens + ' tok' : '—'],
  ];
  el.innerHTML = cards.map(([k, v]) => `
    <div class="preprocess-stat-card"><div class="k">${escapeHtml(k)}</div><div class="v">${escapeHtml(String(v))}</div></div>
  `).join('');
}

// ─── Preprocess SSE ──────────────────────────────────────────
function connectPreprocessSSE(projectId) {
  disconnectPreprocessSSE();
  try {
    preprocessSSE = new EventSource('/api/projects/' + encodeURIComponent(projectId) + '/preprocess/progress');

    const handleProgress = (e) => {
      try {
        updatePreprocessProgress(JSON.parse(e.data));
      } catch { /* ignore malformed payload */ }
    };

    preprocessSSE.onmessage = handleProgress;
    preprocessSSE.addEventListener('progress', handleProgress);
    // 不用 addEventListener('error') — 会与 EventSource 原生连接错误冲突
    preprocessSSE.onerror = () => {
      if (preprocessSSE && preprocessSSE.readyState === EventSource.CLOSED) {
        preprocessSSE = null;
      }
    };
  } catch { /* ignore */ }
}

function disconnectPreprocessSSE() {
  if (preprocessSSE) { preprocessSSE.close(); preprocessSSE = null; }
}

function updatePreprocessProgress(data) {
  if (!data) return;
  const phase = String(data.phase || 'idle');
  const raw = data.phase_progress ?? data.progress ?? data.percent ?? 0;
  let pct = Math.round(Number(raw) * (Number(raw) <= 1 ? 100 : 1));
  if (phase === 'complete') pct = 100;

  const phaseEl = $('preprocess-phase');
  if (phaseEl) {
    phaseEl.textContent = phase.toUpperCase();
    phaseEl.className = 'pill ' + (
      phase === 'complete' ? 'pill-green' :
      phase === 'error' ? 'pill-red' :
      phase === 'idle' ? 'pill-gray' : 'pill-blue'
    );
  }
  if ($('preprocess-percent')) $('preprocess-percent').textContent = pct + '%';
  if ($('preprocess-bar')) $('preprocess-bar').style.width = pct + '%';
  if ($('preprocess-message')) {
    $('preprocess-message').textContent = data.message || (phase === 'complete' ? '预处理完成' : '');
  }
  renderPreprocessPipeline(phase);
  renderPreprocessStats(data);

  const errEl = $('preprocess-error');
  if (phase === 'error' || data.error) {
    const msg = data.error || data.message || '预处理失败';
    if (errEl) {
      errEl.textContent = msg;
      errEl.classList.remove('hidden');
    }
    if (preprocessSSE) showToast(msg, 'error');
    loadProjects();
    disconnectPreprocessSSE();
  } else if (errEl) {
    errEl.classList.add('hidden');
    errEl.textContent = '';
  }

  if (phase === 'complete') {
    if (preprocessSSE) showToast('预处理完成', 'success');
    loadProjects();
    loadKnowledgeOverview(selectedProjectId);
    disconnectPreprocessSSE();
  }
}

// ─── Health & Status ─────────────────────────────────────────
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

function renderComponents(components) {
  const grid = $('components-grid');
  const map = {};
  (components || []).forEach(c => { map[c.name] = c; });

  grid.innerHTML = COMPONENT_DEFS.map(def => {
    const c = map[def.name] || { status: 'unknown', detail: '' };
    const statusCls = c.status === 'running' || c.status === 'ready' ? 'pill-green'
      : c.status === 'degraded' ? 'pill-amber' : c.status === 'error' ? 'pill-red' : 'pill-gray';
    return `
      <div class="component-card">
        <div class="name">${escapeHtml(def.name)}</div>
        <span class="pill ${statusCls}">${escapeHtml(c.status)}</span>
        <div class="status" style="margin-top:6px">${escapeHtml(c.detail || '')}</div>
      </div>`;
  }).join('');
}

// ─── Phase 5: System stats & notifications ───────────────────
function formatDurationSeconds(sec) {
  if (sec == null || Number.isNaN(Number(sec))) return '—';
  const n = Number(sec);
  if (n < 60) return Math.round(n) + 's';
  if (n < 3600) return Math.round(n / 60) + 'm';
  return (n / 3600).toFixed(1) + 'h';
}

function formatAcceptRate(rate) {
  if (rate == null) return '—';
  return (Number(rate) * 100).toFixed(1) + '%';
}

function notificationsUrl(since) {
  const params = [];
  if (selectedProjectId) params.push('project_id=' + encodeURIComponent(selectedProjectId));
  if (since) params.push('since=' + encodeURIComponent(since));
  return params.length ? '/api/notifications?' + params.join('&') : '/api/notifications';
}

function formatTokenCount(n) {
  if (n == null || Number.isNaN(Number(n))) return '—';
  const val = Number(n);
  if (val >= 1_000_000) return (val / 1_000_000).toFixed(1) + 'M';
  if (val >= 1_000) return (val / 1_000).toFixed(1) + 'K';
  return String(Math.round(val));
}

function learningTrendBadge(trend) {
  if (trend === 'improving') {
    return '<span class="pill pill-green" title="近 30 天错题少于前 30 天">改善中</span>';
  }
  if (trend === 'stable') {
    return '<span class="pill pill-amber" title="错题数量持平或上升">稳定</span>';
  }
  return '<span class="pill pill-gray" title="数据不足">未知</span>';
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

async function loadNotifications() {
  try {
    const resp = await fetch(notificationsUrl());
    if (!resp.ok) return;
    const data = await resp.json();
    renderNotifications(data.notifications || []);
  } catch { /* ignore */ }
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

async function openNotificationTask(taskId, projectId) {
  dismissNotificationBanner();
  if (projectId && projectId !== selectedProjectId) {
    selectProject(projectId);
  }
  switchTab('tasks');
  await selectTask(taskId);
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

function formatTestLine(label, item) {
  if (!item) return `${label}: 未知`;
  if (item.ok) return `✓ ${label} (${escapeHtml(item.model || '')}): ${escapeHtml(item.preview || 'OK')}`;
  return `✗ ${label} (${escapeHtml(item.model || '')}): ${escapeHtml(item.error || 'failed')}`;
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

function formatBytes(n) {
  const num = Number(n);
  if (!num || num < 0) return '';
  if (num < 1024) return num + ' B';
  if (num < 1024 * 1024) return (num / 1024).toFixed(1) + ' KB';
  return (num / (1024 * 1024)).toFixed(1) + ' MB';
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

function formatLogTime(iso) {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleTimeString('zh-CN', { hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0');
  } catch {
    return iso;
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
async function runRetrieveExperiment() {
  const el = $('retrieve-result');
  const query = ($('retrieve-query')?.value || '').trim();
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  if (!query) { showToast('请输入任务描述', 'warning'); return; }
  if (el) el.innerHTML = '<p style="color:var(--text-muted)">检索中…</p>';
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/retrieve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    renderRetrieveResult(await resp.json());
  } catch (e) {
    if (el) el.innerHTML = '<p style="color:var(--red)">失败: ' + escapeHtml(e.message) + '</p>';
  }
}

function renderRetrieveResult(data) {
  const el = $('retrieve-result');
  if (!el) return;
  const raw = data.raw_counts || {};
  const limits = data.limits || {};
  const slices = data.slices || {};
  const hitBlock = (title, items) => {
    if (!items || !items.length) return '';
    return `<details style="margin-bottom:8px"><summary style="cursor:pointer;font-size:12px;font-weight:600">${title} (${items.length})</summary>
      <ul style="margin:6px 0 0;padding-left:18px;font-size:11px;line-height:1.5">${items.slice(0, 8).map(it => {
        const label = typeof it === 'string' ? it : (it.title || it.symbol_name || it.file_path || it.content?.slice?.(0, 60) || JSON.stringify(it).slice(0, 80));
        return `<li>${escapeHtml(String(label))}</li>`;
      }).join('')}</ul></details>`;
  };
  let html = `
    <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">
      <span class="pill pill-green">prompt ${data.prompt_chars || 0} 字</span>
      <span class="pill pill-gray">struct ${raw.struct ?? 0}→${limits.struct ?? '?'}</span>
      <span class="pill pill-gray">semantic ${raw.semantic ?? 0}→${limits.semantic ?? '?'}</span>
      <span class="pill pill-gray">harness ${raw.norms ?? 0}→${limits.norms ?? '?'}</span>
      <span class="pill pill-gray">错题 ${raw.mistakes ?? 0}</span>
      <span class="pill pill-gray">成功 ${raw.successes ?? 0}</span>
    </div>
    ${hitBlock('结构 struct', slices.struct)}
    ${hitBlock('语义 semantic', slices.semantic)}
    ${hitBlock('Harness', slices.norms)}
    <details open><summary style="cursor:pointer;font-size:12px;font-weight:600;margin-bottom:8px">Brain 上下文预览</summary>
      <pre class="retrieve-preview">${escapeHtml(data.prompt_preview || '')}</pre>
    </details>`;
  el.innerHTML = html;
}

async function searchSymbols() {
  const el = $('symbol-search-results');
  const q = ($('symbol-search-q')?.value || '').trim();
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  if (!q) { showToast('请输入符号名', 'warning'); return; }
  if (el) el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">搜索中…</p>';
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/symbols?q=' + encodeURIComponent(q));
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();
    const symbols = data.symbols || [];
    if (!symbols.length) {
      el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">无匹配符号</p>';
      return;
    }
    el.innerHTML = `<table style="width:100%;font-size:11px;border-collapse:collapse">
      <thead><tr style="text-align:left;color:var(--text-muted)"><th>符号</th><th>类型</th><th>文件</th><th>行</th></tr></thead>
      <tbody>${symbols.map(s => `
        <tr><td>${escapeHtml(s.symbol_name || '')}</td><td>${escapeHtml(s.symbol_type || '')}</td>
        <td>${escapeHtml(s.file_path || '')}</td><td>${s.start_line || ''}</td></tr>`).join('')}
      </tbody></table>`;
  } catch (e) {
    if (el) el.innerHTML = '<p style="color:var(--red)">失败: ' + escapeHtml(e.message) + '</p>';
  }
}

// ─── Knowledge (Overview + Norms) ────────────────────────────
const ROUTING_TIER_DEFS = [
  { key: 'trivial', label: '简单 trivial', hint: '改配置 / 小修复 → 本地小模型' },
  { key: 'medium', label: '中等 medium', hint: '单模块开发 → 本地代码模型' },
  { key: 'complex', label: '复杂 complex', hint: '跨模块 / 架构 → 云端大模型' },
  { key: 'multimodal', label: '多模态 multimodal', hint: '看图 / UI → 视觉模型' },
];

async function loadKnowledgeOverview(projectId) {
  const el = $('knowledge-overview');
  if (!el || !projectId) return;
  el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">加载中…</p>';
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/knowledge/overview');
    if (!resp.ok) throw new Error('fetch failed');
    renderKnowledgeOverview(await resp.json());
  } catch {
    el.innerHTML = '<p style="font-size:12px;color:var(--red)">加载失败</p>';
  }
}

function assessKnowledgeReadiness(data) {
  const pp = data.preprocess || {};
  const phase = String(pp.phase || '').toLowerCase();
  const projectStatus = data.status || 'UNKNOWN';
  const index = pp.index_stats || {};
  const embed = pp.embed_stats || {};

  const preprocessDone = phase === 'complete' || projectStatus === 'READY';
  const preprocessRunning = projectStatus === 'PREPROCESSING'
    || ['scanning', 'indexing', 'embedding', 'analyzing'].includes(phase);
  const preprocessFailed = phase === 'error' || projectStatus === 'ERROR';

  if (preprocessFailed) {
    return { level: 'error', message: pp.error || pp.message || '预处理失败，请查看预处理 Tab' };
  }
  if (preprocessRunning) {
    return { level: 'running', message: `预处理进行中（${phase || '…'}）— 完成后 Brain 检索将可用` };
  }
  if (!preprocessDone) {
    return { level: 'missing', message: '尚未运行预处理 — Brain 检索质量将受限', showPreprocessCta: true };
  }

  const partial = !!(index.skipped || embed.skipped);
  if (partial) {
    const parts = [];
    if (index.skipped) parts.push('结构索引(Layer A)已跳过');
    if (embed.skipped) parts.push('向量嵌入(Layer B)已跳过');
    return {
      level: 'partial',
      message: '预处理已完成 · ' + parts.join('，') + '（Brain 仍可使用扫描/分析结果，见下方说明）',
    };
  }
  return { level: 'ready', message: '知识库已就绪 — Brain 可正常检索本项目' };
}

function renderKnowledgeStatusBanner(readiness) {
  if (!readiness) return '';
  const styles = {
    ready: { border: 'var(--green)', bg: 'rgba(34,197,94,0.08)', pill: 'pill-green' },
    partial: { border: 'var(--amber)', bg: 'rgba(245,158,11,0.08)', pill: 'pill-amber' },
    running: { border: 'var(--blue)', bg: 'rgba(59,130,246,0.08)', pill: 'pill-blue' },
    missing: { border: 'var(--amber)', bg: 'rgba(245,158,11,0.08)', pill: 'pill-amber' },
    error: { border: 'var(--red)', bg: 'rgba(239,68,68,0.08)', pill: 'pill-red' },
  };
  const s = styles[readiness.level] || styles.missing;
  const cta = readiness.showPreprocessCta
    ? `<button class="btn btn-primary btn-sm" style="margin-top:8px" onclick="switchTab('preprocess')">前往预处理 →</button>`
    : (readiness.level === 'error'
      ? `<button class="btn btn-secondary btn-sm" style="margin-top:8px" onclick="switchTab('preprocess')">查看预处理 →</button>`
      : '');
  return `
    <div class="card" style="padding:12px;margin-bottom:12px;background:${s.bg};border:1px solid ${s.border}">
      <span class="pill ${s.pill}" style="margin-bottom:6px">${readiness.level === 'ready' ? '已就绪' : readiness.level === 'partial' ? '部分就绪' : readiness.level === 'running' ? '进行中' : readiness.level === 'error' ? '异常' : '未预处理'}</span>
      <p style="margin:0;font-size:12px;line-height:1.5;color:var(--text-primary)">${escapeHtml(readiness.message)}</p>
      ${cta}
    </div>`;
}

function renderKnowledgeOverview(data) {
  const el = $('knowledge-overview');
  const pp = data.preprocess || {};
  const scan = pp.scan_stats || {};
  const index = pp.index_stats || {};
  const embed = pp.embed_stats || {};
  const graphStatus = data.graph_status || 'NONE';
  const projectStatus = data.status || 'UNKNOWN';
  const readiness = assessKnowledgeReadiness(data);
  const langs = data.language_breakdown || scan.languages || {};
  const langStr = typeof langs === 'object' && !Array.isArray(langs)
    ? Object.entries(langs).map(([k, v]) => `${k}(${v})`).join(', ')
    : (Array.isArray(langs) ? langs.join(', ') : '');

  const remediation = buildKnowledgeRemediation(data, index, embed, graphStatus, readiness);

  el.innerHTML = `
    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;align-items:center">
      ${graphStatusTagForOverview(graphStatus, index)}
      ${projectStatusTag(projectStatus)}
      <span class="pill pill-blue">预处理 ${escapeHtml(pp.phase || 'unknown')}</span>
      <span class="pill pill-gray">${data.file_count || scan.files || 0} 文件</span>
      <span class="pill pill-gray">${data.symbol_count || data.project_symbol_count || index.symbols || 0} 符号</span>
      <span class="pill pill-gray">${data.qdrant_vectors || 0} 向量</span>
      <span class="pill pill-gray">${data.norms_count || 0} Harness</span>
    </div>
    ${renderKnowledgeStatusBanner(readiness)}
    ${remediation}
    ${langStr ? `<p style="font-size:11px;color:var(--text-muted);margin:0 0 10px">语言: ${escapeHtml(langStr)}</p>` : ''}
    ${embed.skipped && readiness.level !== 'partial' ? `<p style="font-size:11px;color:var(--amber);margin:0 0 10px">向量嵌入已跳过: ${escapeHtml(embed.reason || 'unknown')}</p>` : ''}
    ${pp.error && readiness.level === 'error' ? `<p style="font-size:11px;color:var(--red);margin:0 0 10px">${escapeHtml(pp.error)}</p>` : ''}
    <h4 style="margin:12px 0 6px;font-size:12px;color:var(--text-secondary)">项目架构摘要（Brain 可读）</h4>
    <div style="font-size:12px;line-height:1.6;white-space:pre-wrap;max-height:220px;overflow:auto;color:var(--text-primary)">${escapeHtml(data.description || (readiness.level === 'ready' || readiness.level === 'partial' ? '暂无架构摘要' : '暂无 — 请运行预处理'))}</div>
  `;
}

function buildKnowledgeRemediation(data, index, embed, graphStatus, readiness) {
  if (readiness && (readiness.level === 'missing' || readiness.level === 'running')) {
    return '';
  }
  const cards = [];
  if (index.skipped) {
    const reason = index.reason || 'CodeGraph CLI 未安装或未运行';
    cards.push(`
      <div class="card" style="padding:12px;margin-bottom:10px;border:1px solid var(--border-subtle)">
        <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:var(--amber)">Layer A 结构索引已跳过</p>
        <p style="margin:0 0 8px;font-size:11px;color:var(--text-muted)">${escapeHtml(reason)}</p>
        <p style="margin:0 0 8px;font-size:11px;color:var(--text-secondary)">安装 CodeGraph CLI 后重新预处理，可提升符号级检索精度。</p>
        <button class="btn btn-secondary btn-sm" onclick="switchTab('preprocess');triggerPreprocess()">重新预处理</button>
      </div>`);
  }
  if (embed.skipped) {
    const reason = embed.reason || 'qdrant_unavailable';
    cards.push(`
      <div class="card" style="padding:12px;margin-bottom:10px;border:1px solid var(--border-subtle)">
        <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:var(--amber)">Layer B 向量嵌入已跳过</p>
        <p style="margin:0 0 8px;font-size:11px;color:var(--text-muted)">${escapeHtml(reason)}</p>
        <p style="margin:0 0 8px;font-size:11px;color:var(--text-secondary)">启动 Qdrant 后重新预处理：<code style="font-size:10px">bash scripts/start-services.sh</code></p>
        <button class="btn btn-secondary btn-sm" onclick="switchTab('preprocess');triggerPreprocess()">重新预处理</button>
      </div>`);
  }
  if (data.qdrant_error) {
    cards.push(`
      <div class="card" style="padding:12px;margin-bottom:10px;border:1px solid var(--red)">
        <p style="margin:0 0 6px;font-size:12px;color:var(--red)">Qdrant 连接异常</p>
        <p style="margin:0;font-size:11px">${escapeHtml(data.qdrant_error)}</p>
      </div>`);
  }
  return cards.join('');
}

async function triggerPreprocess() {
  if (!selectedProjectId) return;
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/preprocess', { method: 'POST' });
    if (!resp.ok) throw new Error(await resp.text());
    showToast('预处理已触发', 'success');
    loadPreprocessStatus(selectedProjectId);
  } catch (e) {
    showToast('触发失败: ' + e.message, 'error');
  }
}

async function searchSemantic() {
  const el = $('semantic-search-results');
  const q = ($('semantic-search-q')?.value || '').trim();
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  if (!q) { showToast('请输入检索 query', 'warning'); return; }
  if (el) el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">检索中…</p>';
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/semantic?q=' + encodeURIComponent(q));
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();
    const chunks = data.chunks || [];
    if (!chunks.length) {
      el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">无命中 chunk（检查 Qdrant 是否已嵌入）</p>';
      return;
    }
    el.innerHTML = chunks.map(c => `
      <div class="card" style="margin-bottom:8px;padding:10px">
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px;font-size:11px">
          <span class="pill pill-gray">score ${(c.score ?? 0).toFixed(3)}</span>
          <span class="pill pill-blue">${escapeHtml(c.file_path || '')}:${c.start_line || '?'}</span>
        </div>
        <pre style="margin:0;font-size:11px;white-space:pre-wrap;max-height:120px;overflow:auto">${escapeHtml(c.content_preview || '')}</pre>
      </div>`).join('');
  } catch (e) {
    if (el) el.innerHTML = '<p style="color:var(--red)">失败: ' + escapeHtml(e.message) + '</p>';
  }
}

// ─── Knowledge (Norms) ───────────────────────────────────────
async function loadBehaviorHotspots(projectId) {
  const list = $('behavior-hotspot-list');
  if (!list || !projectId) return;
  list.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">加载中…</p>';
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/knowledge/behavior-hotspots?top_k=15');
    if (!resp.ok) throw new Error('fetch failed');
    const data = await resp.json();
    renderBehaviorHotspots(data.hotspots || []);
  } catch {
    list.innerHTML = '<p style="font-size:12px;color:var(--red)">加载失败</p>';
  }
}

function renderBehaviorHotspots(hotspots) {
  const list = $('behavior-hotspot-list');
  if (!list) return;
  if (!hotspots.length) {
    list.innerHTML = '<p style="font-size:12px;color:var(--text-muted);padding:8px">暂无行为热点（任务 accept 后增量索引会积累修改日志）</p>';
    return;
  }
  list.innerHTML = `<table style="width:100%;font-size:11px;border-collapse:collapse">
    <thead><tr style="text-align:left;color:var(--text-muted)"><th>文件</th><th>修改次数</th><th>最近修改</th></tr></thead>
    <tbody>${hotspots.map(h => `
      <tr>
        <td style="word-break:break-all">${escapeHtml(h.file_path || '')}</td>
        <td>${h.mod_count || 0}</td>
        <td>${h.last_modified ? escapeHtml(String(h.last_modified).substring(0, 19)) : '—'}</td>
      </tr>`).join('')}
    </tbody></table>`;
}

async function loadNorms(projectId) {
  const list = $('norm-list');
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/knowledge/norms');
    if (!resp.ok) throw new Error('fetch failed');
    const data = await resp.json();
    renderNormList(data.norms || data || []);
  } catch {
    list.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">加载失败</p>';
  }
}

function renderNormList(norms) {
  const list = $('norm-list');
  if (!norms.length) {
    list.innerHTML = '<div class="empty-state" style="padding:24px"><p>暂无 Harness 规则</p></div>';
    return;
  }
  list.innerHTML = norms.map(n => {
    const active = n.is_active !== false;
    const editing = normEditingId === String(n.id);
    if (editing) {
      return `
        <div class="card" id="norm-${n.id}" style="padding:14px">
          <h4 style="margin:0 0 10px;font-size:14px">编辑规则 #${n.id}</h4>
          <div class="form-group"><label class="form-label">标题</label><input id="edit-norm-title-${n.id}" class="form-input" value="${escapeHtml(n.title || '')}"></div>
          <div class="form-group"><label class="form-label">内容</label><textarea id="edit-norm-content-${n.id}" class="form-textarea" rows="4">${escapeHtml(n.content || '')}</textarea></div>
          <div class="form-row">
            <div class="form-group"><label class="form-label">标签</label>
              <select id="edit-norm-tag-${n.id}" class="form-select">
                ${['harness','convention','heuristic','preference'].map(t => `<option value="${t}" ${n.tag===t?'selected':''}>${t}</option>`).join('')}
              </select>
            </div>
            <div class="form-group"><label class="form-label">优先级</label><input id="edit-norm-priority-${n.id}" type="number" min="1" max="10" class="form-input" value="${n.priority ?? 5}"></div>
          </div>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="btn btn-ghost btn-sm" onclick="cancelEditNorm()">取消</button>
            <button class="btn btn-primary btn-sm" onclick="saveEditNorm('${n.id}')">保存</button>
          </div>
        </div>`;
    }
    return `
    <div class="card" id="norm-${n.id}">
      <div class="card-head">
        <h4 class="card-title">${escapeHtml(n.title || '')}</h4>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <span class="tag tag-${n.tag || 'harness'}">${escapeHtml(n.tag || 'harness')}</span>
          <span class="pill pill-gray">P${n.priority ?? 5}</span>
          <button class="btn btn-ghost btn-sm" onclick="startEditNorm('${n.id}')">编辑</button>
          <button class="btn btn-ghost btn-sm" onclick="toggleNorm('${n.id}', ${!active})">${active ? '禁用' : '启用'}</button>
          <button class="btn btn-danger btn-sm" onclick="deleteNorm('${n.id}')">删</button>
        </div>
      </div>
      <div class="card-body">${escapeHtml(n.content || '')}</div>
    </div>`;
  }).join('');
}

let normEditingId = null;

function startEditNorm(normId) {
  normEditingId = String(normId);
  loadNorms(selectedProjectId);
}

function cancelEditNorm() {
  normEditingId = null;
  loadNorms(selectedProjectId);
}

async function saveEditNorm(normId) {
  const title = $(`edit-norm-title-${normId}`)?.value.trim();
  const content = $(`edit-norm-content-${normId}`)?.value.trim();
  if (!title || !content) { showToast('标题和内容不能为空', 'warning'); return; }
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/norms/' + encodeURIComponent(normId), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title,
        content,
        tag: $(`edit-norm-tag-${normId}`)?.value,
        priority: parseInt($(`edit-norm-priority-${normId}`)?.value, 10) || 5,
      }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    showToast('已保存', 'success');
    normEditingId = null;
    loadNorms(selectedProjectId);
  } catch (e) {
    showToast(e.message, 'error');
  }
}

function toggleAddNormForm() {
  $('add-norm-form').classList.toggle('hidden');
}

async function submitAddNorm() {
  const title = $('norm-title').value.trim();
  const content = $('norm-content').value.trim();
  if (!title || !content) { showToast('请填写标题和内容', 'warning'); return; }
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/norms', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title, content,
        tag: $('norm-tag').value,
        priority: parseInt($('norm-priority').value, 10) || 5,
      }),
    });
    if (!resp.ok) throw new Error('提交失败');
    showToast('已添加', 'success');
    toggleAddNormForm();
    loadNorms(selectedProjectId);
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function toggleNorm(normId, enabled) {
  await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/norms/' + encodeURIComponent(normId), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ is_active: enabled }),
  });
  loadNorms(selectedProjectId);
}

async function deleteNorm(normId) {
  if (!confirm('确定删除？')) return;
  await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/norms/' + encodeURIComponent(normId), { method: 'DELETE' });
  loadNorms(selectedProjectId);
}

// ─── Memory ──────────────────────────────────────────────────
function loadAllMemories(projectId) {
  loadProfile(projectId);
  loadMistakes(projectId);
  loadSuccesses(projectId);
  loadSummaries(projectId);
}

function _profileSplitList(text) {
  return (text || '').split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
}

function _profileJoinList(items) {
  if (!Array.isArray(items)) return '';
  return items.filter(Boolean).join('\n');
}

function _profileSplitCsv(text) {
  return (text || '').split(',').map(function (s) { return s.trim(); }).filter(Boolean);
}

function _profileJoinCsv(items) {
  if (!Array.isArray(items)) return '';
  return items.filter(Boolean).join(', ');
}

function profileJsonToForm(profile) {
  profile = profile || {};
  var identity = profile.identity || {};
  var workflow = profile.workflow || {};
  var prefs = profile.preferences || {};
  var tech = profile.tech_stack || {};
  var qb = profile.quality_bar || {};

  var setVal = function (id, val) { var el = $(id); if (el) el.value = val || ''; };
  var setChk = function (id, val) { var el = $(id); if (el) el.checked = !!val; };

  setVal('profile-identity-name', identity.display_name);
  setVal('profile-identity-role', identity.role);
  setVal('profile-responsibilities', workflow.responsibilities || profile.responsibilities || '');
  setChk('profile-wf-review', workflow.review_before_apply);
  setChk('profile-wf-incremental', workflow.prefer_incremental_changes);
  setChk('profile-wf-parallel', workflow.parallel_subtasks);
  setVal('profile-wf-merge-conflict', workflow.on_merge_conflict);
  setVal('profile-wf-test-failure', workflow.on_test_failure);
  setVal('profile-pref-language', prefs.language);
  setVal('profile-pref-test-fw', prefs.test_framework);
  setVal('profile-pref-coding-style', prefs.coding_style);
  setVal('profile-pref-diff-scope', prefs.diff_scope);
  setVal('profile-pref-commit-style', prefs.commit_message_style);
  setVal('profile-comm-response-lang', prefs.response_language);
  var density = $('profile-comm-comment-density');
  if (density) density.value = prefs.comment_density || 'minimal';
  setChk('profile-qb-tests', qb.require_tests_for_logic_changes);
  setChk('profile-qb-lint', qb.lint_before_commit);
  setChk('profile-qb-secrets', qb.no_secrets_in_code);
  setVal('profile-tech-backend', _profileJoinCsv(tech.backend));
  setVal('profile-tech-frontend', _profileJoinCsv(tech.frontend));
  setVal('profile-tech-database', _profileJoinCsv(tech.database));
  setVal('profile-tech-infra', _profileJoinCsv(tech.infra));
  setVal('profile-instructions-brain', _profileJoinList(profile.instructions_for_brain));
  setVal('profile-instructions-worker', _profileJoinList(profile.instructions_for_worker));
  setVal('profile-notes', typeof profile.notes === 'string' ? profile.notes : '');

  var jsonEl = $('profile-json');
  if (jsonEl) {
    jsonEl.value = Object.keys(profile).length ? JSON.stringify(profile, null, 2) : '';
  }
}

function profileFormToJson() {
  var getVal = function (id) { var el = $(id); return el ? el.value.trim() : ''; };
  var getChk = function (id) { var el = $(id); return el ? el.checked : false; };

  var profile = { version: 1 };
  var name = getVal('profile-identity-name');
  var role = getVal('profile-identity-role');
  if (name || role) {
    profile.identity = { display_name: name || undefined, role: role || undefined };
  }

  var workflow = {};
  var resp = getVal('profile-responsibilities');
  if (resp) workflow.responsibilities = resp;
  if (getChk('profile-wf-review')) workflow.review_before_apply = true;
  if (getChk('profile-wf-incremental')) workflow.prefer_incremental_changes = true;
  if (getChk('profile-wf-parallel')) workflow.parallel_subtasks = true;
  var mergeConflict = getVal('profile-wf-merge-conflict');
  if (mergeConflict) workflow.on_merge_conflict = mergeConflict;
  var testFailure = getVal('profile-wf-test-failure');
  if (testFailure) workflow.on_test_failure = testFailure;
  if (Object.keys(workflow).length) profile.workflow = workflow;

  var prefs = {};
  var lang = getVal('profile-pref-language');
  if (lang) prefs.language = lang;
  var testFw = getVal('profile-pref-test-fw');
  if (testFw) prefs.test_framework = testFw;
  var codingStyle = getVal('profile-pref-coding-style');
  if (codingStyle) prefs.coding_style = codingStyle;
  var diffScope = getVal('profile-pref-diff-scope');
  if (diffScope) prefs.diff_scope = diffScope;
  var commitStyle = getVal('profile-pref-commit-style');
  if (commitStyle) prefs.commit_message_style = commitStyle;
  var responseLang = getVal('profile-comm-response-lang');
  if (responseLang) prefs.response_language = responseLang;
  var densityEl = $('profile-comm-comment-density');
  if (densityEl && densityEl.value) prefs.comment_density = densityEl.value;
  if (Object.keys(prefs).length) profile.preferences = prefs;

  var qb = {};
  if (getChk('profile-qb-tests')) qb.require_tests_for_logic_changes = true;
  if (getChk('profile-qb-lint')) qb.lint_before_commit = true;
  if (getChk('profile-qb-secrets')) qb.no_secrets_in_code = true;
  if (Object.keys(qb).length) profile.quality_bar = qb;

  var tech = {};
  var backend = _profileSplitCsv(getVal('profile-tech-backend'));
  if (backend.length) tech.backend = backend;
  var frontend = _profileSplitCsv(getVal('profile-tech-frontend'));
  if (frontend.length) tech.frontend = frontend;
  var database = _profileSplitCsv(getVal('profile-tech-database'));
  if (database.length) tech.database = database;
  var infra = _profileSplitCsv(getVal('profile-tech-infra'));
  if (infra.length) tech.infra = infra;
  if (Object.keys(tech).length) profile.tech_stack = tech;

  var brainIns = _profileSplitList(getVal('profile-instructions-brain'));
  if (brainIns.length) profile.instructions_for_brain = brainIns;
  var workerIns = _profileSplitList(getVal('profile-instructions-worker'));
  if (workerIns.length) profile.instructions_for_worker = workerIns;

  var notes = getVal('profile-notes');
  if (notes) profile.notes = notes;

  return profile;
}

function toggleProfileAdvanced() {
  var toggle = $('profile-advanced-toggle');
  var structured = $('profile-form-structured');
  var advanced = $('profile-form-advanced');
  if (!toggle || !structured || !advanced) return;
  var isAdvanced = toggle.checked;
  if (isAdvanced) {
    var profile = profileFormToJson();
    var jsonEl = $('profile-json');
    if (jsonEl) jsonEl.value = JSON.stringify(profile, null, 2);
    structured.classList.add('hidden');
    advanced.classList.remove('hidden');
  } else {
    var jsonEl = $('profile-json');
    if (jsonEl && jsonEl.value.trim()) {
      try {
        profileJsonToForm(JSON.parse(jsonEl.value.trim()));
      } catch (e) {
        showToast('JSON 无效，无法切回表单: ' + e.message, 'error');
        toggle.checked = true;
        return;
      }
    }
    advanced.classList.add('hidden');
    structured.classList.remove('hidden');
  }
}

async function loadProfile(projectId) {
  if (!projectId) return;
  if (!getAuthToken()) return;
  try {
    var resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/memories/profile');
    if (resp.status === 401) return;
    if (resp.status === 403) {
      profileJsonToForm({});
      showToast('无权限访问用户画像，请联系项目管理员添加成员', 'warning');
      return;
    }
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    var data = await resp.json();
    var profile = data.profile_json || {};
    profileJsonToForm(profile);
  } catch (e) {
    profileJsonToForm({});
    showToast('用户画像加载失败: ' + (e.message || e), 'error');
  }
}

async function saveProfile() {
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  var toggle = $('profile-advanced-toggle');
  var profileJson = {};
  if (toggle && toggle.checked) {
    var el = $('profile-json');
    if (!el) return;
    var raw = el.value.trim();
    if (raw) {
      try {
        profileJson = JSON.parse(raw);
        if (typeof profileJson !== 'object' || profileJson === null || Array.isArray(profileJson)) {
          throw new Error('必须是 JSON 对象');
        }
      } catch (e) {
        showToast('JSON 格式无效: ' + e.message, 'error');
        return;
      }
    }
  } else {
    profileJson = profileFormToJson();
  }
  try {
    var resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/memories/profile', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile_json: profileJson }),
    });
    if (!resp.ok) throw new Error('保存失败');
    showToast('用户画像已保存', 'success');
    await loadProfile(selectedProjectId);
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function loadMistakes(projectId) {
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/memories/mistakes');
    const data = await resp.json();
    renderMistakeList(data.mistakes || data || []);
  } catch {
    $('mistake-list').innerHTML = '<p style="font-size:12px;color:var(--text-muted)">加载失败</p>';
  }
}

function renderMistakeList(mistakes) {
  const list = $('mistake-list');
  if (!mistakes.length) {
    list.innerHTML = '<p style="font-size:12px;color:var(--text-muted);padding:8px">暂无错题</p>';
    return;
  }
  list.innerHTML = mistakes.map(m => `
    <div class="card">
      <div class="card-head">
        <h4 class="card-title">${escapeHtml(m.error_type || '错误')}</h4>
        <button class="btn btn-danger btn-sm" onclick="deleteMistake('${m.id}')">删</button>
      </div>
      <div class="card-body">${escapeHtml(m.description || '')}</div>
    </div>`).join('');
}

function toggleAddMistakeForm() { $('add-mistake-form').classList.toggle('hidden'); }

async function submitAddMistake() {
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/memories/mistakes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        error_type: $('mistake-error-type').value.trim(),
        description: $('mistake-description').value.trim(),
        context: $('mistake-context').value.trim(),
        fix_description: $('mistake-fix').value.trim(),
      }),
    });
    if (!resp.ok) throw new Error('提交失败');
    showToast('已添加', 'success');
    toggleAddMistakeForm();
    loadMistakes(selectedProjectId);
  } catch (e) { showToast(e.message, 'error'); }
}

async function deleteMistake(mid) {
  if (!confirm('确定删除？')) return;
  await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/memories/mistakes/' + encodeURIComponent(mid), { method: 'DELETE' });
  loadMistakes(selectedProjectId);
}

async function loadSuccesses(projectId) {
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/memories/successes');
    const data = await resp.json();
    renderSuccessList(data.successes || data || []);
  } catch {
    $('success-list').innerHTML = '<p style="font-size:12px;color:var(--text-muted)">加载失败</p>';
  }
}

function renderSuccessList(successes) {
  const list = $('success-list');
  if (!successes.length) {
    list.innerHTML = '<p style="font-size:12px;color:var(--text-muted);padding:8px">暂无成功模式</p>';
    return;
  }
  list.innerHTML = successes.map(s => `
    <div class="card">
      <div class="card-head">
        <h4 class="card-title">${escapeHtml(s.pattern_name || '模式')}</h4>
        <button class="btn btn-danger btn-sm" onclick="deleteSuccess('${s.id}')">删</button>
      </div>
      <div class="card-body">${escapeHtml(s.description || '')}</div>
    </div>`).join('');
}

function toggleAddSuccessForm() { $('add-success-form').classList.toggle('hidden'); }

async function submitAddSuccess() {
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/memories/successes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        pattern_name: $('success-pattern-name').value.trim(),
        description: $('success-description').value.trim(),
        approach: $('success-approach').value.trim(),
        applicable_when: $('success-applicable').value.trim(),
      }),
    });
    if (!resp.ok) throw new Error('提交失败');
    showToast('已添加', 'success');
    toggleAddSuccessForm();
    loadSuccesses(selectedProjectId);
  } catch (e) { showToast(e.message, 'error'); }
}

async function deleteSuccess(sid) {
  if (!confirm('确定删除？')) return;
  await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/memories/successes/' + encodeURIComponent(sid), { method: 'DELETE' });
  loadSuccesses(selectedProjectId);
}

async function loadSummaries(projectId) {
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/memories/summaries');
    const data = await resp.json();
    renderSummaryList(data.summaries || data || []);
  } catch {
    $('summary-list').innerHTML = '<p style="font-size:12px;color:var(--text-muted)">加载失败</p>';
  }
}

function renderSummaryList(summaries) {
  const list = $('summary-list');
  if (!summaries.length) {
    list.innerHTML = '<p style="font-size:12px;color:var(--text-muted);padding:8px">暂无任务摘要</p>';
    return;
  }
  list.innerHTML = summaries.map(s => `
    <div class="card">
      <div class="card-head">
        <span class="pill pill-gray">#${escapeHtml(String(s.task_id || '').substring(0, 8))}</span>
        ${s.outcome ? `<span class="pill ${s.outcome === 'success' ? 'pill-green' : 'pill-red'}">${escapeHtml(s.outcome)}</span>` : ''}
      </div>
      <div class="card-body">${escapeHtml(s.summary || '')}</div>
      ${s.lessons_learned ? `<p style="font-size:12px;color:var(--purple);margin:8px 0 0">${escapeHtml(s.lessons_learned)}</p>` : ''}
    </div>`).join('');
}

// ─── Init ────────────────────────────────────────────────────
function init() {
  installAuthFetch();
  renderComponents([]);
  renderProjectList();
  showProjectView(false);
  switchDetailTab('overview');

  refreshCurrentUser().then(function (ok) {
    if (!ok) showLoginModal();
    else if (selectedProjectId && currentTab === 'memory') {
      loadAllMemories(selectedProjectId);
    }
  });

  checkHealth();
  fetchStatus();
  loadConfig();
  loadProjects();
  loadRoutingTable();

  statusInterval = setInterval(() => { fetchStatus(); checkHealth(); }, 5000);
  setInterval(async () => {
    await pollSystemNotifications();
  }, 30000);
  setInterval(() => {
    if (selectedProjectId && currentTab === 'system') {
      refreshSandboxes(selectedProjectId);
    }
  }, 15000);
  setInterval(async () => {
    await loadProjects();
    if (selectedProjectId) {
      renderProjectList();
      const project = projects.find(p => p.id === selectedProjectId);
      if (project) {
        let statsHtml = projectStatusTag(project.status) + ' ' + graphStatusTag(project.graph_status || 'NONE');
        if (project.file_count) statsHtml += `<span class="pill pill-gray">${project.file_count} 文件</span>`;
        if (project.symbol_count) statsHtml += `<span class="pill pill-gray">${project.symbol_count} 符号</span>`;
        $('project-stats').innerHTML = statsHtml;
        loadTasks(selectedProjectId);
        reloadCurrentProjectTab(selectedProjectId);
        if (project.status === 'PREPROCESSING' && !preprocessSSE) connectPreprocessSSE(selectedProjectId);
        else if (project.status !== 'PREPROCESSING') disconnectPreprocessSSE();
      }
    }
  }, 15000);

  $('new-task-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) createTask();
  });

  const loginPassword = $('login-password');
  const loginUsername = $('login-username');
  if (loginPassword) {
    loginPassword.addEventListener('keydown', e => {
      if (e.key === 'Enter') submitLogin();
    });
  }
  if (loginUsername) {
    loginUsername.addEventListener('keydown', e => {
      if (e.key === 'Enter') submitLogin();
    });
  }
}

document.addEventListener('DOMContentLoaded', init);
