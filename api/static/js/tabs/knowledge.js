/* Swarm Web UI — tabs/knowledge module (split from app.js, shared global scope) */
'use strict';

function graphStatusTag(status) {
  const map = {
    NONE: { cls: 'pill-gray', label: 'GRAPH:NONE' },
    INDEXING: { cls: 'pill-purple', label: 'INDEXING' },
    INDEXED: { cls: 'pill-green', label: 'INDEXED' },
    ERROR: { cls: 'pill-red', label: 'GRAPH:ERROR' },
  };
  const s = map[status] || map.NONE;
  return `<span class="pill ${s.cls}">${s.label}</span>`;
}

function graphStatusTagForOverview(graphStatus, indexStats) {
  if (indexStats?.skipped && (graphStatus === 'NONE' || !graphStatus)) {
    return '<span class="pill pill-amber" title="CodeGraph 未运行，预处理仍已完成">GRAPH:已跳过</span>';
  }
  return graphStatusTag(graphStatus || 'NONE');
}

// ─── Settings Drawer ─────────────────────────────────────

function normalizePlan(plan) {
  if (!plan) return null;
  if (typeof plan === 'string') {
    try { return JSON.parse(plan); } catch { return null; }
  }
  return plan;
}

function showKnowledgeBanner(stats, complexity) {
  const banner = $('knowledge-banner');
  if (!stats) { banner.classList.add('hidden'); return; }
  banner.classList.remove('hidden');
  banner.innerHTML = `
    <span style="color:var(--blue);font-weight:500">知识检索</span>
    ${complexity ? `<span class="knowledge-stat">复杂度 <strong>${escapeHtml(String(complexity))}</strong></span>` : ''}
    <span class="knowledge-stat">Harness <strong>${stats.norms_count || 0}</strong></span>
    <span class="knowledge-stat">符号 <strong>${stats.struct_count || 0}</strong></span>
    <span class="knowledge-stat">语义 <strong>${stats.semantic_count || 0}</strong></span>
    <span class="knowledge-stat">错题 <strong>${stats.mistakes_count || 0}</strong></span>
    <span class="knowledge-stat">成功模式 <strong>${stats.successes_count || 0}</strong></span>`;
}

async function runRetrieveExperiment() {
  const el = $('retrieve-result');
  const query = ($('retrieve-query')?.value || '').trim();
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  if (!query) { showToast('请输入任务描述', 'warning'); return; }
  if (el) el.innerHTML = '<p style="color:var(--text-muted)">检索中…</p>';
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/retrieve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    renderRetrieveResult(await resp.json());
  } catch (e) {
    if (el) el.innerHTML = '<p style="color:var(--red)">失败: ' + escapeHtml(e.message) + '</p>';
  }
}

function renderRetrieveResult(data) {
  const el = $('retrieve-result');
  if (!el) return;
  const raw = data.raw_counts || {};
  const limits = data.limits || {};
  const slices = data.slices || {};
  const hitBlock = (title, items) => {
    if (!items || !items.length) return '';
    return `<details style="margin-bottom:8px"><summary style="cursor:pointer;font-size:12px;font-weight:600">${title} (${items.length})</summary>
      <ul style="margin:6px 0 0;padding-left:18px;font-size:11px;line-height:1.5">${items.slice(0, 8).map(it => {
        const label = typeof it === 'string' ? it : (it.title || it.symbol_name || it.file_path || it.content?.slice?.(0, 60) || JSON.stringify(it).slice(0, 80));
        return `<li>${escapeHtml(String(label))}</li>`;
      }).join('')}</ul></details>`;
  };
  let html = `
    <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">
      <span class="pill pill-green">prompt ${data.prompt_chars || 0} 字</span>
      <span class="pill pill-gray">struct ${raw.struct ?? 0}→${limits.struct ?? '?'}</span>
      <span class="pill pill-gray">semantic ${raw.semantic ?? 0}→${limits.semantic ?? '?'}</span>
      <span class="pill pill-gray">harness ${raw.norms ?? 0}→${limits.norms ?? '?'}</span>
      <span class="pill pill-gray">错题 ${raw.mistakes ?? 0}</span>
      <span class="pill pill-gray">成功 ${raw.successes ?? 0}</span>
    </div>
    ${hitBlock('结构 struct', slices.struct)}
    ${hitBlock('语义 semantic', slices.semantic)}
    ${hitBlock('Harness', slices.norms)}
    <details open><summary style="cursor:pointer;font-size:12px;font-weight:600;margin-bottom:8px">Brain 上下文预览</summary>
      <pre class="retrieve-preview">${escapeHtml(data.prompt_preview || '')}</pre>
    </details>`;
  el.innerHTML = html;
}

