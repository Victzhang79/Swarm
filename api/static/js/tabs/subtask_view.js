/* Swarm Web UI — #32 概览仪表盘 + 计划明细表 + 生长树小卡（共享实时数据源）
 *
 * 设计：概览(宏观聚合 + 生长树点缀) 与 计划(逐子任务明细表) 两视图【订阅同一实时源】，
 * 由 computeSubtaskView(task) 单点合成 —— 消除此前两 tab 渲染同一列表的重复。
 * 数据 = plan.subtasks(静态：id/描述/依赖/model/难度) ⋈ task.subtask_runtime(运行态：
 * 状态/重试/handle-fail，来自 graph state 单一事实源)。"当前在做" 由 depends_on+done 集
 * 在活跃态推导（brain 不跟踪 worker 沙箱内相位，绝不臆造 CODING/VERIFYING）。
 */
'use strict';

// 子任务运行态 → 展示桶（含中文/图标/CSS 类）。
const SUBTASK_BUCKETS = {
  done:      { label: '完成',   icon: '✅', cls: 'sv-done',    tree: 'done' },
  running:   { label: '进行中', icon: '🔄', cls: 'sv-running', tree: 'grow' },
  retrying:  { label: '重试中', icon: '⚠️', cls: 'sv-retry',   tree: 'retry' },
  pending:   { label: '待办',   icon: '⏳', cls: 'sv-pending',  tree: 'bud' },
  abandoned: { label: '放弃',   icon: '🥀', cls: 'sv-abandoned', tree: 'failed' },
  unknown:   { label: '待定',   icon: '·',  cls: 'sv-pending',  tree: 'bud' },
};

// 任务状态 → 概览「当前阶段」中文标签（复用 pipeline 语义，不新造事实源）。
const STATUS_PHASE_LABEL = {
  SUBMITTED: '已接收', PENDING: '排队中', ANALYZING: '分析中',
  CLARIFYING: '需求澄清', DESIGN_REVIEW: '方案评审',
  PLANNING: '拆解中', VALIDATING_PLAN: '校验计划', CONFIRMING: '待确认计划',
  DISPATCHING: '派发中', MONITORING: '执行中', HANDLING_FAILURE: '失败处理',
  MERGING: '合并中', VERIFYING_L2: 'L2 验证', VERIFYING_RUNTIME: '运行时冒烟',
  VERIFYING_L3: 'L3 预发验证', DELIVERING: '待交付', IN_REVISION: '修订中',
  LEARNING_SUCCESS: '学习中', LEARNING_FAILURE: '学习中',
  DONE: '已完成', PARTIAL: '部分交付', FAILED: '已失败', CANCELLED: '已取消',
};

function _isActiveStatus(status) {
  return typeof ACTIVE_STATUSES !== 'undefined' && ACTIVE_STATUSES.has(status);
}

/**
 * 合成两视图共享的子任务视图。返回 {subtasks, buckets, activeIds}。
 * buckets 精确守恒：done+running+retrying+pending+abandoned === total。
 */
