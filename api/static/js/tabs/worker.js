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
  workerEventSource = new EventSource(sseUrl(url));

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
  // round27：后端失权断流发 event:end（C6 重鉴权）——无监听器时 EventSource 会对已断权的
  // 端点无限自动重连（每次 403），面板冻在"运行中"无任何提示。收到即关闭并明示。
  workerEventSource.addEventListener('end', () => {
    closeWorkerSSE();
    const statusEl = $('worker-run-status');
    if (statusEl) statusEl.textContent = '连接已断开';
    if (typeof showToast === 'function') showToast('Worker 进度流已断开（权限撤销或会话失效）', 'warning');
  });
  workerEventSource.onmessage = ev => handle(ev, 'progress');
  workerEventSource.onerror = () => {
    /* EventSource 会自动重连；完成时由 complete 事件关闭 */
  };
}

// 置信度 → pill 配色
function confidencePill(conf) {
  const map = { high: ['pill-green', '高 high'], medium: ['pill-amber', '中 medium'], low: ['pill-red', '低 low'] };
  const [cls, label] = map[String(conf || '').toLowerCase()] || ['pill-gray', String(conf || 'unknown')];
  return `<span class="pill ${cls}" title="Worker 自评置信度">置信度 ${label}</span>`;
}

// L1 确定性闸门详情 → 结构化展示（lint 闸门 / 决策来源 / scoped 测试）
function renderL1Details(details) {
  if (!details || typeof details !== 'object' || !Object.keys(details).length) return '';
  const parts = [];
  // 决策来源：deterministic（确定性断言）优于 llm_self_report（LLM 自报）
  const src = details.l1_decision_source;
  if (src) {
    const isDet = src === 'deterministic';
    parts.push(`<span class="pill ${isDet ? 'pill-purple' : 'pill-gray'}" title="L1 通过/失败由谁裁定">判定来源 ${isDet ? '确定性闸门' : 'LLM 自报'}</span>`);
  }
  // lint 确定性闸门
  const lint = details.lint;
  if (lint && typeof lint === 'object') {
    const lintErr = lint.has_error || lint.status === 'error';
    let lintPill = `<span class="pill ${lintErr ? 'pill-red' : 'pill-green'}">lint ${lintErr ? 'error' : 'ok'}</span>`;
    if (lint.gated === true) lintPill += '<span class="pill pill-red" title="lint error 硬阻断了流水线">已阻断 gated</span>';
    else if (lintErr && lint.gated === false) lintPill += '<span class="pill pill-amber" title="lint error 仅告警，未阻断">仅告警</span>';
    parts.push(lintPill);
    const issues = Array.isArray(lint.issues) ? lint.issues : [];
    const errCount = issues.filter(i => i && i.severity === 'error').length;
    if (errCount) parts.push(`<span class="pill pill-gray">${errCount} 个 lint error</span>`);
  }
  // scoped 测试
  if ('l1_3_test_ok' in details) {
    const tOk = details.l1_3_test_ok;
    if (details.test_skipped) parts.push('<span class="pill pill-gray" title="未检测到测试命令">测试 skipped</span>');
    else parts.push(`<span class="pill ${tOk ? 'pill-green' : 'pill-red'}">scoped 测试 ${tOk ? 'pass' : 'fail'}</span>`);
  }
  if ('deterministic_l1' in details) {
    parts.push(`<span class="pill ${details.deterministic_l1 ? 'pill-green' : 'pill-red'}" title="确定性 L1 闸门最终结论">确定性 L1 ${details.deterministic_l1 ? '✓' : '✗'}</span>`);
  }
  let html = parts.length ? `<div class="l1-gate-pills" style="display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 0">${parts.join('')}</div>` : '';
  // test_output 折叠展示（失败时尤其有用）
  const testOut = details.test_output;
  if (testOut && String(testOut).trim()) {
    html += `<details style="margin:8px 0 0"><summary style="cursor:pointer;color:var(--text-muted,#888)">测试/闸门输出</summary><pre style="white-space:pre-wrap;font-size:12px;margin:6px 0 0;max-height:240px;overflow:auto">${escapeHtml(String(testOut))}</pre></details>`;
  }
  return html;
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
  // notes：Worker 自报的"需人工审查项"，审批前必看
  const notesHtml = (result.notes && String(result.notes).trim())
    ? `<div class="worker-notes" style="margin:8px 0 0;padding:8px 10px;border-left:3px solid var(--amber,#d99);background:rgba(217,153,153,0.08);border-radius:4px"><strong>⚠ 需人工审查：</strong>${escapeHtml(String(result.notes))}</div>`
    : '';
  summary.innerHTML = `
    <span class="pill ${success ? 'pill-green' : 'pill-red'}">${success ? '成功' : '失败'}</span>
    <span class="pill pill-gray">${escapeHtml(result.phase || 'done')}</span>
    ${confidencePill(result.confidence)}
    ${result.summary ? '<p style="margin:8px 0 0">' + escapeHtml(result.summary) + '</p>' : ''}
    ${renderL1Details(result.l1_details)}
    ${notesHtml}`;
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