async function searchSymbols() {
  const el = $('symbol-search-results');
  const q = ($('symbol-search-q')?.value || '').trim();
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  if (!q) { showToast('请输入符号名', 'warning'); return; }
  if (el) el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">搜索中…</p>';
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/symbols?q=' + encodeURIComponent(q));
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();
    const symbols = data.symbols || [];
    if (!symbols.length) {
      el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">无匹配符号</p>';
      return;
    }
    el.innerHTML = `<table style="width:100%;font-size:11px;border-collapse:collapse">
      <thead><tr style="text-align:left;color:var(--text-muted)"><th>符号</th><th>类型</th><th>文件</th><th>行</th></tr></thead>
      <tbody>${symbols.map(s => `
        <tr><td>${escapeHtml(s.symbol_name || '')}</td><td>${escapeHtml(s.symbol_type || '')}</td>
        <td>${escapeHtml(s.file_path || '')}</td><td>${s.start_line || ''}</td></tr>`).join('')}
      </tbody></table>`;
  } catch (e) {
    if (el) el.innerHTML = '<p style="color:var(--red)">失败: ' + escapeHtml(e.message) + '</p>';
  }
}

// ─── Knowledge (Import / Ingest) ─────────────────────────────

// 隐藏原生 input，选中后用 pill chip 列出文件名（统一风格，不裸露 <input type=file>）。
function renderIngestFileChips() {
  const input = $('ingest-files');
  const chips = $('ingest-file-chips');
  const count = $('ingest-files-count');
  const files = input && input.files ? Array.from(input.files) : [];
  if (count) count.textContent = files.length ? `已选 ${files.length} 个文件` : '未选择文件';
  if (!chips) return;
  chips.innerHTML = files.map(f =>
    `<span class="pill pill-gray" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>`
  ).join('');
}

// 本地文件：先 POST /api/uploads 拿隔离存储后的 path，再 POST .../knowledge/ingest。
async function ingestLocalFiles() {
  const el = $('ingest-result');
  const fileInput = $('ingest-files');
  const btn = $('btn-ingest-local');
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  const files = fileInput && fileInput.files ? Array.from(fileInput.files) : [];
  if (!files.length) { showToast('请选择文件', 'warning'); return; }
  const dryRun = !!($('ingest-dry-run') && $('ingest-dry-run').checked);

  if (btn) btn.disabled = true;
  if (el) el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">上传中…</p>';
  try {
    // 1) 上传到隔离目录
    const fd = new FormData();
    files.forEach(f => fd.append('files', f));
    const upResp = await fetch('/api/uploads', { method: 'POST', body: fd });
    if (!upResp.ok) throw new Error('上传失败: ' + (await upResp.text()));
    const upData = await upResp.json();
    const paths = (upData.files || []).filter(f => f.path).map(f => f.path);
    const upErrors = (upData.files || []).filter(f => f.error);
    if (!paths.length) {
      const msg = upErrors.map(f => `${f.filename}: ${f.error}`).join('；') || '无可用文件';
      if (el) el.innerHTML = '<p style="color:var(--red);font-size:12px">上传后无可导入文件（' + escapeHtml(msg) + '）</p>';
      return;
    }
    if (el) el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">解析' + (dryRun ? '预览' : '+落库') + '中（共 ' + paths.length + ' 个文件）…</p>';

    // 2) 采集
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_paths: paths, source_type: 'local', dry_run: dryRun }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || ('HTTP ' + resp.status));
    }
    renderIngestResult(await resp.json(), upErrors);
    // 落库成功后清空已选文件（dry_run 预览保留，便于继续真导入）
    if (!dryRun && fileInput) { fileInput.value = ''; renderIngestFileChips(); }
  } catch (e) {
    if (el) el.innerHTML = '<p style="color:var(--red);font-size:12px">失败: ' + escapeHtml(e.message || String(e)) + '</p>';
  } finally {
    if (btn) btn.disabled = false;
  }
}