function computeSubtaskView(task) {
  const plan = (typeof normalizePlan === 'function') ? normalizePlan(task && task.plan) : (task && task.plan);
  const runtime = (task && task.subtask_runtime && typeof task.subtask_runtime === 'object') ? task.subtask_runtime : {};
  const active = _isActiveStatus(task && task.status);

  // id 全集 = plan 子任务 ∪ runtime 键（新拆子块可能先于 plan 刷新到达，占位显示）。
  const planList = (plan && Array.isArray(plan.subtasks)) ? plan.subtasks : [];
  const byId = new Map();
  planList.forEach(st => { if (st && st.id != null) byId.set(String(st.id), st); });
  const ids = [];
  const seen = new Set();
  planList.forEach(st => { if (st && st.id != null) { ids.push(String(st.id)); seen.add(String(st.id)); } });
  Object.keys(runtime).forEach(id => { if (!seen.has(id)) { ids.push(id); seen.add(id); } });

  // 第一遍：确定每个子任务的原始 status（done 集用于依赖就绪判定）。
  const rawStatus = {};
  ids.forEach(id => {
    const rt = runtime[id] || {};
    rawStatus[id] = rt.status || 'pending';
  });
  const doneSet = new Set(ids.filter(id => rawStatus[id] === 'done'));

  const subtasks = [];
  let buckets = { done: 0, running: 0, retrying: 0, pending: 0, abandoned: 0, total: ids.length };
  const activeIds = [];
  const hasRuntime = Object.keys(runtime).length > 0;

  ids.forEach(id => {
    const st = byId.get(id) || { id };
    const rt = runtime[id] || {};
    const raw = rawStatus[id];
    const deps = Array.isArray(st.depends_on) ? st.depends_on.map(String) : [];
    const depsMet = deps.every(d => doneSet.has(d));

    // 有效桶：活跃态下"待办且依赖已满足"= 正在做(running)；其余按原始 status。
    let bucket = raw;
    if (raw === 'pending' && active && depsMet) bucket = 'running';
    // 只接受受守恒不变量追踪的 5 个桶；未知 status 或 'unknown' 哨兵（仅供降级路径整体
    // 使用，不参与逐行累加）一律折回 pending，保证 done+running+retrying+pending+abandoned===total。
    if (!SUBTASK_BUCKETS[bucket] || bucket === 'unknown') bucket = 'pending';
    buckets[bucket] = (buckets[bucket] || 0) + 1;
    if (bucket === 'running' || bucket === 'retrying') activeIds.push(id);

    subtasks.push({
      id,
      description: st.description || '',
      difficulty: st.difficulty || '',
      model: st.model_preference || st.model || '',
      depends_on: deps,
      status: raw,          // 原始运行态（明细表状态列）
      bucket,               // 有效展示桶（含 running 推导）
      retry: rt.retry || 0,
      contract_retry: rt.contract_retry || 0,
      handle_fail: !!rt.handle_fail,
      alternate: !!rt.alternate,
      force_strong: !!rt.force_strong,
      l1_passed: !!rt.l1_passed,
      depsMet,
      placeholder: !byId.has(id),   // runtime-only（新拆子块，plan 尚未刷新）
    });
  });

  // 降级兜底：明细可观测(subtask_runtime)上线前执行的历史任务无逐子块运行态——用任务级
  // 聚合计数(completed/abandoned)如实还原【总量】，避免 0/N 假象；逐行状态标"待定"（诚实：
  // 当时未记录是哪几条完成）。仅对无 runtime 且有聚合信号/已终态的任务生效。
  const total = buckets.total;
  let degraded = false;
  if (!hasRuntime && total > 0) {
    const cDone = Math.max(0, Number(task && task.completed_subtasks) || 0);
    const cAband = Math.max(0, Number(task && task.abandoned_subtasks) || 0);
    if (cDone > 0 || cAband > 0 || !active) {
      degraded = true;
      const done = Math.min(cDone, total);
      const abandoned = Math.min(cAband, total - done);
      buckets = { done, running: 0, retrying: 0, pending: total - done - abandoned, abandoned, total };
      subtasks.forEach(s => { s.bucket = 'unknown'; });
      activeIds.length = 0;
    }
  }

  return { subtasks, buckets, activeIds, degraded };
}

// ─── 生长树小卡（Canvas，移植原型 growth-tree-overview.html） ───────────
class SwarmGrowthTree {
  constructor(canvas) {
    this.cv = canvas;
    this.ctx = canvas.getContext('2d');
    this.reduce = matchMedia('(prefers-reduced-motion:reduce)').matches;
    this.CW = 120; this.CH = 140;
    const DPR = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = this.CW * DPR; canvas.height = this.CH * DPR;
    canvas.style.width = this.CW + 'px'; canvas.style.height = this.CH + 'px';
    this.ctx.scale(DPR, DPR);
    this.counts = { total: 0, done: 0, grow: 0, retry: 0, bud: 0, failed: 0 };
    this.branches = [];
    this.trunkG = 0;
    this.t0 = performance.now();
    this._raf = null;
    this._buildBranches();
  }

  // 应用 CSS 变量色（跟随 app 主题）。
  _col(state) {
    const map = { done: '--green', grow: '--green', retry: '--accent', failed: '--red', bud: '--text-muted' };
    const v = getComputedStyle(document.documentElement).getPropertyValue(map[state] || '--text-muted').trim();
    return v || '#888';
  }
  _stem() { return getComputedStyle(document.documentElement).getPropertyValue('--border').trim() || '#3a3f4c'; }

