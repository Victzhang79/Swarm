// 经验技能管理（系统级，仅管理员）——编写/导入 + 准入校验。
// 后端:api/routers/skills.py。鉴权走同源 Cookie(swarm_token),plain fetch 即可。
'use strict';

let _skillsData = { builtin: [], db: [] };
let _skillEditingId = null;   // 非空=正在编辑该 DB 技能(PUT);空=新建(POST)
let _skillMode = 'form';       // 'form' | 'md'

function _skEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function _skVal(id) { const el = document.getElementById(id); return el ? el.value : ''; }
function _skSet(id, v) { const el = document.getElementById(id); if (el) el.value = (v == null ? '' : v); }
function _skArr(v) { return String(v || '').split(',').map(x => x.trim()).filter(Boolean); }

async function loadSkills() {
  const el = document.getElementById('skills-list');
  if (!el) return;
  el.textContent = '加载中…';
  try {
    const resp = await fetch('/api/skills');
    if (!resp.ok) { el.textContent = '加载失败: HTTP ' + resp.status; return; }
    _skillsData = await resp.json();
    _renderSkillsList();
  } catch (e) { el.textContent = '加载失败: ' + e; }
}

function _chips(arr) {
  return (arr || []).map(t =>
    `<span style="display:inline-block;padding:1px 5px;border-radius:3px;background:var(--bg-tertiary,#2a2a2a);font-size:10px;margin:0 2px 2px 0">${_skEsc(t)}</span>`
  ).join('');
}

function _renderSkillsList() {
  const el = document.getElementById('skills-list');
  const cnt = document.getElementById('skills-count');
  const builtin = _skillsData.builtin || [];
  const db = _skillsData.db || [];
  if (cnt) cnt.textContent = `内置 ${builtin.length} · 自定义 ${db.length}`;
  let html = '';
  if (db.length) {
    html += '<div style="font-weight:600;margin:2px 0 4px">自定义（可编辑）</div>';
    html += db.map(s => `
      <div style="border:1px solid var(--border,#3a3a3a);border-radius:6px;padding:8px;margin-bottom:6px;${s.enabled ? '' : 'opacity:.5'}">
        <div style="display:flex;justify-content:space-between;gap:6px;align-items:center">
          <b>${_skEsc(s.title || s.id)}</b><span class="hint">${_skEsc(s.source || 'user')}${s.enabled ? '' : ' · 已停用'}</span>
        </div>
        <div class="hint" style="margin:2px 0">${_skEsc(s.id)}</div>
        <div>${_chips(s.applies_to_stacks)}${_chips(s.applies_to_intents)}</div>
        <div style="display:flex;gap:6px;margin-top:4px;flex-wrap:wrap">
          <button class="btn btn-ghost btn-sm" onclick="skillEditDb('${s.id}')">编辑</button>
          <button class="btn btn-ghost btn-sm" onclick="skillToggle('${s.id}', ${s.enabled ? 'false' : 'true'})">${s.enabled ? '停用' : '启用'}</button>
          <button class="btn btn-ghost btn-sm" onclick="skillDelete('${s.id}')">删除</button>
        </div>
      </div>`).join('');
  }
  html += '<div style="font-weight:600;margin:8px 0 4px">内置种子（只读）</div>';
  html += builtin.map(s => `
    <div style="border:1px solid var(--border,#2a2a2a);border-radius:6px;padding:6px 8px;margin-bottom:4px">
      <div>${_skEsc(s.title || s.id)} ${s.overridden ? '<span class="hint">(被自定义覆盖)</span>' : ''}</div>
      <div class="hint">${_skEsc(s.id)}</div>
      <div>${_chips(s.applies_to_stacks)}</div>
      <button class="btn btn-ghost btn-sm" style="margin-top:4px" onclick="skillCloneBuiltin('${s.id}')">复制为自定义</button>
    </div>`).join('');
  el.innerHTML = html;
}

function skillSetMode(mode) {
  _skillMode = mode;
  const f = document.getElementById('skill-form-mode');
  const m = document.getElementById('skill-md-mode');
  if (f) f.style.display = (mode === 'form') ? '' : 'none';
  if (m) m.style.display = (mode === 'md') ? '' : 'none';
  ['skill-mode-form', 'skill-mode-md'].forEach(id => {
    const b = document.getElementById(id);
    if (b) b.classList.toggle('btn-primary', id === 'skill-mode-' + mode);
  });
}

function skillNew() {
  _skillEditingId = null;
  ['sk-id', 'sk-title', 'sk-desc', 'sk-body', 'sk-tags', 'sk-md'].forEach(i => _skSet(i, ''));
  _skSet('sk-stacks', '*'); _skSet('sk-intents', '*'); _skSet('sk-phases', '*');
  _skSet('sk-target', 'worker'); _skSet('sk-priority', '50'); _skSet('sk-maxchars', '1200');
  const eid = document.getElementById('skill-editing-id'); if (eid) eid.textContent = '';
  const res = document.getElementById('skill-result'); if (res) res.innerHTML = '';
  skillSetMode('form');
}