// 远端源：无 token 时端点返 400 + 接入提示，直接显示给用户。
async function ingestRemoteSource() {
  const el = $('ingest-result');
  const btn = $('btn-ingest-remote');
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  const sourceType = ($('ingest-remote-source') && $('ingest-remote-source').value) || 'yuque';
  const dryRun = !!($('ingest-dry-run') && $('ingest-dry-run').checked);
  // 语雀可填命名空间覆盖 env（留空走 YUQUE_NAMESPACE）。
  const sourceConfig = {};
  if (sourceType === 'yuque') {
    const ns = ($('ingest-yuque-namespace') && $('ingest-yuque-namespace').value || '').trim();
    if (ns) sourceConfig.namespace = ns;
  }

  if (btn) btn.disabled = true;
  if (el) el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">连接 ' + escapeHtml(sourceType) + ' 中…</p>';
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/ingest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_type: sourceType, source_config: sourceConfig, dry_run: dryRun }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      // 400 一般是缺 token —— 把后端的接入说明完整显示
      if (el) el.innerHTML = '<div class="card" style="padding:12px;border:1px solid var(--amber)">'
        + '<p style="margin:0 0 6px;font-size:12px;font-weight:600;color:var(--amber)">无法从 ' + escapeHtml(sourceType) + ' 导入</p>'
        + '<pre style="margin:0;font-size:11px;white-space:pre-wrap;color:var(--text-secondary)">' + escapeHtml(err.detail || ('HTTP ' + resp.status)) + '</pre></div>';
      return;
    }
    renderIngestResult(await resp.json(), []);
  } catch (e) {
    if (el) el.innerHTML = '<p style="color:var(--red);font-size:12px">失败: ' + escapeHtml(e.message || String(e)) + '</p>';
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderIngestResult(data, uploadErrors) {
  const el = $('ingest-result');
  if (!el) return;
  const dry = data.dry_run;
  const docs = data.docs || [];
  const skipped = docs.filter(d => d.status === 'skipped');
  const failed = docs.filter(d => d.status === 'error');
  const ok = docs.filter(d => d.status === 'parsed');
  const upErrs = uploadErrors || [];

  let html = `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">
    <span class="pill ${dry ? 'pill-amber' : 'pill-green'}">${dry ? '预览（未落库）' : '已落库'}</span>
    <span class="pill pill-gray">总 ${data.total_docs || 0}</span>
    <span class="pill pill-green">解析 ${data.parsed_docs || 0}</span>
    <span class="pill pill-gray">跳过 ${data.skipped_docs || 0}</span>
    <span class="pill pill-red">失败 ${data.failed_docs || 0}</span>
    ${dry ? '' : `<span class="pill pill-blue">落库 ${data.indexed_chunks || 0} chunk</span>`}
  </div>`;

  const block = (title, items, color) => {
    if (!items || !items.length) return '';
    return `<details style="margin-bottom:8px"${color === 'green' ? ' open' : ''}>
      <summary style="cursor:pointer;font-size:12px;font-weight:600;color:var(--${color})">${title} (${items.length})</summary>
      <ul style="margin:6px 0 0;padding-left:18px;font-size:11px;line-height:1.6">
        ${items.map(d => `<li>${escapeHtml(d.filename || d.title || '?')}${d.num_chunks ? ` — ${d.num_chunks} chunk` : ''}${d.error ? `：${escapeHtml(d.error)}` : ''}</li>`).join('')}
      </ul></details>`;
  };
  html += block('成功', ok, 'green');
  html += block('跳过', skipped, 'amber');
  html += block('失败', failed, 'red');
  if (upErrs.length) {
    html += `<details style="margin-bottom:8px"><summary style="cursor:pointer;font-size:12px;font-weight:600;color:var(--text-muted)">上传被拒 (${upErrs.length})</summary>
      <ul style="margin:6px 0 0;padding-left:18px;font-size:11px;line-height:1.6">${upErrs.map(f => `<li>${escapeHtml(f.filename || '?')}：${escapeHtml(f.error || '')}</li>`).join('')}</ul></details>`;
  }
  el.innerHTML = html;
  if (!dry && data.parsed_docs) {
    showToast(`导入完成：${data.parsed_docs} 篇 / ${data.indexed_chunks} chunk`, 'success');
    if (typeof loadKnowledgeOverview === 'function') loadKnowledgeOverview(selectedProjectId);
  }
}

// ─── Knowledge (Overview + Norms) ────────────────────────────

async function loadKnowledgeOverview(projectId) {
  const el = $('knowledge-overview');
  if (!el || !projectId) return;
  el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">加载中…</p>';
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/knowledge/overview');
    if (!resp.ok) throw new Error('fetch failed');
    renderKnowledgeOverview(await resp.json());
  } catch {
    el.innerHTML = '<p style="font-size:12px;color:var(--red)">加载失败</p>';
  }
}