  update(counts) {
    const c = Object.assign({ total: 0, done: 0, grow: 0, retry: 0, bud: 0, failed: 0 }, counts || {});
    const changedTotal = c.total !== this.counts.total;
    this.counts = c;
    if (changedTotal || this.branches.length === 0) this._buildBranches();
    this._assignStates();
  }

  _buildBranches() {
    const n = Math.min(Math.max(this.counts.total || 3, 3), 18);
    const b = [];
    for (let i = 0; i < n; i++) {
      const at = 0.16 + (0.78 * i) / Math.max(1, n - 1);
      b.push({
        at,
        side: i % 2 === 0 ? -1 : 1,
        len: 20 + ((i * 7) % 14),
        state: 'bud', g: 0, seed: ((i * 97) % 50) / 50,
      });
    }
    this.branches = b;
  }

  // 桶计数 → 视觉枝状态（比例分配，象征而非 1:1）。
  _assignStates() {
    const n = this.branches.length;
    if (!n) return;
    const total = this.counts.total || 0;
    const scale = (v) => (total > 0 ? Math.round((v / total) * n) : 0);
    let quota = {
      done: scale(this.counts.done),
      grow: scale(this.counts.grow),
      retry: scale(this.counts.retry),
      failed: scale(this.counts.failed),
    };
    // 保证至少显示 1 个非 bud（有活动时）且不超发。
    ['grow', 'retry', 'failed'].forEach(k => {
      if (this.counts[k] > 0 && quota[k] === 0) quota[k] = 1;
    });
    let assigned = quota.done + quota.grow + quota.retry + quota.failed;
    if (assigned > n) {
      // 超发时按优先级回收（bud 最先，done 最后被削）。
      for (const k of ['grow', 'retry', 'failed', 'done']) {
        while (assigned > n && quota[k] > 0) { quota[k]--; assigned--; }
      }
    }
    const order = []
      .concat(Array(quota.done).fill('bloom'))
      .concat(Array(quota.grow).fill('grow'))
      .concat(Array(quota.retry).fill('retry'))
      .concat(Array(quota.failed).fill('failed'));
    this.branches.forEach((br, i) => { br.state = order[i] || 'bud'; });
  }

  _spineAt(f) {
    const baseX = this.CW * 0.5, baseY = this.CH - 12, topY = 18;
    const y = baseY + (topY - baseY) * f;
    const bend = Math.sin(f * 2.2) * 7;
    return { x: baseX + bend, y };
  }