function _fillForm(s, editing) {
  _skSet('sk-id', s.id); _skSet('sk-title', s.title || ''); _skSet('sk-desc', s.description || '');
  _skSet('sk-body', s.body || '');
  _skSet('sk-stacks', (s.applies_to_stacks || ['*']).join(','));
  _skSet('sk-intents', (s.applies_to_intents || ['*']).join(','));
  _skSet('sk-phases', (s.applies_to_phases || ['*']).join(','));
  _skSet('sk-target', (s.target || ['worker']).join(','));
  _skSet('sk-priority', s.priority == null ? 50 : s.priority);
  _skSet('sk-maxchars', s.max_chars == null ? 1200 : s.max_chars);
  _skSet('sk-tags', (s.tags || []).join(','));
  _skillEditingId = editing ? s.id : null;
  const eid = document.getElementById('skill-editing-id');
  if (eid) eid.textContent = editing ? ('编辑中: ' + s.id) : '（复制自内置，另存为新技能）';
  const res = document.getElementById('skill-result'); if (res) res.innerHTML = '';
  skillSetMode('form');
}

function skillEditDb(id) {
  const s = (_skillsData.db || []).find(x => x.id === id);
  if (s) _fillForm(s, true);
}
function skillCloneBuiltin(id) {
  const s = (_skillsData.builtin || []).find(x => x.id === id);
  if (s) _fillForm({ ...s, id: s.id + '-custom' }, false);
}

function _skillFromForm() {
  return {
    id: _skVal('sk-id').trim(), title: _skVal('sk-title'), description: _skVal('sk-desc'),
    body: _skVal('sk-body'),
    applies_to_stacks: _skArr(_skVal('sk-stacks') || '*'),
    applies_to_intents: _skArr(_skVal('sk-intents') || '*'),
    applies_to_phases: _skArr(_skVal('sk-phases') || '*'),
    target: _skArr(_skVal('sk-target') || 'worker'),
    priority: parseInt(_skVal('sk-priority') || '50', 10),
    max_chars: parseInt(_skVal('sk-maxchars') || '1200', 10),
    tags: _skArr(_skVal('sk-tags')), enabled: true,
  };
}

function _renderResult(r, prefixOk) {
  const el = document.getElementById('skill-result');
  if (!el) return;
  let h = '';
  if (r.ok) {
    h += `<div style="color:var(--green,#22a06b)">✓ ${prefixOk || '校验通过'}${r.llm_checked ? '（含 LLM 一致性裁判）' : ''}</div>`;
  }
  (r.errors || []).forEach(e => { h += `<div style="color:var(--red,#e5484d)">✗ ${_skEsc(e)}</div>`; });
  (r.warnings || []).forEach(w => { h += `<div style="color:var(--orange,#e08c00)">⚠ ${_skEsc(w)}</div>`; });
  el.innerHTML = h;
}

async function skillValidate() {
  const useLlm = !!(document.getElementById('sk-llm-judge') || {}).checked;
  const payload = (_skillMode === 'md')
    ? { text: _skVal('sk-md'), use_llm_judge: useLlm }
    : { skill: _skillFromForm(), use_llm_judge: useLlm };
  try {
    const resp = await fetch('/api/skills/validate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    if (resp.status === 400) { _renderResult({ ok: false, errors: ['请填写内容后再校验'] }); return; }
    _renderResult(await resp.json());
  } catch (e) { _renderResult({ ok: false, errors: ['校验请求失败: ' + e] }); }
}

async function skillSave() {
  const useLlm = !!(document.getElementById('sk-llm-judge') || {}).checked;
  let url, method, body;
  if (_skillMode === 'md') {
    url = '/api/skills/import'; method = 'POST';
    body = { text: _skVal('sk-md'), use_llm_judge: useLlm, enabled: true };
  } else {
    const skill = _skillFromForm();
    if (_skillEditingId && _skillEditingId === skill.id) {
      url = '/api/skills/' + encodeURIComponent(skill.id); method = 'PUT';
    } else { url = '/api/skills'; method = 'POST'; }
    body = skill;
  }
  try {
    const resp = await fetch(url, {
      method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (resp.ok) {
      _renderResult({ ok: true, warnings: data.warnings, llm_checked: data.llm_checked }, '已保存入库');
      await loadSkills();
    } else if (resp.status === 422 && data.detail) {
      _renderResult({ ok: false, errors: data.detail.errors || [data.detail.message || '未通过准入校验'],
        warnings: data.detail.warnings });
    } else {
      _renderResult({ ok: false, errors: [(data.detail && (data.detail.message || JSON.stringify(data.detail))) || ('HTTP ' + resp.status)] });
    }
  } catch (e) { _renderResult({ ok: false, errors: ['保存请求失败: ' + e] }); }
}

async function skillToggle(id, enabled) {
  try {
    await fetch('/api/skills/' + encodeURIComponent(id) + '/enabled?enabled=' + (enabled ? 'true' : 'false'),
      { method: 'POST' });
    await loadSkills();
  } catch (e) { /* noop */ }
}

async function skillDelete(id) {
  if (!confirm('删除自定义技能 ' + id + '？')) return;
  try {
    await fetch('/api/skills/' + encodeURIComponent(id), { method: 'DELETE' });
    if (_skillEditingId === id) skillNew();
    await loadSkills();
  } catch (e) { /* noop */ }
}