// round22：索引一致性检查 + 对账修复（surface #4(a) 的陈旧符号/缺失对账到 WebUI）
function renderConsistency(data) {
  const el = $('knowledge-consistency');
  if (!el) return;
  if (data.ok === false) {
    el.innerHTML = '<p style="font-size:12px;color:var(--red)">' + escapeHtml(data.error || '检查失败') + '</p>';
    return;
  }
  const missing = data.missing_index || [];     // 工作区有、索引无
  const stale = data.stale_files || [];         // 索引有、工作区已变/删（陈旧）
  const clean = (!missing.length && !stale.length);
  const head = '<p style="font-size:11px;color:var(--text-muted);margin:0 0 6px">已索引 '
    + (data.indexed_count || 0) + ' · 检查 ' + (data.checked_files || 0) + ' 文件</p>';
  if (clean) {
    el.innerHTML = head + '<p style="font-size:12px;color:var(--green)">✅ 索引与工作区一致，无缺失/陈旧项</p>';
    return;
  }
  const row = (label, arr, color) => arr.length
    ? '<div style="margin-bottom:8px"><b style="color:' + color + '">' + label + ' (' + arr.length + ')</b>'
      + '<div style="font-size:11px;color:var(--text-muted);max-height:120px;overflow:auto">'
      + arr.slice(0, 50).map(x => escapeHtml(typeof x === 'string' ? x : (x.file_path || x.name || JSON.stringify(x)))).join('<br>')
      + (arr.length > 50 ? '<br>… 其余 ' + (arr.length - 50) + ' 项' : '') + '</div></div>'
    : '';
  el.innerHTML = head
    + row('缺失（工作区有、索引无）', missing, 'var(--amber)')
    + row('陈旧（索引有、工作区已变/删）', stale, 'var(--amber)')
    + '<p style="font-size:11px;color:var(--text-muted);margin-top:6px">点击“对账修复”入队重索引以清理。</p>';
}

async function loadKnowledgeConsistency(projectId) {
  const el = $('knowledge-consistency');
  if (!el || !projectId) { if (el) el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">请先选择项目</p>'; return; }
  el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">检查中…</p>';
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/knowledge/consistency');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    renderConsistency(await resp.json());
  } catch (e) {
    el.innerHTML = '<p style="font-size:12px;color:var(--red)">检查失败: ' + escapeHtml(e.message || String(e)) + '</p>';
  }
}

