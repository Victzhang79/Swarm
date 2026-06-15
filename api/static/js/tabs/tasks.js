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
  // 需求池任务（B.5）：显示「执行」按钮
  if (t.status === 'POOLED') {
    html += `<button class="btn btn-primary btn-sm" style="${btnStyle}" onclick="event.stopPropagation();executePooledTask('${t.id}')" title="从需求池执行">▶ 执行</button>`;
  }
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
  restorePipelineFromStatus(task);

  // Q4：规划过程回看（澄清/技术方案/评审）
  if (typeof loadPlanningArtifacts === 'function' && task.id) {
    loadPlanningArtifacts(task.id);
  }

  if (task.learn_summary) {
    tryShowLearnNotice(typeof task.learn_summary === 'string' ? JSON.parse(task.learn_summary) : task.learn_summary);
  }

  const actionsEl = $('detail-actions');
  if (actionsEl) {
    actionsEl.innerHTML =
      `<button class="btn btn-secondary btn-sm" onclick="viewTaskLogs('${task.id}')" title="查看该任务的执行日志">📜 日志</button>`
      + renderTaskActions(task, false);
  }
}

// ── 任务执行日志查看（SSE 实时流）──────────────────
let _taskLogsES = null;
async function viewTaskLogs(taskId) {
  let overlay = document.getElementById('task-logs-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'task-logs-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1000;display:flex;align-items:center;justify-content:center';
    overlay.innerHTML = `
      <div style="background:var(--bg-primary,#1e1e1e);border:1px solid var(--border,#333);border-radius:8px;width:min(900px,92vw);height:min(640px,86vh);display:flex;flex-direction:column">
        <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border,#333)">
          <strong id="task-logs-title">任务日志</strong>
          <div style="display:flex;align-items:center;gap:8px">
            <span id="task-logs-live" style="font-size:11px;color:var(--text-muted,#888)"></span>
            <label style="font-size:11px;display:flex;align-items:center;gap:4px;cursor:pointer"><input type="checkbox" id="task-logs-follow" checked>跟随</label>
            <button class="btn btn-ghost btn-sm" id="task-logs-close" title="关闭">✕</button>
          </div>
        </div>
        <pre id="task-logs-body" style="flex:1;overflow:auto;margin:0;padding:12px 16px;font-size:12px;line-height:1.5;white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,Menlo,monospace"></pre>
      </div>`;
    document.body.appendChild(overlay);
    const close = () => { _closeTaskLogsStream(); overlay.remove(); };
    overlay.querySelector('#task-logs-close').onclick = close;
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  }
  overlay.querySelector('#task-logs-title').textContent = '任务日志 #' + String(taskId).substring(0, 8);
  const body = overlay.querySelector('#task-logs-body');
  const liveEl = overlay.querySelector('#task-logs-live');
  const followEl = overlay.querySelector('#task-logs-follow');
  body.textContent = '';

  _closeTaskLogsStream();
  let gotAny = false;
  const appendLine = (line) => {
    gotAny = true;
    body.textContent += (body.textContent ? '\n' : '') + line;
    if (followEl.checked) body.scrollTop = body.scrollHeight;
  };

  // 优先 SSE 实时流；失败则回退到一次性拉取
  try {
    const es = new EventSource(sseUrl('/api/tasks/' + encodeURIComponent(taskId) + '/logs/stream'));
    _taskLogsES = es;
    liveEl.textContent = '● 实时';
    liveEl.style.color = '#4ade80';
    es.addEventListener('log', (e) => appendLine(e.data));
    es.addEventListener('end', () => {
      liveEl.textContent = '— 已结束';
      liveEl.style.color = 'var(--text-muted,#888)';
      _closeTaskLogsStream();
      // 终态任务：SSE 仅吐末尾窗口内的实时增量，历史日志（已被新日志挤出
      // tail 窗口 / 滚动到 backup）SSE 吐不出 → 正常 end 但 gotAny=false。
      // 必须回退到一次性 /logs 拉取（后端有全量+轮转 backup 回退），否则历史
      // 终态任务永远显示"暂无日志"。此前只在 onerror 回退，end 路径漏了。
      if (!gotAny) fetchTaskLogsOnce(taskId, body);
    });
    es.onerror = () => {
      // 连接错误：若尚无数据，回退一次性拉取
      liveEl.textContent = '○ 断开';
      liveEl.style.color = 'var(--text-muted,#888)';
      _closeTaskLogsStream();
      if (!gotAny) fetchTaskLogsOnce(taskId, body);
    };
  } catch (e) {
    fetchTaskLogsOnce(taskId, body);
  }
}

function _closeTaskLogsStream() {
  if (_taskLogsES) { try { _taskLogsES.close(); } catch (e) {} _taskLogsES = null; }
}