  start() { this.stop(); const loop = (now) => { this._draw(now); this._raf = requestAnimationFrame(loop); }; this._raf = requestAnimationFrame(loop); }
  stop() { if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; } }

  _draw(now) {
    // 概览 pane 隐藏（切到计划/日志/Diff）或页面不可见时跳过绘制——省 CPU，不停 rAF
    // （浏览器对隐藏页自动降频；pane 隐藏时 offsetParent 为 null）。切回时无缝续画。
    if (this.cv.offsetParent === null || document.hidden) return;
    const ctx = this.ctx, t = (now - this.t0) / 1000;
    ctx.clearRect(0, 0, this.CW, this.CH);
    const stem = this._stem();
    // #118 主干"点亮"完善——原主干只随 done/total 生长、且恒用灰 stem 描边（任务进行中/规划期
    // 主干一片死灰不点亮）。改：①有效进度纳入【进行中】工作(running/retrying 半权)，执行期主干持续
    // 前进而非只在完成时跳；②已生长段随有效进度渐进【点亮】上色(--green，失败为主转警示色)并柔和呼吸；
    // ③起跑即有基干 + 规划期(total=0)给低幅萌发基态，不再一片死灰。
    const total = this.counts.total || 0;
    const done = this.counts.done || 0;
    const running = (this.counts.grow || 0) + (this.counts.retry || 0);
    const failed = this.counts.failed || 0;
    const prog = total > 0 ? Math.min(1, (done + 0.5 * running) / total) : 0;
    const tg = total > 0 ? (0.42 + 0.53 * prog) : 0.30;  // 起跑即有基干→满进度点亮
    this.trunkG += (tg - this.trunkG) * 0.05;
    const sway = this.reduce ? 0 : Math.sin(t * 0.7) * 2.5;

    // 点亮长度（沿主干）：至少留一点萌发亮，随有效进度增长；失败为主时用警示色。
    const litFrac = total > 0 ? Math.max(0.08, prog) : 0.08;
    const litLen = this.trunkG * litFrac;
    const litCol = (failed > 0 && failed >= done) ? this._col('failed') : this._col('grow');
    const litPulse = this.reduce ? 1 : (0.72 + 0.28 * (0.6 + 0.4 * Math.sin(t * 2.2)));

    // 主干（锥形分段）——已点亮段上色+呼吸，未点亮段保留灰 stem。
    let prev = this._spineAt(0);
    const SEG = 20;
    for (let i = 1; i <= SEG; i++) {
      const f = (i / SEG) * this.trunkG;
      const p = this._spineAt(f); p.x += sway * (i / SEG);
      ctx.beginPath(); ctx.moveTo(prev.x, prev.y); ctx.lineTo(p.x, p.y);
      ctx.lineWidth = Math.max(1.3, 6 * (1 - f * 0.8));
      ctx.lineCap = 'round';
      if (f <= litLen + 1e-3) {
        ctx.strokeStyle = litCol; ctx.globalAlpha = litPulse;
      } else {
        ctx.strokeStyle = stem; ctx.globalAlpha = 1;
      }
      ctx.stroke(); ctx.globalAlpha = 1;
      prev = p;
    }
    // 顶芽
    const tip = this._spineAt(this.trunkG); tip.x += sway;
    ctx.fillStyle = this._col('grow');
    ctx.globalAlpha = this.reduce ? 0.7 : (0.5 + 0.4 * (0.6 + 0.4 * Math.sin(t * 3)));
    ctx.beginPath(); ctx.arc(tip.x, tip.y, 2.2, 0, 7); ctx.fill(); ctx.globalAlpha = 1;

    for (const b of this.branches) {
      if (b.at > this.trunkG + 0.02) continue;
      b.g += (1 - b.g) * 0.06;
      const root = this._spineAt(b.at); root.x += sway * b.at;
      const bsway = this.reduce ? 0 : Math.sin(t * 0.9 + b.seed * 6.28) * 2.5;
      const dx = b.side * (b.len * b.g), dy = -b.len * b.g * 0.7;
      const ex = root.x + dx + bsway, ey = root.y + dy;
      const cx = root.x + dx * 0.5 - b.side * 4, cy = root.y + dy * 0.5 - 6;
      ctx.beginPath(); ctx.moveTo(root.x, root.y); ctx.quadraticCurveTo(cx, cy, ex, ey);
      ctx.lineWidth = Math.max(1, 2.4 * b.g);
      const grad = ctx.createLinearGradient(root.x, root.y, ex, ey);
      grad.addColorStop(0, stem);
      grad.addColorStop(1, b.state === 'bud' ? stem : this._stateColor(b.state));
      ctx.strokeStyle = grad; ctx.stroke();
      this._glyph(ex, ey, b, t, b.state === 'bud' ? 2.4 : 3);
    }
  }

  _stateColor(s) {
    if (s === 'bloom') return this._col('done');
    if (s === 'grow') return this._col('grow');
    if (s === 'retry') return this._col('retry');
    if (s === 'failed') return this._col('failed');
    return this._col('bud');
  }

  _glyph(x, y, n, t, r) {
    const ctx = this.ctx, s = n.state, col = this._stateColor(s);
    if (s === 'grow') {
      const p = this.reduce ? 1 : (0.7 + 0.35 * Math.sin(t * 3 + (n.seed || 0) * 10));
      ctx.save(); ctx.shadowColor = this._col('grow'); ctx.shadowBlur = 9 * p; ctx.fillStyle = col;
      ctx.beginPath(); ctx.arc(x, y, r * (1 + 0.15 * p), 0, 7); ctx.fill(); ctx.restore();
    } else if (s === 'bloom') {
      ctx.fillStyle = col;
      for (let k = 0; k < 5; k++) { const a = (k / 5) * 6.28 + (n.seed || 0) * 3;
        ctx.beginPath(); ctx.ellipse(x + Math.cos(a) * r * 0.8, y + Math.sin(a) * r * 0.8, r * 0.55, r * 0.36, a, 0, 7); ctx.fill(); }
      ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--bg-base').trim() || '#08090d';
      ctx.beginPath(); ctx.arc(x, y, r * 0.45, 0, 7); ctx.fill();
      ctx.fillStyle = col; ctx.globalAlpha = 0.9; ctx.beginPath(); ctx.arc(x, y, r * 0.28, 0, 7); ctx.fill(); ctx.globalAlpha = 1;
    } else if (s === 'retry') {
      ctx.save(); ctx.shadowColor = this._col('retry'); ctx.shadowBlur = 7; ctx.fillStyle = col;
      ctx.beginPath(); ctx.arc(x, y, r, 0, 7); ctx.fill(); ctx.restore();
      ctx.strokeStyle = col; ctx.globalAlpha = 0.6; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(x, y, r * 1.9, 0, 7); ctx.stroke(); ctx.globalAlpha = 1;
    } else if (s === 'failed') {
      ctx.strokeStyle = col; ctx.lineWidth = 1.4; ctx.globalAlpha = 0.85;
      ctx.beginPath(); ctx.moveTo(x - r, y - r); ctx.lineTo(x + r, y + r); ctx.moveTo(x + r, y - r); ctx.lineTo(x - r, y + r); ctx.stroke(); ctx.globalAlpha = 1;
    } else {
      ctx.fillStyle = col; ctx.globalAlpha = 0.55; ctx.beginPath(); ctx.arc(x, y, r * 0.8, 0, 7); ctx.fill(); ctx.globalAlpha = 1;
    }
  }
}