async function repairKnowledgeConsistency(projectId) {
  const el = $('knowledge-consistency');
  if (!el || !projectId) { if (el) el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">请先选择项目</p>'; return; }
  el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">对账修复入队中…</p>';
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/knowledge/consistency?repair=true');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    const queued = data.queued || data.repaired || data.enqueued || 0;
    el.innerHTML = '<p style="font-size:12px;color:var(--green)">✅ 已入队对账修复（' + escapeHtml(String(queued)) + ' 项重索引），稍后自动生效</p>';
  } catch (e) {
    el.innerHTML = '<p style="font-size:12px;color:var(--red)">修复失败: ' + escapeHtml(e.message || String(e)) + '</p>';
  }
}

function assessKnowledgeReadiness(data) {
  const pp = data.preprocess || {};
  const phase = String(pp.phase || '').toLowerCase();
  const projectStatus = data.status || 'UNKNOWN';
  const index = pp.index_stats || {};
  const embed = pp.embed_stats || {};

  const preprocessDone = phase === 'complete' || projectStatus === 'READY';
  const preprocessRunning = projectStatus === 'PREPROCESSING'
    || ['scanning', 'indexing', 'embedding', 'analyzing'].includes(phase);
  const preprocessFailed = phase === 'error' || projectStatus === 'ERROR';

  if (preprocessFailed) {
    return { level: 'error', message: pp.error || pp.message || '预处理失败，请查看预处理 Tab' };
  }
  if (preprocessRunning) {
    return { level: 'running', message: `预处理进行中（${phase || '…'}）— 完成后 Brain 检索将可用` };
  }
  if (!preprocessDone) {
    return { level: 'missing', message: '尚未运行预处理 — Brain 检索质量将受限', showPreprocessCta: true };
  }

  const partial = !!(index.skipped || embed.skipped);
  if (partial) {
    const parts = [];
    if (index.skipped) parts.push('结构索引(Layer A)已跳过');
    if (embed.skipped) parts.push('向量嵌入(Layer B)已跳过');
    return {
      level: 'partial',
      message: '预处理已完成 · ' + parts.join('，') + '（Brain 仍可使用扫描/分析结果，见下方说明）',
    };
  }
  return { level: 'ready', message: '知识库已就绪 — Brain 可正常检索本项目' };
}

function renderKnowledgeStatusBanner(readiness) {
  if (!readiness) return '';
  const styles = {
    ready: { border: 'var(--green)', bg: 'rgba(34,197,94,0.08)', pill: 'pill-green' },
    partial: { border: 'var(--amber)', bg: 'rgba(245,158,11,0.08)', pill: 'pill-amber' },
    running: { border: 'var(--blue)', bg: 'rgba(59,130,246,0.08)', pill: 'pill-blue' },
    missing: { border: 'var(--amber)', bg: 'rgba(245,158,11,0.08)', pill: 'pill-amber' },
    error: { border: 'var(--red)', bg: 'rgba(239,68,68,0.08)', pill: 'pill-red' },
  };
  const s = styles[readiness.level] || styles.missing;
  const cta = readiness.showPreprocessCta
    ? `<button class="btn btn-primary btn-sm" style="margin-top:8px" onclick="switchTab('preprocess')">前往预处理 →</button>`
    : (readiness.level === 'error'
      ? `<button class="btn btn-secondary btn-sm" style="margin-top:8px" onclick="switchTab('preprocess')">查看预处理 →</button>`
      : '');
  return `
    <div class="card" style="padding:12px;margin-bottom:12px;background:${s.bg};border:1px solid ${s.border}">
      <span class="pill ${s.pill}" style="margin-bottom:6px">${readiness.level === 'ready' ? '已就绪' : readiness.level === 'partial' ? '部分就绪' : readiness.level === 'running' ? '进行中' : readiness.level === 'error' ? '异常' : '未预处理'}</span>
      <p style="margin:0;font-size:12px;line-height:1.5;color:var(--text-primary)">${escapeHtml(readiness.message)}</p>
      ${cta}
    </div>`;
}

