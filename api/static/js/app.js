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
