/* Swarm Web UI — 通知铃铛模块
 *
 * 顶部右上角铃铛：未读时显示绿点，点击展开浮窗通知列表，
 * 每条可归档，全部归档后绿点消失。通知来自后端 /api/notifications。
 */
'use strict';

// 事件类型 → 中文标签 + 药丸样式
function notifEventLabel(eventType) {
  switch (eventType) {
    case 'task_created': return '建立';
    case 'task_completed': return '完成';
    case 'task_failed': return '失败';
    case 'task_partial': return '部分交付';
    case 'task_cancelled': return '取消';
    case 'waiting_review': return '待审';
    default: return '通知';
  }
}

function notifEventPill(eventType) {
  switch (eventType) {
    case 'task_created': return 'pill-blue';
    case 'task_completed': return 'pill-green';
    case 'task_failed': return 'pill-red';
    case 'task_partial': return 'pill-amber';
    case 'task_cancelled': return 'pill-gray';
    case 'waiting_review': return 'pill-amber';
    default: return 'pill-gray';
  }
}

// 通知时间：解析 ISO 字符串 → 本地「月-日 时:分」。无效时回退原串。
function formatNotifTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// 更新铃铛绿点
function renderNotifDot(unread) {
  notifUnreadCount = unread || 0;
  const dot = document.getElementById('notif-dot');
  if (!dot) return;
  dot.classList.toggle('hidden', notifUnreadCount <= 0);
}

// 渲染浮窗通知列表
function renderNotifPanel(items) {
  const list = document.getElementById('notif-panel-list');
  if (!list) return;
  if (!items || !items.length) {
    list.innerHTML = '<p class="notif-empty">暂无通知</p>';
    return;
  }
  list.innerHTML = items.map(n => {
    const tid = n.task_id || '';
    const pid = n.project_id || '';
    const time = formatNotifTime(n.created_at);
    return `
      <div class="notif-row" data-id="${n.id}">
        <span class="pill ${notifEventPill(n.event_type)}">${escapeHtml(notifEventLabel(n.event_type))}</span>
        <div class="notif-row-body" onclick="openNotificationTask('${escapeHtml(tid)}','${escapeHtml(pid)}')">
          <p class="notif-row-title">${escapeHtml(n.title || '')}</p>
          <p class="notif-row-msg">${escapeHtml(n.message || '')}</p>
          <span class="notif-row-meta">${escapeHtml(time)}</span>
        </div>
        <button class="notif-row-archive" title="归档"
                onclick="archiveNotification(${n.id})">归档</button>
      </div>`;
  }).join('');
}

// 轮询：只取未读数刷新绿点（轻量，后台 15s 一次）
async function pollNotificationBell() {
  try {
    const resp = await fetch('/api/notifications/unread_count');
    if (!resp.ok) return;
    const data = await resp.json();
    renderNotifDot(data.unread_count || 0);
  } catch { /* ignore */ }
}

// 拉取完整列表（打开浮窗时）
async function loadNotifList() {
  try {
    const resp = await fetch('/api/notifications?limit=50');
    if (!resp.ok) return;
    const data = await resp.json();
    renderNotifPanel(data.notifications || []);
    renderNotifDot(data.unread_count || 0);
  } catch (e) {
    console.error('loadNotifList failed:', e);
  }
}

function toggleNotifPanel() {
  const panel = document.getElementById('notif-panel');
  if (!panel) return;
  notifPanelOpen = panel.classList.contains('hidden');
  panel.classList.toggle('hidden', !notifPanelOpen);
  if (notifPanelOpen) {
    loadNotifList();
    // 点击浮窗外部关闭
    setTimeout(() => document.addEventListener('click', notifOutsideClick), 0);
  } else {
    document.removeEventListener('click', notifOutsideClick);
  }
}

function closeNotifPanel() {
  const panel = document.getElementById('notif-panel');
  if (panel) panel.classList.add('hidden');
  notifPanelOpen = false;
  document.removeEventListener('click', notifOutsideClick);
}

function notifOutsideClick(e) {
  const wrap = document.querySelector('.notif-bell-wrap');
  if (wrap && !wrap.contains(e.target)) closeNotifPanel();
}

async function archiveNotification(id) {
  try {
    const resp = await fetch(`/api/notifications/${id}/archive`, { method: 'POST' });
    if (!resp.ok) return;
    await loadNotifList();
  } catch { /* ignore */ }
}

async function archiveAllNotifications() {
  try {
    const resp = await fetch('/api/notifications/archive_all', { method: 'POST' });
    if (!resp.ok) return;
    await loadNotifList();
  } catch { /* ignore */ }
}
