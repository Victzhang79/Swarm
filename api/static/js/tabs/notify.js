/* Swarm Web UI — tabs/notify module：外部通知渠道配置 */
'use strict';

let _notifyChannels = [];
let _notifyCatalog = [];      // 预置类型 [{type,label,url_hint}]
let _notifyEventTypes = [];   // 可订阅事件 [{type,label}]
let _notifyChannelSeq = 0;

async function loadNotifyChannels() {
  try {
    const data = await fetch('/api/notify-channels').then(r => r.json());
    _notifyCatalog = data.catalog || [];
    _notifyEventTypes = data.event_types || [];
    _notifyChannels = (data.channels || []).map(c => ({
      id: c.id, type: c.type || 'generic', label: c.label || '',
      webhook_url: '', webhook_url_masked: c.webhook_url_masked || '', has_url: !!c.has_url,
      enabled: c.enabled !== false, events: c.events || [], user_id: c.user_id || '',
    }));
    clearChannelsDirty();
    drawNotifyChannels();
  } catch { /* ignore */ }
}

function markChannelsDirty() {
  const hint = $('channels-dirty-hint');
  if (hint) hint.style.display = '';
  const btn = $('btn-save-channels');
  if (btn) btn.classList.add('btn-pulse');
}

function clearChannelsDirty() {
  const hint = $('channels-dirty-hint');
  if (hint) hint.style.display = 'none';
  const btn = $('btn-save-channels');
  if (btn) btn.classList.remove('btn-pulse');
}

function _typeOptions(sel) {
  return _notifyCatalog.map(t =>
    `<option value="${escapeHtml(t.type)}"${t.type === sel ? ' selected' : ''}>${escapeHtml(t.label || t.type)}</option>`
  ).join('');
}

function _eventChips(ch, i) {
  if (!_notifyEventTypes.length) return '';
  return _notifyEventTypes.map(e => {
    const on = (ch.events || []).includes(e.type);
    return `<label style="font-size:11px;display:inline-flex;align-items:center;gap:3px;margin-right:8px;cursor:pointer">
      <input type="checkbox" class="notify-evt" data-evt="${escapeHtml(e.type)}" ${on ? 'checked' : ''} onchange="markChannelsDirty()">${escapeHtml(e.label)}</label>`;
  }).join('');
}

function drawNotifyChannels() {
  const el = $('notify-channels-list');
  if (!el) return;
  if (!_notifyChannels.length) {
    el.innerHTML = '<p style="font-size:11px;color:var(--text-muted);padding:6px">（暂无通知渠道。点"+ 添加渠道"配置飞书/钉钉/Slack 等，系统通知会自动推送。）</p>';
    return;
  }
  el.innerHTML = _notifyChannels.map((ch, i) => {
    const tpl = _notifyCatalog.find(t => t.type === ch.type) || {};
    const urlPlaceholder = ch.has_url ? (ch.webhook_url_masked || '已配置(留空不改)') : (tpl.url_hint || 'Webhook URL');
    return `
    <div class="card notify-card" style="margin-bottom:8px;padding:10px" data-cidx="${i}">
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
        <select class="form-select notify-f" data-f="type" onchange="markChannelsDirty()" style="flex:0 0 200px">${_typeOptions(ch.type)}</select>
        <label class="form-label" style="margin:0;display:inline-flex;align-items:center;gap:4px;font-size:12px;cursor:pointer">
          <input type="checkbox" class="notify-f" data-f="enabled" ${ch.enabled ? 'checked' : ''} onchange="markChannelsDirty()">启用</label>
        <span style="flex:1"></span>
        <button class="btn btn-ghost btn-sm" onclick="testNotifyChannel(${i})" title="发送测试通知">测试</button>
        <button class="btn btn-danger btn-sm" onclick="removeNotifyChannel(${i})">删除</button>
      </div>
      <input class="form-input notify-f" data-f="webhook_url" type="password" value="" placeholder="${escapeHtml(urlPlaceholder)}" oninput="markChannelsDirty()" style="margin-bottom:6px">
      <div style="font-size:11px;color:var(--text-muted)">订阅事件（不勾=全部）：${_eventChips(ch, i)}</div>
      <input type="hidden" class="notify-f" data-f="id" value="${escapeHtml(ch.id)}">
    </div>`;
  }).join('');
}

function _syncChannelsFromDom() {
  document.querySelectorAll('#notify-channels-list [data-cidx]').forEach(card => {
    const i = parseInt(card.dataset.cidx, 10);
    if (Number.isNaN(i) || !_notifyChannels[i]) return;
    const ch = _notifyChannels[i];
    card.querySelectorAll('.notify-f').forEach(f => {
      const key = f.dataset.f;
      if (f.type === 'checkbox') ch[key] = f.checked;
      else ch[key] = f.value;
    });
    // 事件多选
    const evts = [];
    card.querySelectorAll('.notify-evt:checked').forEach(c => evts.push(c.dataset.evt));
    ch.events = evts;
  });
}

function addNotifyChannel() {
  _syncChannelsFromDom();
  _notifyChannelSeq += 1;
  _notifyChannels.push({
    id: 'ch' + Date.now().toString(36) + _notifyChannelSeq,
    type: 'feishu', label: '', webhook_url: '', has_url: false,
    enabled: true, events: [], user_id: '',
  });
  markChannelsDirty();
  drawNotifyChannels();
}

function removeNotifyChannel(i) {
  _syncChannelsFromDom();
  _notifyChannels.splice(i, 1);
  markChannelsDirty();
  drawNotifyChannels();
}

async function saveNotifyChannels() {
  _syncChannelsFromDom();
  const btn = $('btn-save-channels');
  if (btn) btn.disabled = true;
  try {
    const channels = _notifyChannels
      .filter(c => (c.id || '').trim())
      .map(c => {
        const o = { id: c.id, type: c.type || 'generic', label: c.label || '', enabled: !!c.enabled, events: c.events || [], user_id: c.user_id || '' };
        if (c.webhook_url) o.webhook_url = c.webhook_url;  // 留空不覆盖
        return o;
      });
    const resp = await fetch('/api/notify-channels', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channels }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    await resp.json();
    showToast('通知渠道已保存', 'success');
    await loadNotifyChannels();
  } catch (e) {
    showToast('保存失败: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function testNotifyChannel(i) {
  _syncChannelsFromDom();
  const ch = _notifyChannels[i];
  if (!ch) return;
  try {
    // 优先用已保存渠道(channel_id)；若用户刚填了新 url 则用临时 url 测
    const body = ch.webhook_url
      ? { type: ch.type, webhook_url: ch.webhook_url }
      : { channel_id: ch.id };
    const resp = await fetch('/api/notify-channels/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'test failed');
    showToast(data.delivered ? '测试通知已送达 ✅' : '测试发送失败，检查 URL', data.delivered ? 'success' : 'warning');
  } catch (e) {
    showToast('测试失败: ' + e.message, 'error');
  }
}