// ─── 概览仪表盘 ────────────────────────────────────────────
let _growthTree = null;
let _elapsedTimer = null;

// 停止概览实时动画（生长树 rAF + 已耗时计时器）。renderOverviewDashboard 重渲时会先停旧
// 再起新；但【隐藏面板而不再重渲】的路径（删除当前活跃任务、切换项目 → showTaskDetailEmpty）
// 不经过重渲，旧 rAF/interval 会对着隐藏(仍在 DOM)的 canvas/计时器空转 → 由 showTaskDetailEmpty
// 统一调用本函数兜底，覆盖所有隐藏路径。
function stopSubtaskViewRealtime() {
  if (_growthTree) { _growthTree.stop(); _growthTree = null; }
  if (_elapsedTimer) { clearInterval(_elapsedTimer); _elapsedTimer = null; }
}

function _fmtElapsed(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const pad = (v) => String(v).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

function _taskElapsedSeconds(task) {
  if (task && typeof task.duration_seconds === 'number' && !_isActiveStatus(task.status)) {
    return task.duration_seconds;
  }
  if (task && task.created_at) {
    const start = Date.parse(task.created_at);
    if (!isNaN(start)) return (Date.now() - start) / 1000;
  }
  return 0;
}

function renderOverviewDashboard(task) {
  const host = $('overview-dashboard');
  if (!host) return;
  const view = computeSubtaskView(task);
  const b = view.buckets;
  const total = b.total || 0;
  const pct = total > 0 ? Math.round((b.done / total) * 100) : 0;
  const phase = STATUS_PHASE_LABEL[task && task.status] || (task && task.status) || '—';
  const activeUnits = b.running + b.retrying;
  const conflicts = Array.isArray(task && task.merge_conflicts) ? task.merge_conflicts.length : 0;

  // 状态分桶 tiles（精确守恒，各桶和=总数）。放弃桶仅在 >0 时显示。
  const tileDefs = [
    ['done', b.done], ['running', b.running], ['retrying', b.retrying], ['pending', b.pending],
  ];
  if (b.abandoned > 0) tileDefs.push(['abandoned', b.abandoned]);
  const tiles = tileDefs.map(([k, v]) => {
    const d = SUBTASK_BUCKETS[k];
    return `<div class="sv-tile ${d.cls}"><span class="sv-tile-n">${v}</span><span class="sv-tile-l">${d.icon} ${d.label}</span></div>`;
  }).join('');

  // "当前在做" 摘要（running/retrying 子任务 id）。
  const activeLabel = view.activeIds.length
    ? view.activeIds.slice(0, 8).map(id => `<code class="sv-active-id">${escapeHtml(id)}</code>`).join(' ')
      + (view.activeIds.length > 8 ? ` <span class="sv-more">+${view.activeIds.length - 8}</span>` : '')
    : (_isActiveStatus(task && task.status) ? '<span class="sv-dim">调度中…</span>' : '<span class="sv-dim">无</span>');

  // 进度环（SVG，周长 = 2πr）。
  const R = 26, C = 2 * Math.PI * R, off = C * (1 - pct / 100);

  host.innerHTML = `
    <div class="sv-hero">
      <div class="sv-tree"><canvas id="sv-tree-canvas"></canvas></div>
      <div class="sv-hero-main">
        <div class="sv-progress">
          <svg class="sv-ring" viewBox="0 0 64 64" width="64" height="64" aria-hidden="true">
            <circle cx="32" cy="32" r="${R}" class="sv-ring-bg"></circle>
            <circle cx="32" cy="32" r="${R}" class="sv-ring-fg" stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}" transform="rotate(-90 32 32)"></circle>
          </svg>
          <div class="sv-progress-txt">
            <div class="sv-big"><span class="sv-done-n">${b.done}</span><span class="sv-slash">/</span><span class="sv-total-n">${total}</span></div>
            <div class="sv-cap">子任务完成 · ${pct}%</div>
          </div>
        </div>
        <div class="sv-metrics">
          <span class="sv-metric" title="当前 pipeline 阶段">阶段 <b>${escapeHtml(phase)}</b></span>
          <span class="sv-metric" title="正在执行/重试中的子任务数">活跃单元 <b>${activeUnits}</b></span>
          <span class="sv-metric" title="任务运行时长"><span id="sv-elapsed">${_fmtElapsed(_taskElapsedSeconds(task))}</span></span>
          ${conflicts > 0 ? `<span class="sv-metric sv-metric-warn" title="合并冲突数">冲突 <b>${conflicts}</b></span>` : ''}
        </div>
      </div>
    </div>
    <div class="sv-buckets">${tiles}</div>
    <div class="sv-active-row"><span class="sv-active-label">当前在做</span> ${activeLabel}</div>`;

  // 生长树：counts 由桶映射（done→bloom / running→grow / retrying→retry / pending→bud / abandoned→failed）。
  const canvas = $('sv-tree-canvas');
  if (canvas) {
    if (_growthTree) _growthTree.stop();
    _growthTree = new SwarmGrowthTree(canvas);
    _growthTree.update({ total, done: b.done, grow: b.running, retry: b.retrying, bud: b.pending, failed: b.abandoned });
    _growthTree.start();
  }

  // 已耗时实时计时（仅活跃任务；终态用 duration_seconds 定格）。
  if (_elapsedTimer) { clearInterval(_elapsedTimer); _elapsedTimer = null; }
  if (_isActiveStatus(task && task.status)) {
    _elapsedTimer = setInterval(() => {
      const el = document.getElementById('sv-elapsed');
      if (!el) { clearInterval(_elapsedTimer); _elapsedTimer = null; return; }
      el.textContent = _fmtElapsed(_taskElapsedSeconds(task));
    }, 1000);
  }
}

// ─── 计划明细表（微观逐子任务） ─────────────────────────────
function renderPlanTable(task) {
  const container = $('plan-content');
  if (!container) return;
  const view = computeSubtaskView(task);
  if (!view.subtasks.length) {
    container.innerHTML = '<div class="sv-empty">计划尚未生成</div>';
    return;
  }
  const rows = view.subtasks.map(st => {
    const bk = SUBTASK_BUCKETS[st.bucket] || SUBTASK_BUCKETS.pending;
    // 阶段/L1 列：诚实反映 brain 可见的进度信号。
    let phase;
    if (st.bucket === 'done') phase = 'L1 通过';
    else if (st.bucket === 'running') phase = st.depsMet ? '执行中' : '就绪';
    else if (st.bucket === 'retrying') phase = st.l1_passed === false && st.retry > 0 ? `重试中 · 第 ${st.retry + 1} 轮` : '重试中';
    else if (st.bucket === 'abandoned') phase = '已放弃';
    else if (st.bucket === 'unknown') phase = '—';
    else phase = st.depsMet ? '待派发' : '待依赖';
    const retryTxt = st.retry > 0 ? String(st.retry) : '—';
    const retryExtra = st.contract_retry > 0 ? ` <span class="sv-sub" title="契约偏离重试">+契约${st.contract_retry}</span>` : '';
    let hf = '';
    if (st.handle_fail) {
      const tags = [];
      if (st.alternate) tags.push('换备');
      if (st.force_strong) tags.push('强模型');
      hf = `<span class="sv-hf" title="handle-failure 已介入${tags.length ? '：' + tags.join('/') : ''}">⚠ ${tags.join('/') || 'HF'}</span>`;
    }
    const deps = st.depends_on.length
      ? st.depends_on.map(d => `<code class="sv-dep">${escapeHtml(d)}</code>`).join(' ')
      : '<span class="sv-dim">—</span>';
    const desc = escapeHtml(st.description || st.id) + (st.placeholder ? ' <span class="sv-sub">(新拆·待刷新)</span>' : '');
    return `
      <tr class="${bk.cls}">
        <td class="sv-c-id"><code>${escapeHtml(st.id)}</code></td>
        <td class="sv-c-desc">${desc}</td>
        <td class="sv-c-status"><span class="sv-badge ${bk.cls}">${bk.icon} ${bk.label}</span></td>
        <td class="sv-c-phase">${escapeHtml(phase)}</td>
        <td class="sv-c-retry sv-num">${retryTxt}${retryExtra}</td>
        <td class="sv-c-hf">${hf || '<span class="sv-dim">—</span>'}</td>
        <td class="sv-c-deps">${deps}</td>
        <td class="sv-c-model">${st.model ? `<span class="sv-model">${escapeHtml(st.model)}</span>` : '<span class="sv-dim">默认</span>'}</td>
        <td class="sv-c-diff">${st.difficulty ? escapeHtml(st.difficulty) : '<span class="sv-dim">—</span>'}</td>
      </tr>`;
  }).join('');

  const b = view.buckets;
  const degradedNote = view.degraded
    ? '<div class="sv-degraded">该任务在逐子任务可观测上线前执行 · 仅有聚合计数，逐行状态标「待定」</div>'
    : '';
  container.innerHTML = `
    <div class="sv-plan-summary">共 <b>${b.total}</b> 子任务 · ✅${b.done} · 🔄${b.running} · ⚠️${b.retrying} · ⏳${b.pending}${b.abandoned ? ` · 🥀${b.abandoned}` : ''}</div>
    ${degradedNote}
    <div class="sv-table-wrap">
      <table class="sv-table">
        <thead><tr>
          <th>st-ID</th><th>描述</th><th>状态</th><th>阶段/L1</th><th>重试</th><th>HF</th><th>依赖</th><th>model</th><th>难度</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// ─── 实时源：SSE 'subtasks' tick 应用（两视图同源刷新） ─────────
let _lastSubtaskRefetch = 0;

function applySubtaskTick(data) {
  if (!selectedTaskDetail || !data) return;
  const rt = data.subtask_runtime;
  if (rt && typeof rt === 'object') {
    // 检测结构性变化（新拆子块 id）→ 拉全量 detail 取新 plan（含描述/依赖）；否则原地更新。
    const prevIds = new Set(Object.keys(selectedTaskDetail.subtask_runtime || {}));
    const newIds = Object.keys(rt).filter(id => !prevIds.has(id));
    selectedTaskDetail.subtask_runtime = rt;
    if (typeof data.total === 'number') selectedTaskDetail.subtask_count = data.total;
    if (typeof data.completed === 'number') selectedTaskDetail.completed_subtasks = data.completed;

    const planIds = new Set(
      (selectedTaskDetail.plan && Array.isArray(selectedTaskDetail.plan.subtasks)
        ? selectedTaskDetail.plan.subtasks : []).map(s => String(s && s.id)));
    const needsPlan = newIds.some(id => !planIds.has(id));
    const now = Date.now();
    if (needsPlan && now - _lastSubtaskRefetch > 1500 && selectedTaskId) {
      _lastSubtaskRefetch = now;
      fetch('/api/tasks/' + encodeURIComponent(selectedTaskId))
        .then(r => r.ok ? r.json() : null)
        .then(d => {
          const t = d && (d.task || d);
          if (!t || t.id !== selectedTaskId) return;
          t.plan = (typeof normalizePlan === 'function') ? normalizePlan(t.plan) : t.plan;
          selectedTaskDetail = t;
          _rerenderSubtaskViews(selectedTaskDetail);
        })
        .catch(() => {});
      return;
    }
  }
  _rerenderSubtaskViews(selectedTaskDetail);
}

// 只重渲当前可见的视图（省无谓 DOM/canvas 重建），另一视图切过去时由 renderTaskDetail 覆盖。
function _rerenderSubtaskViews(task) {
  const detailTab = (typeof currentDetailTab !== 'undefined') ? currentDetailTab : 'overview';
  if (detailTab === 'overview') renderOverviewDashboard(task);
  else if (detailTab === 'plan') renderPlanTable(task);
}