function renderKnowledgeOverview(data) {
  const el = $('knowledge-overview');
  const pp = data.preprocess || {};
  const scan = pp.scan_stats || {};
  const index = pp.index_stats || {};
  const embed = pp.embed_stats || {};
  const graphStatus = data.graph_status || 'NONE';
  const projectStatus = data.status || 'UNKNOWN';
  const readiness = assessKnowledgeReadiness(data);
  const langs = data.language_breakdown || scan.languages || {};
  const langStr = typeof langs === 'object' && !Array.isArray(langs)
    ? Object.entries(langs).map(([k, v]) => `${k}(${v})`).join(', ')
    : (Array.isArray(langs) ? langs.join(', ') : '');

  const remediation = buildKnowledgeRemediation(data, index, embed, graphStatus, readiness);

  el.innerHTML = `
    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;align-items:center">
      ${graphStatusTagForOverview(graphStatus, index)}
      ${projectStatusTag(projectStatus)}
      <span class="pill pill-blue">预处理 ${escapeHtml(pp.phase || 'unknown')}</span>
      <span class="pill pill-gray">${data.file_count || scan.files || 0} 文件</span>
      <span class="pill pill-gray">${data.symbol_count || data.project_symbol_count || index.symbols || 0} 符号</span>
      <span class="pill pill-gray">${data.qdrant_vectors || 0} 向量</span>
      <span class="pill pill-gray">${data.norms_count || 0} Harness</span>
    </div>
    ${renderKnowledgeStatusBanner(readiness)}
    ${remediation}
    ${langStr ? `<p style="font-size:11px;color:var(--text-muted);margin:0 0 10px">语言: ${escapeHtml(langStr)}</p>` : ''}
    ${embed.skipped && readiness.level !== 'partial' ? `<p style="font-size:11px;color:var(--amber);margin:0 0 10px">向量嵌入已跳过: ${escapeHtml(embed.reason || 'unknown')}</p>` : ''}
    ${pp.error && readiness.level === 'error' ? `<p style="font-size:11px;color:var(--red);margin:0 0 10px">${escapeHtml(pp.error)}</p>` : ''}
    <h4 style="margin:12px 0 6px;font-size:12px;color:var(--text-secondary)">项目架构摘要（Brain 可读）</h4>
    <div style="font-size:12px;line-height:1.6;white-space:pre-wrap;max-height:220px;overflow:auto;color:var(--text-primary)">${escapeHtml(data.description || (readiness.level === 'ready' || readiness.level === 'partial' ? '暂无架构摘要' : '暂无 — 请运行预处理'))}</div>
  `;
}

function buildKnowledgeRemediation(data, index, embed, graphStatus, readiness) {
  if (readiness && (readiness.level === 'missing' || readiness.level === 'running')) {
    return '';
  }
  const cards = [];
  if (index.skipped) {
    const reason = index.reason || 'CodeGraph CLI 未安装或未运行';
    cards.push(`
      <div class="card" style="padding:12px;margin-bottom:10px;border:1px solid var(--border-subtle)">
        <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:var(--amber)">Layer A 结构索引已跳过</p>
        <p style="margin:0 0 8px;font-size:11px;color:var(--text-muted)">${escapeHtml(reason)}</p>
        <p style="margin:0 0 8px;font-size:11px;color:var(--text-secondary)">安装 CodeGraph CLI 后重新预处理，可提升符号级检索精度。</p>
        <button class="btn btn-secondary btn-sm" onclick="switchTab('preprocess');triggerPreprocess()">重新预处理</button>
      </div>`);
  }
  if (embed.skipped) {
    const reason = embed.reason || 'qdrant_unavailable';
    cards.push(`
      <div class="card" style="padding:12px;margin-bottom:10px;border:1px solid var(--border-subtle)">
        <p style="margin:0 0 6px;font-size:12px;font-weight:600;color:var(--amber)">Layer B 向量嵌入已跳过</p>
        <p style="margin:0 0 8px;font-size:11px;color:var(--text-muted)">${escapeHtml(reason)}</p>
        <p style="margin:0 0 8px;font-size:11px;color:var(--text-secondary)">启动 Qdrant 后重新预处理：<code style="font-size:10px">bash scripts/start-services.sh</code></p>
        <button class="btn btn-secondary btn-sm" onclick="switchTab('preprocess');triggerPreprocess()">重新预处理</button>
      </div>`);
  }
  if (data.qdrant_error) {
    cards.push(`
      <div class="card" style="padding:12px;margin-bottom:10px;border:1px solid var(--red)">
        <p style="margin:0 0 6px;font-size:12px;color:var(--red)">Qdrant 连接异常</p>
        <p style="margin:0;font-size:11px">${escapeHtml(data.qdrant_error)}</p>
      </div>`);
  }
  return cards.join('');
}

