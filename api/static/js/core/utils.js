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
