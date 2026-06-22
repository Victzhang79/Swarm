/* Swarm Web UI — tabs/planning_ui：交互式渐进规划的人机交互层（澄清/虚假前提/方案评审）。
 *
 * 治本重写（不再补丁式累加）：根因是旧实现【无组件、无生命周期、无任务作用域】——slot 被注入
 * 全局日志面板前且从不移除→挡其他任务；渲染从 SSE/选中/重渲染三处触发无单一事实源→竞态清空。
 *
 * 新设计 = 单一控制器 PlanningInteraction：
 *   - 唯一事实源 = GET /api/tasks/{id}/pending（实时快照），渲染是其纯函数；
 *   - 任务作用域：卡片内嵌【任务详情内容区】，记 dataset.taskId；切任务即整体 remove（不挡别的任务）；
 *   - 幂等 + 不打断编辑：同卡片(同 sig)不重建；用户正在填则被动事件不清空；提交后强制收并续进度；
 *   - 异步竞态守卫：/pending 回来时若已不是当前选中任务则丢弃。
 * 复用全局 appendLog / showToast / sseUrl / escapeHtml / $ / selectedTaskId / startTaskSSE / closeTaskSSE。
 */
'use strict';

const PlanningInteraction = (() => {
  const SLOT_ID = 'planning-interaction-slot';
  const WAIT = new Set(['CLARIFYING', 'CONFIRMING', 'DESIGN_REVIEW', 'DELIVERING']);

  function _existing() { return document.getElementById(SLOT_ID); }

  function _ensureSlot() {
    let slot = _existing();
    if (slot) return slot;
    const host = document.getElementById('task-detail-content') || document.getElementById('task-detail');
    if (!host) return null;  // 详情区未就绪：不强插全局，避免挡屏
    slot = document.createElement('div');
    slot.id = SLOT_ID;
    slot.style.cssText = 'margin:10px 0';
    host.insertBefore(slot, host.firstChild);
    return slot;
  }

  function clear() {
    const slot = _existing();
    if (slot) slot.remove();  // 整体移除（不是清空 innerHTML）——杜绝空容器残留挡屏
  }

  // 用户正在填写（焦点在内 / 任一输入有内容）→ 被动事件不得清空或重建，防丢未提交答复。
  function _isEditing(slot) {
    if (!slot) return false;
    if (slot.contains(document.activeElement)) return true;
    return Array.from(slot.querySelectorAll('.clarify-answer,.fact-clarify-answer,#design-review-feedback'))
      .some(i => ((i.value || '').trim().length > 0));
  }

  // 单一入口：按【当前选中任务】同步交互卡片。renderTaskDetail / SSE awaiting_review 都只调它。
  async function syncForTask(task) {
    const slot = _existing();
    // 切任务：先销毁旧任务的卡片（治本：挡其他任务日志）
    if (slot && slot.dataset.taskId && task && slot.dataset.taskId !== task.id) clear();
    if (!task || !task.id || !WAIT.has(String(task.status || '').toUpperCase())) {
      const s = _existing();
      if (s && !_isEditing(s)) clear();
      return;
    }
    let pending = null;
    try {
      const data = await fetch('/api/tasks/' + encodeURIComponent(task.id) + '/pending').then(r => r.json());
      pending = data.pending;
    } catch (e) { return; }  // 拉取失败：保留现状，不破坏正在填的卡片
    // 异步竞态：回来时已切走 → 丢弃
    if (typeof selectedTaskId !== 'undefined' && selectedTaskId && selectedTaskId !== task.id) return;
    if (!pending) {
      const s = _existing();
      if (s && !_isEditing(s)) clear();
      return;
    }
    _render(task.id, pending);
  }

  function _render(taskId, pending) {
    const it = pending.interrupt || {};
    let pair = null;
    if (pending.interrupt_type === 'clarify') pair = _clarify(taskId, it);
    else if (pending.interrupt_type === 'clarify_fact_issue') pair = _factIssue(taskId, it);
    else if (pending.interrupt_type === 'review_design') pair = _designReview(taskId, it);
    else return;  // deliver / confirm_plan：由审核条(updateReviewBar)处理
    if (!pair || !pair[1]) return;
    const [sig, html] = pair;
    const slot = _ensureSlot();
    if (!slot) return;
    // 幂等：同卡片已渲染则不重建（防抢焦点）；正在填写也不重建（防清空）
    if (slot.dataset.sig === sig && slot.firstChild) return;
    if (slot.firstChild && _isEditing(slot)) return;
    slot.dataset.taskId = taskId;
    slot.dataset.sig = sig;
    slot.innerHTML = html;
  }

  // ── 卡片模板（返回 [sig, html]）。统一限高 + flex 纵向：内容滚动、按钮固定可见。 ──
  const _CARD_OPEN = (color) =>
    `<div class="card" style="padding:14px;border-left:3px solid ${color};max-height:72vh;display:flex;flex-direction:column">`;

  function _clarify(taskId, it) {
    const questions = it.questions || [];
    if (!questions.length) return null;
    const round = it.round || 1, maxR = it.max_rounds || 5;
    const sig = 'clarify:' + round + ':' + questions.map(q => q.q || '').join('¶');
    const qHtml = questions.map((q, i) => `
      <div style="margin-bottom:10px">
        <label class="form-label" style="font-weight:500">${i + 1}. ${escapeHtml(q.q || '')}</label>
        <div style="font-size:11px;color:var(--text-muted);margin:2px 0">${escapeHtml(q.why || '')}
          <span style="color:var(--text-secondary)">（跳过则默认：${escapeHtml(q.default_if_skipped || '无')}）</span></div>
        <input class="form-input clarify-answer" data-qidx="${i}" placeholder="你的回答（可留空用默认）">
      </div>`).join('');
    const html = _CARD_OPEN('var(--orange)') +
      `<h3 style="font-size:14px;margin:0 0 4px;flex:0 0 auto">💬 需求澄清 <span class="hint">第 ${round}/${maxR} 轮 · 启发式提问</span></h3>
       <p style="font-size:12px;color:var(--text-muted);margin:0 0 10px;flex:0 0 auto">${escapeHtml(it.message || '请回答以下问题以便精确规划')}</p>
       <div style="flex:1 1 auto;overflow-y:auto;padding-right:6px;min-height:0">${qHtml}</div>
       <div style="display:flex;gap:8px;margin-top:10px;flex:0 0 auto;border-top:1px solid var(--border-subtle,#333);padding-top:10px">
         <button class="btn btn-primary btn-sm" onclick="PlanningInteraction.submitClarify('${taskId}')">提交答复</button>
         <button class="btn btn-ghost btn-sm" onclick="PlanningInteraction.skipClarify('${taskId}')">整体跳过（用默认假设）</button>
       </div></div>`;
    return [sig, html];
  }

  function _factIssue(taskId, it) {
    const q = it.question || it.message || '事实核验检出虚假前提，请确认或修正需求。';
    const sig = 'fact:' + q;
    const html = _CARD_OPEN('var(--red,#e5484d)') +
      `<h3 style="font-size:14px;margin:0 0 6px;flex:0 0 auto">⚠️ 虚假前提澄清 <span class="hint">事实核验拦截 · 需人工确认</span></h3>
       <pre style="font-size:12px;color:var(--text-secondary);white-space:pre-wrap;word-break:break-word;margin:0 0 10px;flex:1 1 auto;overflow:auto;min-height:0">${escapeHtml(q)}</pre>
       <textarea class="form-input fact-clarify-answer" rows="4" style="width:100%;flex:0 0 auto" placeholder="确认或修正需求（例：PRD 实际有 6 种渠道：Slack/企业微信/飞书/语音电话/VoIP/内部推送，配置表只列 4 种是文档不全）"></textarea>
       <div style="display:flex;gap:8px;margin-top:8px;flex:0 0 auto">
         <button class="btn btn-primary btn-sm" onclick="PlanningInteraction.submitFactClarify('${taskId}')">提交澄清，继续规划</button>
         <button class="btn btn-ghost btn-sm" onclick="PlanningInteraction.skipClarify('${taskId}')">跳过（按默认假设继续）</button>
       </div></div>`;
    return [sig, html];
  }

  function _designReview(taskId, it) {
    const td = it.tech_design || {}, stack = td.stack || {};
    const sig = 'design:' + (td.architecture || '') + ':' + (td.data_model_diagram || '').slice(0, 40);
    const fmtList = (arr) => (arr || []).map(x => `<li>${escapeHtml(String(x))}</li>`).join('') || '<li class="hint">（无）</li>';
    const html = _CARD_OPEN('var(--accent,#6ea8fe)') +
      `<h3 style="font-size:14px;margin:0 0 8px;flex:0 0 auto">📐 技术方案评审 <span class="hint">通过后进入任务拆解</span></h3>
       <div style="flex:1 1 auto;overflow:auto;min-height:0;font-size:12px;line-height:1.7;background:var(--bg-elevated);padding:10px;border-radius:6px;border:1px solid var(--border-subtle)">
         <b>技术栈</b>：前端 ${escapeHtml(stack.frontend || '-')}｜后端 ${escapeHtml(stack.backend || '-')}｜存储 ${escapeHtml(stack.storage || '-')}<br>
         <span class="hint">${escapeHtml(stack.rationale || '')}</span>
         <div style="margin-top:8px"><b>架构</b><br>${escapeHtml(td.architecture || '-')}</div>
         <div style="margin-top:8px"><b>数据模型</b><br><pre style="white-space:pre-wrap;font-size:11px">${escapeHtml(td.data_model_diagram || '-')}</pre></div>
         <div style="margin-top:8px"><b>业务流程</b><br><pre style="white-space:pre-wrap;font-size:11px">${escapeHtml(td.flow_diagram || '-')}</pre></div>
         <div style="margin-top:8px"><b>风险</b><ul style="margin:4px 0 0;padding-left:18px">${fmtList(td.risks)}</ul></div>
         <div style="margin-top:8px"><b>验收标准</b><ul style="margin:4px 0 0;padding-left:18px">${fmtList(td.acceptance)}</ul></div>
       </div>
       <textarea id="design-review-feedback" class="form-input" style="margin-top:10px;min-height:56px;flex:0 0 auto" placeholder="打回时填写修改意见（通过可留空）"></textarea>
       <div style="display:flex;gap:8px;margin-top:8px;flex:0 0 auto">
         <button class="btn btn-primary btn-sm" onclick="PlanningInteraction.approveDesign('${taskId}')">✓ 通过，进入拆解</button>
         <button class="btn btn-danger btn-sm" onclick="PlanningInteraction.rejectDesign('${taskId}')">✗ 打回重做</button>
       </div></div>`;
    return [sig, html];
  }

  // ── 提交（统一走 _post：收卡 + 重连 SSE + 回拉 /pending 续进度）──
  async function _post(taskId, path, body, okMsg) {
    try {
      const resp = await fetch('/api/tasks/' + encodeURIComponent(taskId) + path, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || 'HTTP ' + resp.status);
      if (typeof showToast === 'function') showToast(okMsg, 'success');
      clear();  // 提交后整体收卡（用户已显式提交）
      if (typeof appendLog === 'function') appendLog('info', okMsg + '，规划继续…');
      // 任务已 resume → 后端开新一段图执行；前端 SSE 在 interrupt 处已断，重连才看得到进度。
      if (typeof closeTaskSSE === 'function') closeTaskSSE();
      if (typeof startTaskSSE === 'function') startTaskSSE(taskId);
      // 延迟回拉一次：状态可能推进到下一轮澄清/评审或派发 → syncForTask 自动续上。
      setTimeout(() => {
        if (typeof selectedTaskId !== 'undefined' && selectedTaskId !== taskId) return;
        fetch('/api/tasks/' + encodeURIComponent(taskId)).then(r => r.json())
          .then(d => syncForTask(d.task || d)).catch(() => {});
      }, 2500);
    } catch (e) {
      if (typeof showToast === 'function') showToast('提交失败: ' + e.message, 'error');
    }
  }

  function submitClarify(taskId) {
    const answers = {};
    document.querySelectorAll('#' + SLOT_ID + ' .clarify-answer').forEach(inp => { answers[inp.dataset.qidx] = inp.value || ''; });
    return _post(taskId, '/clarify', { answers }, '澄清已提交');
  }
  function skipClarify(taskId) { return _post(taskId, '/clarify', { action: 'skip' }, '已跳过澄清，用默认假设'); }
  function submitFactClarify(taskId) {
    const el = document.querySelector('#' + SLOT_ID + ' .fact-clarify-answer');
    const text = ((el && el.value) || '').trim();
    if (!text) { if (typeof showToast === 'function') showToast('请填写澄清内容，或点"跳过"', 'warning'); return; }
    return _post(taskId, '/clarify', { answers: { '0': text } }, '澄清已提交，规划继续');
  }
  function approveDesign(taskId) {
    const fb = (document.getElementById('design-review-feedback') || {}).value || '';
    return _post(taskId, '/review-design', { decision: 'approve', feedback: fb }, '方案已通过');
  }
  function rejectDesign(taskId) {
    const fb = (document.getElementById('design-review-feedback') || {}).value || '';
    if (!fb.trim()) { if (typeof showToast === 'function') showToast('打回需填写修改意见', 'warning'); return; }
    return _post(taskId, '/review-design', { decision: 'reject', feedback: fb }, '方案已打回，将重做');
  }

  return {
    syncForTask, clear,
    submitClarify, skipClarify, submitFactClarify, approveDesign, rejectDesign,
  };
})();

// ── 规划过程回看（任务详情，独立于上面的交互卡片）──
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

// 向后兼容垫片：旧代码/SSE 处理可能直接调这些名字 → 统一转交单一控制器。
function recoverPendingInteraction(task) { return PlanningInteraction.syncForTask(task); }
function renderClarifyPrompt(taskId, it) { return PlanningInteraction.syncForTask({ id: taskId, status: 'CLARIFYING' }); }
function renderFactIssueClarify(taskId, it) { return PlanningInteraction.syncForTask({ id: taskId, status: 'CLARIFYING' }); }
function renderDesignReviewPrompt(taskId, it) { return PlanningInteraction.syncForTask({ id: taskId, status: 'DESIGN_REVIEW' }); }