async function searchSemantic() {
  const el = $('semantic-search-results');
  const q = ($('semantic-search-q')?.value || '').trim();
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  if (!q) { showToast('请输入检索 query', 'warning'); return; }
  if (el) el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">检索中…</p>';
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/semantic?q=' + encodeURIComponent(q));
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();
    const chunks = data.chunks || [];
    if (!chunks.length) {
      el.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">无命中 chunk（检查 Qdrant 是否已嵌入）</p>';
      return;
    }
    el.innerHTML = chunks.map(c => `
      <div class="card" style="margin-bottom:8px;padding:10px">
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px;font-size:11px">
          <span class="pill pill-gray">score ${(c.score ?? 0).toFixed(3)}</span>
          <span class="pill pill-blue">${escapeHtml(c.file_path || '')}:${c.start_line || '?'}</span>
        </div>
        <pre style="margin:0;font-size:11px;white-space:pre-wrap;max-height:120px;overflow:auto">${escapeHtml(c.content_preview || '')}</pre>
      </div>`).join('');
  } catch (e) {
    if (el) el.innerHTML = '<p style="color:var(--red)">失败: ' + escapeHtml(e.message) + '</p>';
  }
}

// ─── Knowledge (Norms) ───────────────────────────────────────

async function loadBehaviorHotspots(projectId) {
  const list = $('behavior-hotspot-list');
  if (!list || !projectId) return;
  list.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">加载中…</p>';
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/knowledge/behavior-hotspots?top_k=15');
    if (!resp.ok) throw new Error('fetch failed');
    const data = await resp.json();
    renderBehaviorHotspots(data.hotspots || []);
  } catch {
    list.innerHTML = '<p style="font-size:12px;color:var(--red)">加载失败</p>';
  }
}

function renderBehaviorHotspots(hotspots) {
  const list = $('behavior-hotspot-list');
  if (!list) return;
  if (!hotspots.length) {
    list.innerHTML = '<p style="font-size:12px;color:var(--text-muted);padding:8px">暂无行为热点（任务 accept 后增量索引会积累修改日志）</p>';
    return;
  }
  list.innerHTML = `<table style="width:100%;font-size:11px;border-collapse:collapse">
    <thead><tr style="text-align:left;color:var(--text-muted)"><th>文件</th><th>修改次数</th><th>最近修改</th></tr></thead>
    <tbody>${hotspots.map(h => `
      <tr>
        <td style="word-break:break-all">${escapeHtml(h.file_path || '')}</td>
        <td>${h.mod_count || 0}</td>
        <td>${h.last_modified ? escapeHtml(String(h.last_modified).substring(0, 19)) : '—'}</td>
      </tr>`).join('')}
    </tbody></table>`;
}

async function loadNorms(projectId) {
  const list = $('norm-list');
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/knowledge/norms');
    if (!resp.ok) throw new Error('fetch failed');
    const data = await resp.json();
    renderNormList(data.norms || data || []);
  } catch {
    list.innerHTML = '<p style="font-size:12px;color:var(--text-muted)">加载失败</p>';
  }
}

