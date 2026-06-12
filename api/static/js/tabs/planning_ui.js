/* Swarm Web UI — tabs/planning_ui：Q4 交互式渐进规划的人机交互层
   澄清多轮问答 / 技术方案评审 / 规划过程回看。
   复用全局 appendLog / showToast / sseUrl / escapeHtml / $。 */
'use strict';

// 在任务详情日志区上方插入一个交互卡片容器（若无则创建）。
function _planningSlot() {
  let slot = document.getElementById('planning-interaction-slot');
  if (!slot) {
    slot = document.createElement('div');
    slot.id = 'planning-interaction-slot';
    slot.style.cssText = 'margin:10px 0';
    // 优先插到任务日志面板前；否则插到主区域
    const logPanel = document.getElementById('task-log') || document.getElementById('task-detail') || document.body;
    logPanel.parentNode ? logPanel.parentNode.insertBefore(slot, logPanel) : logPanel.appendChild(slot);
  }
  return slot;
}

function _clearPlanningSlot() {
  const slot = document.getElementById('planning-interaction-slot');
  if (slot) slot.innerHTML = '';
}

// ── 澄清多轮问答 ──
function renderClarifyPrompt(taskId, interrupt) {
  const slot = _planningSlot();
  const questions = interrupt.questions || [];
  const round = interrupt.round || 1;
  const maxR = interrupt.max_rounds || 5;
  if (!questions.length) { _clearPlanningSlot(); return; }

  const qHtml = questions.map((q, i) => `
    <div style="margin-bottom:10px">
      <label class="form-label" style="font-weight:500">${i + 1}. ${escapeHtml(q.q || '')}</label>
      <div style="font-size:11px;color:var(--text-muted);margin:2px 0">${escapeHtml(q.why || '')}　
        <span style="color:var(--text-secondary)">（跳过则默认：${escapeHtml(q.default_if_skipped || '无')}）</span></div>
      <input class="form-input clarify-answer" data-qidx="${i}" placeholder="你的回答（可留空用默认）">
    </div>`).join('');

  slot.innerHTML = `
    <div class="card" style="padding:14px;border-left:3px solid var(--orange)">
      <h3 style="font-size:14px;margin:0 0 4px">💬 需求澄清 <span class="hint">第 ${round}/${maxR} 轮 · 云端大模型启发式提问</span></h3>
      <p style="font-size:12px;color:var(--text-muted);margin:0 0 12px">${escapeHtml(interrupt.message || '请回答以下问题以便精确规划')}</p>
      ${qHtml}
      <div style="display:flex;gap:8px;margin-top:8px">
        <button class="btn btn-primary btn-sm" onclick="submitClarify('${taskId}')">提交答复</button>
        <button class="btn btn-ghost btn-sm" onclick="skipClarify('${taskId}')">整体跳过（用默认假设）</button>
      </div>
    </div>`;
}

async function submitClarify(taskId) {
  const answers = {};
  document.querySelectorAll('#planning-interaction-slot .clarify-answer').forEach(inp => {
    answers[inp.dataset.qidx] = inp.value || '';
  });
  await _postPlanning(taskId, '/clarify', { answers }, '澄清已提交');
}

async function skipClarify(taskId) {
  await _postPlanning(taskId, '/clarify', { action: 'skip' }, '已跳过澄清，采用默认假设');
}

