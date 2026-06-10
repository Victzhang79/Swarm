/* Swarm Web UI — tabs/preprocess module (split from app.js, shared global scope) */
'use strict';

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

function resetPipeline() {
  PIPELINE_NODES.forEach(n => {
    const step = document.querySelector(`.pipeline-step[data-node="${n}"]`);
    if (step) step.className = 'pipeline-step pending';
  });
  $('knowledge-banner').classList.add('hidden');
  $('learn-notice').classList.add('hidden');
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
