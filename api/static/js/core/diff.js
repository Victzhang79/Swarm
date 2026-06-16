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

function extractDiffFilePath(line) {
  const m = line.match(/^diff --git a\/(.+?) b\/(.+)$/);
  if (m) return m[2] || m[1];
  if (line.startsWith('+++ ')) return line.slice(4).replace(/^b\//, '').replace(/\t.*$/, '').trim();
  if (line.startsWith('--- ')) return line.slice(4).replace(/^a\//, '').replace(/\t.*$/, '').trim();
  return line;
}

// 把 unified diff 拆成 per-file sections。
// 关键(task a58b5cd8)：swarm 的 merged_diff 用 `--- a/ +++ b/` 分隔文件，【没有 diff --git 行】，
// 所以文件边界必须同时识别 `--- a/`（在已有 hunk 之后再遇到 --- 即为新文件）和 `diff --git`。
function parseUnifiedDiffSections(diff) {
  const sections = [];
  let current = null;
  let currentHunk = null;

  const lines = diff.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const isGitHeader = line.startsWith('diff --git');
    // `--- a/...` 且下一行是 `+++ b/...` → 新文件起点（swarm diff 无 diff --git 时的边界）
    const isFileMinus = line.startsWith('--- ') && (lines[i + 1] || '').startsWith('+++ ');

    if (isGitHeader || (isFileMinus && (!current || current.hunks.length > 0 || current.started))) {
      if (current) sections.push(current);
      current = { filePath: '', hunks: [], started: false, adds: 0, dels: 0 };
      currentHunk = null;
      if (isGitHeader) current.filePath = extractDiffFilePath(line);
      else { current.filePath = extractDiffFilePath(line); current.started = true; i++; /* 吃掉 +++ 行 */ }
    } else if (line.startsWith('+++ ')) {
      if (current && !current.filePath) current.filePath = extractDiffFilePath(line);
    } else if (line.startsWith('--- ')) {
      // 文件头的 --- 行（紧跟 diff --git 后），忽略
    } else if (line.startsWith('@@')) {
      currentHunk = { header: line, lines: [] };
      if (current) current.hunks.push(currentHunk);
    } else if (currentHunk && current) {
      currentHunk.lines.push(line);
      if (line.startsWith('+') && !line.startsWith('+++')) current.adds++;
      else if (line.startsWith('-') && !line.startsWith('---')) current.dels++;
    }
  }
  if (current) sections.push(current);
  return sections.filter(s => s.filePath && s.hunks.length);
}

// GitHub 风格：按文件分组，每文件带文件名标题 + 增删统计 + 该文件的 hunks。
function renderUnifiedDiff(diff) {
  const sections = parseUnifiedDiffSections(diff);
  if (!sections.length) {
    // 兜底：解析不出文件就平铺（极少见）
    return diff.split('\n').map(line => {
      let cls = '';
      if (line.startsWith('+') && !line.startsWith('+++')) cls = 'diff-line-add';
      else if (line.startsWith('-') && !line.startsWith('---')) cls = 'diff-line-del';
      else if (line.startsWith('@@')) cls = 'diff-line-hunk';
      return `<div class="${cls}">${escapeHtml(line)}</div>`;
    }).join('');
  }

  const totalAdds = sections.reduce((s, f) => s + f.adds, 0);
  const totalDels = sections.reduce((s, f) => s + f.dels, 0);
  const summary = `<div class="diff-summary">${sections.length} 个文件变更`
    + ` <span class="diff-stat-add">+${totalAdds}</span>`
    + ` <span class="diff-stat-del">-${totalDels}</span></div>`;

  const files = sections.map(sec => {
    const label = escapeHtml(sec.filePath || 'file');
    const stat = `<span class="diff-file-stat">`
      + `<span class="diff-stat-add">+${sec.adds}</span> `
      + `<span class="diff-stat-del">-${sec.dels}</span></span>`;
    const body = sec.hunks.map(hunk => {
      const hunkHeader = `<div class="diff-line-hunk">${escapeHtml(hunk.header)}</div>`;
      const hunkLines = hunk.lines.map(line => {
        let cls = '';
        if (line.startsWith('+') && !line.startsWith('+++')) cls = 'diff-line-add';
        else if (line.startsWith('-') && !line.startsWith('---')) cls = 'diff-line-del';
        else cls = 'diff-line-ctx';
        return `<div class="${cls}">${escapeHtml(line) || '&nbsp;'}</div>`;
      }).join('');
      return hunkHeader + hunkLines;
    }).join('');
    return `<div class="diff-file-section">`
      + `<div class="diff-file-header"><span class="diff-file-name">📄 ${label}</span>${stat}</div>`
      + `<div class="diff-file-body">${body}</div></div>`;
  }).join('');

  return summary + files;
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