// ── 技术方案评审 ──
function renderDesignReviewPrompt(taskId, interrupt) {
  const slot = _planningSlot();
  const td = interrupt.tech_design || {};
  const stack = td.stack || {};
  const fmtList = (arr) => (arr || []).map(x => `<li>${escapeHtml(String(x))}</li>`).join('') || '<li class="hint">（无）</li>';

  slot.innerHTML = `
    <div class="card" style="padding:14px;border-left:3px solid var(--accent, #6ea8fe)">
      <h3 style="font-size:14px;margin:0 0 8px">📐 技术方案评审 <span class="hint">通过后进入任务拆解</span></h3>
      <div style="font-size:12px;line-height:1.7;max-height:340px;overflow:auto;background:var(--bg-elevated);padding:10px;border-radius:6px;border:1px solid var(--border-subtle)">
        <b>技术栈</b>：前端 ${escapeHtml(stack.frontend || '-')}｜后端 ${escapeHtml(stack.backend || '-')}｜存储 ${escapeHtml(stack.storage || '-')}<br>
        <span class="hint">${escapeHtml(stack.rationale || '')}</span>
        <div style="margin-top:8px"><b>架构</b><br>${escapeHtml(td.architecture || '-')}</div>
        <div style="margin-top:8px"><b>数据模型</b><br><pre style="white-space:pre-wrap;font-size:11px">${escapeHtml(td.data_model_diagram || '-')}</pre></div>
        <div style="margin-top:8px"><b>业务流程</b><br><pre style="white-space:pre-wrap;font-size:11px">${escapeHtml(td.flow_diagram || '-')}</pre></div>
        <div style="margin-top:8px"><b>风险</b><ul style="margin:4px 0 0;padding-left:18px">${fmtList(td.risks)}</ul></div>
        <div style="margin-top:8px"><b>注意事项</b><ul style="margin:4px 0 0;padding-left:18px">${fmtList(td.notes)}</ul></div>
        <div style="margin-top:8px"><b>验收标准</b><ul style="margin:4px 0 0;padding-left:18px">${fmtList(td.acceptance)}</ul></div>
        ${td.change_impact ? `<div style="margin-top:8px"><b>变更影响</b><br>${escapeHtml(td.change_impact)}</div>` : ''}
        ${td.maintainability ? `<div style="margin-top:8px"><b>可维护性</b><br>${escapeHtml(td.maintainability)}</div>` : ''}
      </div>
      <textarea id="design-review-feedback" class="form-input" style="margin-top:10px;min-height:60px" placeholder="打回时填写修改意见（通过可留空）"></textarea>
      <div style="display:flex;gap:8px;margin-top:8px">
        <button class="btn btn-primary btn-sm" onclick="approveDesign('${taskId}')">✓ 通过，进入拆解</button>
        <button class="btn btn-danger btn-sm" onclick="rejectDesign('${taskId}')">✗ 打回重做</button>
      </div>
    </div>`;
}

async function approveDesign(taskId) {
  const fb = (document.getElementById('design-review-feedback') || {}).value || '';
  await _postPlanning(taskId, '/review-design', { decision: 'approve', feedback: fb }, '方案已通过');
}

async function rejectDesign(taskId) {
  const fb = (document.getElementById('design-review-feedback') || {}).value || '';
  if (!fb.trim()) { showToast('打回需填写修改意见', 'warning'); return; }
  await _postPlanning(taskId, '/review-design', { decision: 'reject', feedback: fb }, '方案已打回，将重做');
}

async function _postPlanning(taskId, path, body, okMsg) {
  try {
    const resp = await fetch('/api/tasks/' + encodeURIComponent(taskId) + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'HTTP ' + resp.status);
    showToast(okMsg, 'success');
    _clearPlanningSlot();
    if (typeof appendLog === 'function') appendLog('info', okMsg + '，规划继续…');
  } catch (e) {
    showToast('提交失败: ' + e.message, 'error');
  }
}

// ── 规划过程回看（任务详情）──
async function loadPlanningArtifacts(taskId) {
  try {
    const data = await fetch('/api/tasks/' + encodeURIComponent(taskId) + '/planning').then(r => r.json());
    const p = data.planning || {};
    let box = document.getElementById('planning-artifacts-box');
    if (!box) {
      const host = document.getElementById('task-detail-content') || document.getElementById('task-detail');
      if (!host) return;
      box = document.createElement('div');
      box.id = 'planning-artifacts-box';
      host.appendChild(box);
    }
    if (!p.clarify_history && !p.tech_design) { box.innerHTML = ''; return; }
    const ch = (p.clarify_history || []).map(h =>
      `<div style="margin-bottom:4px"><b>第${h.round}轮</b>：${(h.questions || []).map((q, i) =>
        `${escapeHtml(q.q || '')} → ${escapeHtml((h.answers || {})[i] || (h.answers || {})[String(i)] || '(默认)')}`).join('；')}</div>`
    ).join('');
    const td = p.tech_design || {};
    box.innerHTML = `
      <details style="margin-top:10px"><summary style="cursor:pointer;font-size:13px;font-weight:500">📋 规划过程回看</summary>
        <div style="font-size:12px;padding:8px;background:var(--bg-elevated);border-radius:6px;margin-top:6px">
          ${ch ? `<div><b>澄清问答</b>${ch}</div>` : ''}
          ${td.architecture ? `<div style="margin-top:8px"><b>技术方案</b>：${escapeHtml(td.architecture)}</div>` : ''}
          ${p.assessed_complexity ? `<div style="margin-top:4px" class="hint">澄清后定级：${escapeHtml(p.assessed_complexity)}</div>` : ''}
          ${p.design_review ? `<div class="hint">评审：${escapeHtml(p.design_review.decision || '')}（打回 ${p.design_review.reject_count || 0} 次）</div>` : ''}
        </div>
      </details>`;
  } catch { /* ignore */ }
}
