/* Swarm Web UI — core/diff module (split from app.js, shared global scope) */
'use strict';

let diffViewMode = 'unified';

let lastDiffText = '';

function setApplyDiffButtonsDisabled(disabled) {
  document.querySelectorAll('[data-action="apply-diff"], [data-action="check-diff"]').forEach(btn => {
    btn.disabled = disabled;
    btn.title = disabled ? '存在 merge 冲突，无法 apply' : '';
  });
}

function renderDiff(diff) {
  lastDiffText = diff || '';
  const container = $('diff-content');
  if (!diff || !diff.trim()) {
    container.innerHTML = '<div class="diff-empty">暂无 Diff — 任务执行后将在此显示合并后的代码变更</div>';
    return;
  }
  if (diffViewMode === 'split') {
    container.innerHTML = renderSplitDiff(diff);
    container.classList.add('diff-view-split');
  } else {
    container.innerHTML = renderUnifiedDiff(diff);
    container.classList.remove('diff-view-split');
  }
}

function setDiffViewMode(mode) {
  diffViewMode = mode === 'split' ? 'split' : 'unified';
  document.querySelectorAll('[data-diff-mode]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.diffMode === diffViewMode);
  });
  renderDiff(lastDiffText);
}

function renderUnifiedDiff(diff) {
  const lines = diff.split('\n');
  return lines.map(line => {
    let cls = '';
    if (line.startsWith('+') && !line.startsWith('+++')) cls = 'diff-line-add';
    else if (line.startsWith('-') && !line.startsWith('---')) cls = 'diff-line-del';
    else if (line.startsWith('@@')) cls = 'diff-line-hunk';
    return `<div class="${cls}">${escapeHtml(line)}</div>`;
  }).join('');
}

function extractDiffFilePath(line) {
  const m = line.match(/^diff --git a\/(.+?) b\/(.+)$/);
  if (m) return m[2] || m[1];
  if (line.startsWith('+++ ')) return line.slice(4).replace(/^b\//, '');
  return line;
}

function parseUnifiedDiffSections(diff) {
  const sections = [];
  let current = null;
  let currentHunk = null;

  for (const line of diff.split('\n')) {
    if (line.startsWith('diff --git')) {
      if (current) sections.push(current);
      current = { header: line, filePath: extractDiffFilePath(line), hunks: [], fileHeaders: [] };
      currentHunk = null;
    } else if (line.startsWith('@@')) {
      currentHunk = { header: line, lines: [] };
      if (current) current.hunks.push(currentHunk);
    } else if (currentHunk) {
      currentHunk.lines.push(line);
    } else if (current && (line.startsWith('---') || line.startsWith('+++'))) {
      current.fileHeaders.push(line);
    }
  }
  if (current) sections.push(current);
  return sections;
}

function renderSplitDiff(diff) {
  const sections = parseUnifiedDiffSections(diff);
  if (!sections.length) return renderUnifiedDiff(diff);

  return sections.map(sec => {
    const label = escapeHtml(sec.filePath || sec.header || 'file');
    const headers = (sec.fileHeaders || []).map(h =>
      `<div class="diff-file-meta">${escapeHtml(h)}</div>`
    ).join('');
    const hunks = sec.hunks.map(hunk => {
      const leftLines = [];
      const rightLines = [];
      for (const line of hunk.lines) {
        if (line.startsWith('-') && !line.startsWith('---')) {
          leftLines.push({ cls: 'diff-line-del', text: line.slice(1) });
          rightLines.push({ cls: 'diff-line-empty', text: '' });
        } else if (line.startsWith('+') && !line.startsWith('+++')) {
          leftLines.push({ cls: 'diff-line-empty', text: '' });
          rightLines.push({ cls: 'diff-line-add', text: line.slice(1) });
        } else {
          const ctx = line.startsWith(' ') ? line.slice(1) : line;
          leftLines.push({ cls: 'diff-line-ctx', text: ctx });
          rightLines.push({ cls: 'diff-line-ctx', text: ctx });
        }
      }
      const renderCol = lines => lines.map(l =>
        `<div class="${l.cls}">${l.text ? escapeHtml(l.text) : '&nbsp;'}</div>`
      ).join('');
      return `
        <div class="diff-hunk-header">${escapeHtml(hunk.header)}</div>
        <div class="diff-split-row">
          <div class="diff-split-col diff-split-old" aria-label="删除">${renderCol(leftLines)}</div>
          <div class="diff-split-col diff-split-new" aria-label="新增">${renderCol(rightLines)}</div>
        </div>`;
    }).join('');
    return `<div class="diff-file-section"><div class="diff-file-header">${label}</div>${headers}${hunks}</div>`;
  }).join('');
}

async function checkApplyDiff() {
  if (!selectedTaskId) { showToast('请先选择任务', 'warning'); return; }
  try {
    const resp = await fetch('/api/tasks/' + encodeURIComponent(selectedTaskId) + '/apply-diff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ check_only: true }),
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
