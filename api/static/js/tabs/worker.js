/* Swarm Web UI — tabs/worker module (split from app.js, shared global scope) */
'use strict';

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
