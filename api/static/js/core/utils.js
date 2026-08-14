/* Swarm Web UI — core/utils module (split from app.js, shared global scope) */
'use strict';

function parseScopeInput(text) {
  if (!text || !String(text).trim()) return null;
  const paths = String(text).split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
  return paths.length ? paths : null;
}

// ─── Helpers ─────────────────────────────────────────────
function $(id) { return document.getElementById(id); }

function escapeHtml(text) {
  if (text == null) return '';
  const d = document.createElement('div');
  d.textContent = String(text);
  return d.innerHTML;
}

// 30 号文批11 D-1②：属性上下文分档转义器。escapeHtml 只覆盖 HTML 文本上下文
// （按规范文本节点序列化只转 & < >），引号定界属性里 ' " ` 原样穿过=注入面。
// 凡插值进 HTML 属性（value="..."/data-arg0="..." 等）必须用本函数；
// 绝不在 escapeHtml 里补引号转义——文本上下文占多数，全局改会把正常引号变实体
// （可见回归），且不解决 JS 字符串上下文其它逃逸维度（上下文分档=消费契约分档）。
function escapeAttr(text) {
  if (text == null) return '';
  return String(text).replace(/[&<>"'`]/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;',
  }[c]));
}

// 委托迁移辅助（data-on-click 形态，见 core/delegate.js 约定）：
// 原内联 `document.getElementById('x').click()` / `this.classList.toggle(...)` 的命名化。
function clickElementById(id) {
  const el = $(id);
  if (el) el.click();
}

function toggleExpandedClass(cls) {
  this.classList.toggle(cls || 'expanded');
}

function formatTime(d) {
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatDurationSeconds(sec) {
  if (sec == null || Number.isNaN(Number(sec))) return '—';
  const n = Number(sec);
  if (n < 60) return Math.round(n) + 's';
  if (n < 3600) return Math.round(n / 60) + 'm';
  return (n / 3600).toFixed(1) + 'h';
}

function formatAcceptRate(rate) {
  if (rate == null) return '—';
  return (Number(rate) * 100).toFixed(1) + '%';
}

function formatTokenCount(n) {
  if (n == null || Number.isNaN(Number(n))) return '—';
  const val = Number(n);
  if (val >= 1_000_000) return (val / 1_000_000).toFixed(1) + 'M';
  if (val >= 1_000) return (val / 1_000).toFixed(1) + 'K';
  return String(Math.round(val));
}

function formatTestLine(label, item) {
  if (!item) return `${label}: 未知`;
  if (item.ok) return `✓ ${label} (${escapeHtml(item.model || '')}): ${escapeHtml(item.preview || 'OK')}`;
  return `✗ ${label} (${escapeHtml(item.model || '')}): ${escapeHtml(item.error || 'failed')}`;
}

function formatBytes(n) {
  const num = Number(n);
  if (!num || num < 0) return '';
  if (num < 1024) return num + ' B';
  if (num < 1024 * 1024) return (num / 1024).toFixed(1) + ' KB';
  return (num / (1024 * 1024)).toFixed(1) + ' MB';
}

function formatLogTime(iso) {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleTimeString('zh-CN', { hour12: false }) + '.' + String(d.getMilliseconds()).padStart(3, '0');
  } catch {
    return iso;
  }
}
