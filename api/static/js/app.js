/* Swarm v0.3 — Web UI 主入口 (app entry)
 *
 * 业务逻辑已按域拆分到 core/ 与 tabs/ 模块（见 index.html 的 <script> 加载顺序）。
 * 本文件只保留应用启动入口 init()，所有模块共享同一全局作用域
 * （原生多 <script> 顺序加载，无 ES module / 无构建工具）。
 */

'use strict';

// ─── App 启动入口 ─────────────────────────────────────────
function init() {
  installAuthFetch();
  renderComponents([]);
  renderProjectList();
  showProjectView(false);
  switchDetailTab('overview');

  // 先确认登录态，再触发任何 /api 加载与轮询，避免未登录/token 失效时
  // 后台定时器反复打 API 触发 401（并反复弹登录框）。
  refreshCurrentUser().then(function (ok) {
    if (!ok) {
      showLoginModal();
      return;  // 未登录：不启动初始加载，登录成功后由 submitLogin 触发
    }
    startInitialLoad();
    if (selectedProjectId && currentTab === 'memory') {
      loadAllMemories(selectedProjectId);
    }
  });

  startPollers();

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

// 首屏加载（仅在已登录时调用）
function startInitialLoad() {
  checkHealth();
  fetchStatus();
  loadConfig();
  loadProjects();
  loadRoutingTable();
  pollNotificationBell();
}

// 后台轮询：每个 tick 都先检查登录态，未登录直接跳过（防 401 风暴）。
function startPollers() {
  if (pollersStarted) return;
  pollersStarted = true;

  statusInterval = setInterval(() => {
    if (!getAuthToken()) return;
    fetchStatus();
    checkHealth();
  }, 5000);

  setInterval(async () => {
    if (!getAuthToken()) return;
    await pollNotificationBell();
  }, 15000);

  setInterval(() => {
    if (!getAuthToken()) return;
    if (selectedProjectId && currentTab === 'system') {
      refreshSandboxes(selectedProjectId);
    }
  }, 15000);

  setInterval(async () => {
    if (!getAuthToken()) return;
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
}

document.addEventListener('DOMContentLoaded', init);