function renderNormList(norms) {
  const list = $('norm-list');
  if (!norms.length) {
    list.innerHTML = '<div class="empty-state" style="padding:24px"><p>暂无 Harness 规则</p></div>';
    return;
  }
  list.innerHTML = norms.map(n => {
    const active = n.is_active !== false;
    const editing = normEditingId === String(n.id);
    if (editing) {
      return `
        <div class="card" id="norm-${n.id}" style="padding:14px">
          <h4 style="margin:0 0 10px;font-size:14px">编辑规则 #${n.id}</h4>
          <div class="form-group"><label class="form-label">标题</label><input id="edit-norm-title-${n.id}" class="form-input" value="${escapeHtml(n.title || '')}"></div>
          <div class="form-group"><label class="form-label">内容</label><textarea id="edit-norm-content-${n.id}" class="form-textarea" rows="4">${escapeHtml(n.content || '')}</textarea></div>
          <div class="form-row">
            <div class="form-group"><label class="form-label">标签</label>
              <select id="edit-norm-tag-${n.id}" class="form-select">
                ${['harness','convention','heuristic','preference'].map(t => `<option value="${t}" ${n.tag===t?'selected':''}>${t}</option>`).join('')}
              </select>
            </div>
            <div class="form-group"><label class="form-label">优先级</label><input id="edit-norm-priority-${n.id}" type="number" min="1" max="10" class="form-input" value="${n.priority ?? 5}"></div>
          </div>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="btn btn-ghost btn-sm" onclick="cancelEditNorm()">取消</button>
            <button class="btn btn-primary btn-sm" onclick="saveEditNorm('${n.id}')">保存</button>
          </div>
        </div>`;
    }
    return `
    <div class="card" id="norm-${n.id}">
      <div class="card-head">
        <h4 class="card-title">${escapeHtml(n.title || '')}</h4>
        <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
          <span class="tag tag-${n.tag || 'harness'}">${escapeHtml(n.tag || 'harness')}</span>
          <span class="pill pill-gray">P${n.priority ?? 5}</span>
          <button class="btn btn-ghost btn-sm" onclick="startEditNorm('${n.id}')">编辑</button>
          <button class="btn btn-ghost btn-sm" onclick="toggleNorm('${n.id}', ${!active})">${active ? '禁用' : '启用'}</button>
          <button class="btn btn-danger btn-sm" onclick="deleteNorm('${n.id}')">删</button>
        </div>
      </div>
      <div class="card-body">${escapeHtml(n.content || '')}</div>
    </div>`;
  }).join('');
}

function startEditNorm(normId) {
  normEditingId = String(normId);
  loadNorms(selectedProjectId);
}

function cancelEditNorm() {
  normEditingId = null;
  loadNorms(selectedProjectId);
}

async function saveEditNorm(normId) {
  const title = $(`edit-norm-title-${normId}`)?.value.trim();
  const content = $(`edit-norm-content-${normId}`)?.value.trim();
  if (!title || !content) { showToast('标题和内容不能为空', 'warning'); return; }
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/norms/' + encodeURIComponent(normId), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title,
        content,
        tag: $(`edit-norm-tag-${normId}`)?.value,
        priority: parseInt($(`edit-norm-priority-${normId}`)?.value, 10) || 5,
      }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    showToast('已保存', 'success');
    normEditingId = null;
    loadNorms(selectedProjectId);
  } catch (e) {
    showToast(e.message, 'error');
  }
}

function toggleAddNormForm() {
  $('add-norm-form').classList.toggle('hidden');
}

async function submitAddNorm() {
  const title = $('norm-title').value.trim();
  const content = $('norm-content').value.trim();
  if (!title || !content) { showToast('请填写标题和内容', 'warning'); return; }
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/norms', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title, content,
        tag: $('norm-tag').value,
        priority: parseInt($('norm-priority').value, 10) || 5,
      }),
    });
    if (!resp.ok) throw new Error('提交失败');
    showToast('已添加', 'success');
    toggleAddNormForm();
    loadNorms(selectedProjectId);
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function toggleNorm(normId, enabled) {
  await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/norms/' + encodeURIComponent(normId), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ is_active: enabled }),
  });
  loadNorms(selectedProjectId);
}

async function deleteNorm(normId) {
  if (!confirm('确定删除？')) return;
  await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/knowledge/norms/' + encodeURIComponent(normId), { method: 'DELETE' });
  loadNorms(selectedProjectId);
}

// ─── Memory ──────────────────────────────────────────────────
