/* Swarm Web UI — tabs/tasks module (split from app.js, shared global scope) */
'use strict';

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

async function openNotificationTask(taskId, projectId) {
  dismissNotificationBanner();
  if (projectId && projectId !== selectedProjectId) {
    selectProject(projectId);
  }
  switchTab('tasks');
  await selectTask(taskId);
}
