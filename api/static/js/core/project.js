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
