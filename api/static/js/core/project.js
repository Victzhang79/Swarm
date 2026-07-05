/* Swarm Web UI — core/project module (split from app.js, shared global scope) */
'use strict';

function projectStatusTag(status) {
  const map = {
    READY: { cls: 'pill-green', label: 'READY' },
    PREPROCESSING: { cls: 'pill-blue', label: 'PREPROCESSING' },
    ERROR: { cls: 'pill-red', label: 'ERROR' },
  };
  const s = map[status] || { cls: 'pill-gray', label: status || 'UNKNOWN' };
  return `<span class="pill ${s.cls}">${s.label}</span>`;
}

function showAddProjectModal() {
  $('add-project-overlay').classList.add('open');
  $('add-project-modal').classList.add('open');
  $('add-project-path').value = '';
  $('add-project-name').value = '';
  // 重置为"导入现有"模式
  const importRadio = document.querySelector('input[name="add-project-mode"][value="import"]');
  if (importRadio) importRadio.checked = true;
  onAddProjectModeChange();
  $('add-project-path').focus();
}

function hideAddProjectModal() {
  $('add-project-overlay').classList.remove('open');
  $('add-project-modal').classList.remove('open');
}

function getAddProjectMode() {
  const checked = document.querySelector('input[name="add-project-mode"]:checked');
  return checked ? checked.value : 'import';
}

function onAddProjectModeChange() {
  const greenfield = getAddProjectMode() === 'greenfield';
  const label = $('add-project-path-label');
  const pathInput = $('add-project-path');
  const hint = $('add-project-greenfield-hint');
  if (label) label.textContent = greenfield ? '项目路径（可留空，自动创建）' : '项目路径（绝对路径）';
  if (pathInput) pathInput.placeholder = greenfield ? '留空则自动在 workdir/<项目名> 下创建' : '/path/to/your/project';
  if (hint) hint.classList.toggle('hidden', !greenfield);
}

function submitAddProjectFromModal() {
  const greenfield = getAddProjectMode() === 'greenfield';
  const path = $('add-project-path').value.trim();
  if (!greenfield && !path) { showToast('请输入项目路径', 'warning'); return; }
  let name = $('add-project-name').value.trim();
  if (!name) name = path.split('/').pop() || (greenfield ? '' : 'New Project');
  if (greenfield && !name) { showToast('从零创建请填写项目名称', 'warning'); return; }
  hideAddProjectModal();
  submitAddProject(name, path, greenfield);
}

// ─── Revise Modal ──────────────────────────────────────────

function reloadCurrentProjectTab(projectId) {
  if (!projectId) return;
  // 上层在系统级 tab 时，刷新系统面板（不再由下层 currentTab 驱动）
  if (typeof currentTopTab !== 'undefined' && currentTopTab === 'system') {
    refreshSandboxes(projectId);
    loadSystemStats();
    fetchStatus();
    return;
  }
  if (typeof currentTopTab !== 'undefined' && currentTopTab === 'observability') {
    return;  // 可观测全局，不随项目刷新
  }
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
  } else if (currentTab === 'stats') {
    // 下层项目级统计 tab：当前项目任务统计 + 项目关联沙箱
    loadSystemStats();
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

// A2 治本：登录后即显示应用外壳（含系统级导航栏），让"系统/设置"等系统级 tab
// 不再依赖"先选中一个项目"。系统级功能(用户管理/沙箱/系统)本就独立于项目。
// project-view 容器登录后常驻显示；未选项目时仅 workspace tab 内显示占位提示。
function showAppShell() {
  const pv = $('project-view');
  if (pv) {
    pv.classList.remove('hidden');
    pv.style.display = 'flex';
  }
  // 项目栏(标题/预处理/删除)依赖选中项目，未选时隐藏
  applyProjectBarVisibility();
  // workspace 区的"选择项目"占位：仅当前在 workspace tab 且未选项目时显示
  applyNoProjectPlaceholder();
}

// 项目栏(project-bar)仅在选中项目时有意义
function applyProjectBarVisibility() {
  const bar = document.querySelector('#project-view .project-bar');
  if (bar) bar.style.display = selectedProjectId ? '' : 'none';
}

// "选择或创建项目"占位：仅在 workspace 顶tab + 未选项目时显示；
// 系统级 tab(沙箱/系统/设置)下永远不显示（它们不需要项目）。
function applyNoProjectPlaceholder() {
  const np = $('no-project-view');
  if (!np) return;
  const onWorkspace = (typeof currentTopTab === 'undefined') || currentTopTab === 'workspace';
  const show = onWorkspace && !selectedProjectId;
  np.classList.toggle('hidden', !show);
}

function showProjectView(show) {
  // 选中项目后：确保外壳显示 + 项目栏出现 + 隐藏占位 + 子导航可用
  const pv = $('project-view');
  if (pv) {
    pv.classList.remove('hidden');
    pv.style.display = 'flex';
  }
  applyProjectBarVisibility();
  applyNoProjectPlaceholder();
}

// ─── Projects ──────────────────────────────────────────────

async function loadProjects() {
  try {
    const resp = await fetch('/api/projects');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    projects = Array.isArray(data) ? data : (data.projects || []);
    renderProjectList();
    restoreSelectedProject();
  } catch (e) {
    projects = [];
    renderProjectList();
    // 断链/后端故障显式提示——否则空项目列表被误当"暂无项目"。401 由全局登录框处理，跳过。
    if (!/HTTP 401/.test(String(e && e.message)) && typeof showToast === 'function') {
      showToast('项目列表加载失败（后端不可达或异常）', 'error');
    }
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

async function submitAddProject(name, path, greenfield) {
  try {
    const resp = await fetch('/api/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, path, greenfield: !!greenfield }),
    });
    if (!resp.ok) {
      const err = await resp.text();
      throw new Error(err);
    }
    showToast(greenfield ? '空项目已创建，可直接发起任务' : '项目已添加，预处理启动中', 'success');
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
  const project = projects.find(p => p.id === selectedProjectId);
  const pname = project ? project.name : selectedProjectId;
  // 二次确认防误删：查该项目任务数，明确告知将级联删除的内容（任务硬删不可恢复）
  let taskCount = 0;
  try {
    const r = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/tasks');
    if (r.ok) {
      const d = await r.json();
      taskCount = (d.tasks || d || []).length || 0;
    }
  } catch (e) { /* 查不到任务数不阻断，仍走确认 */ }
  const warn = `确定删除项目「${pname}」？\n\n` +
    `这将级联取消运行中任务并永久删除该项目下的 ${taskCount} 个任务记录（硬删除，不可恢复）。\n` +
    `审计日志会保留删除留痕，但任务内容无法找回。`;
  if (!confirm(warn)) return;
  // 有任务时再要求一次显式确认，进一步防误删
  if (taskCount > 0 && !confirm(`再次确认：真的要删除「${pname}」及其 ${taskCount} 个任务？`)) return;
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