async function fetchTaskLogsOnce(taskId, body) {
  body.textContent = '加载中…';
  try {
    const resp = await fetch('/api/tasks/' + encodeURIComponent(taskId) + '/logs?limit=1000');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    body.textContent = (data.lines && data.lines.length) ? data.lines.join('\n') : (data.hint || '暂无该任务日志。');
    body.scrollTop = body.scrollHeight;
  } catch (e) {
    body.textContent = '加载日志失败: ' + e.message;
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

// 根据任务当前状态回放 pipeline 进度（选中任意任务时调用，含已完成/失败任务）。
// 解决「已完成任务 7 个状态不亮」「选中进行中任务在 SSE 首个事件前一片灰」的问题。
function restorePipelineFromStatus(task) {
  if (!task || !task.status) { resetPipeline(); return; }
  const status = task.status;

  // 终态 DONE：全部 7 步亮起
  if (TERMINAL_DONE_STATUSES.has(status)) {
    PIPELINE_NODES.forEach(n => {
      const step = document.querySelector(`.pipeline-step[data-node="${n}"]`);
      if (step) step.className = 'pipeline-step done';
    });
    return;
  }

  // 失败/取消：把已走过的阶段标 done，当前阶段标 error，其余 pending
  const curNode = STATUS_TO_PIPELINE_NODE[status];
  const curIdx = curNode ? PIPELINE_NODES.indexOf(curNode) : -1;
  const isFail = TERMINAL_FAIL_STATUSES.has(status);

  PIPELINE_NODES.forEach((n, i) => {
    const step = document.querySelector(`.pipeline-step[data-node="${n}"]`);
    if (!step) return;
    if (curIdx < 0) { step.className = 'pipeline-step pending'; return; }
    if (i < curIdx) step.className = 'pipeline-step done';
    else if (i === curIdx) step.className = isFail ? 'pipeline-step error' : 'pipeline-step running';
    else step.className = 'pipeline-step pending';
  });
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

// ─── B 部分：任务附件上传 ──────────────────────────────────
// 选中文件暂存（待创建任务时一并上传）。
let _pendingTaskFiles = [];

function handleTaskFileSelect(event) {
  _addTaskFiles(event.target.files);
  event.target.value = '';  // 允许重复选同名文件
}

function handleTaskFileDrop(event) {
  if (event.dataTransfer && event.dataTransfer.files) {
    _addTaskFiles(event.dataTransfer.files);
  }
}

const _UPLOAD_ALLOWED = ['.png', '.jpg', '.jpeg', '.webp', '.pdf', '.docx', '.md', '.markdown', '.txt'];
const _UPLOAD_MAX_BYTES = 20 * 1024 * 1024;

function _addTaskFiles(fileList) {
  for (const f of fileList) {
    const ext = '.' + (f.name.split('.').pop() || '').toLowerCase();
    if (!_UPLOAD_ALLOWED.includes(ext)) {
      showToast(`不支持的格式: ${f.name}`, 'warning');
      continue;
    }
    if (f.size > _UPLOAD_MAX_BYTES) {
      showToast(`文件过大(>20MB): ${f.name}`, 'warning');
      continue;
    }
    if (_pendingTaskFiles.length >= 10) {
      showToast('最多 10 个文件', 'warning');
      break;
    }
    _pendingTaskFiles.push(f);
  }
  _renderTaskFileList();
}

function _renderTaskFileList() {
  const el = $('task-upload-list');
  if (!el) return;
  if (!_pendingTaskFiles.length) { el.innerHTML = ''; return; }
  el.innerHTML = _pendingTaskFiles.map((f, i) =>
    `<span style="display:inline-flex;align-items:center;gap:4px;background:var(--bg-subtle,rgba(0,0,0,0.04));border-radius:4px;padding:2px 6px;margin:2px">
       📄 ${escapeHtml(f.name)} (${(f.size / 1024).toFixed(0)}KB)
       <span style="cursor:pointer;color:var(--text-muted)" onclick="_removeTaskFile(${i})">✕</span>
     </span>`
  ).join('');
}

function _removeTaskFile(i) {
  _pendingTaskFiles.splice(i, 1);
  _renderTaskFileList();
}

async function _uploadPendingFiles() {
  if (!_pendingTaskFiles.length) return [];
  const fd = new FormData();
  for (const f of _pendingTaskFiles) fd.append('files', f);
  const resp = await fetch('/api/uploads', { method: 'POST', body: fd });
  if (!resp.ok) throw new Error('文件上传失败: ' + await resp.text());
  const data = await resp.json();
  const errors = (data.files || []).filter(f => f.error);
  if (errors.length) {
    errors.forEach(e => showToast(`${e.filename}: ${e.error}`, 'warning'));
  }
  return (data.files || []).filter(f => f.path).map(f => f.path);
}

async function createTask() {
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  const description = $('new-task-input').value.trim();
  if (!description && !_pendingTaskFiles.length) {
    showToast('请输入任务描述或上传需求文件', 'warning'); return;
  }
  if (!await ensureTaskReadiness()) return;

  $('btn-create-task').disabled = true;
  try {
    clearLogs();
    resetPipeline();
    showTaskDetailPanel();

    // 先上传附件，拿到隔离存储后的路径
    let uploadedPaths = [];
    if (_pendingTaskFiles.length) {
      appendLog('info', `上传 ${_pendingTaskFiles.length} 个附件…`);
      uploadedPaths = await _uploadPendingFiles();
    }

    const pooled = $('task-pooled')?.checked || false;
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        description: description || '（见上传的需求文件）',
        auto_accept: $('task-auto-accept')?.checked || false,
        uploaded_files: uploadedPaths,
        auto_confirm_vision: $('task-auto-confirm-vision')?.checked || false,
        pooled,
      }),
    });
    if (!resp.ok) throw new Error(await resp.text());

    const data = await resp.json();
    const taskId = data.task?.id;
    if (!taskId) throw new Error('未返回 task id');

    // 清空输入
    $('new-task-input').value = '';
    _pendingTaskFiles = [];
    _renderTaskFileList();
    selectedTaskId = taskId;

    if (data.status === 'pooled') {
      showToast('任务已入池（待执行）', 'success');
      await loadTasks(selectedProjectId);
      await selectTask(taskId);
      return;
    }

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

// 需求池：执行 POOLED 任务
async function executePooledTask(taskId) {
  try {
    const resp = await fetch('/api/tasks/' + encodeURIComponent(taskId) + '/execute', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    if (!resp.ok) throw new Error(await resp.text());
    showToast('已从需求池触发执行', 'success');
    await loadTasks(selectedProjectId);
    await selectTask(taskId);
    startTaskSSE(taskId);
  } catch (e) {
    showToast('执行失败: ' + e.message, 'error');
  }
}

function startTaskSSE(taskId) {
  closeTaskSSE();
  selectedTaskId = taskId;

  try {
    const url = '/api/tasks/' + encodeURIComponent(taskId) + '/stream';
    taskEventSource = new EventSource(sseUrl(url));

    const handlePayload = (e, eventType) => {
      try {
        const data = JSON.parse(e.data);
        handleBrainProgressEvent(data, eventType);

        if (eventType === 'awaiting_review' || data.step === 'awaiting_review') {
          const itype = data.interrupt_type || '';
          if (itype === 'clarify') {
            renderClarifyPrompt(taskId, data.interrupt || {});
            appendLog('warning', data.message || '等待需求澄清');
          } else if (itype === 'review_design') {
            renderDesignReviewPrompt(taskId, data.interrupt || {});
            appendLog('warning', data.message || '等待技术方案评审');
          } else {
            appendLog('warning', data.message || '等待人工审核');
            if (selectedProjectId) loadTasks(selectedProjectId).then(() => selectTask(taskId));
          }
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
  // 组件健康常驻顶栏（#components-strip）；兼容旧的系统标签页容器。
  // 设计：平时不显示一排绿灯——只在某组件异常(error/degraded/unknown)时亮灯，
  // 全部正常则显示一个简洁的"系统正常"绿点，让用户一眼看出有没有问题。
  const grid = $('components-strip') || $('components-grid');
  if (!grid) return;
  const map = {};
  (components || []).forEach(c => { map[c.name] = c; });

  const healthy = (s) => s === 'running' || s === 'ready';
  const problems = COMPONENT_DEFS
    .map(def => ({ def, c: map[def.name] || { status: 'unknown', detail: '' } }))
    .filter(({ c }) => !healthy(c.status));

  if (problems.length === 0) {
    // 全部正常：单个绿点 + 文案（仍可悬浮提示"组件全部正常"）
    grid.innerHTML = `
      <div class="component-chip component-chip-ok" data-tip="系统组件全部正常（每 5s 刷新）">
        <span class="component-dot dot-green"></span>
        <span class="component-chip-name">系统正常</span>
      </div>`;
    return;
  }

  grid.innerHTML = problems.map(({ def, c }) => {
    const dotCls = c.status === 'degraded' ? 'dot-amber'
      : c.status === 'error' ? 'dot-red' : 'dot-gray';
    const tip = `${def.name}: ${c.status}${c.detail ? ' · ' + c.detail : ''}`;
    return `
      <div class="component-chip component-chip-bad" data-tip="${escapeHtml(tip)}">
        <span class="component-dot ${dotCls}"></span>
        <span class="component-chip-name">${escapeHtml(def.name)}</span>
      </div>`;
  }).join('');
}

// ─── 点击通知跳转到对应任务 ───────────────────

async function openNotificationTask(taskId, projectId) {
  if (typeof closeNotifPanel === 'function') closeNotifPanel();
  if (projectId && projectId !== selectedProjectId) {
    selectProject(projectId);
  }
  switchTab('tasks');
  await selectTask(taskId);
}
