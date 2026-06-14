/* Swarm Web UI — tabs/user_admin：用户与项目成员管理（A2，仅系统管理员）。
   - 创建用户 + 设全局角色（admin/developer/viewer）
   - 项目成员指派：选项目 → 加成员并设项目角色（owner=项目管理员 / developer=成员 / viewer=访客）
   非 admin 自动隐藏整个区块（后端仍强制 config:write / member:manage 鉴权）。 */
'use strict';

let _userCache = [];

// 设置页激活时调用：非 admin 隐藏，admin 加载用户 + 项目下拉。
async function loadUserAdmin() {
  const section = $('user-admin-section');
  if (!section) return;
  const admin = typeof isAdmin === 'function' ? isAdmin() : false;
  if (!admin) { section.style.display = 'none'; return; }
  section.style.display = '';
  await loadUserList();
  await loadProjectOptions();
}

async function loadUserList() {
  const box = $('user-list');
  if (!box) return;
  box.innerHTML = '<p class="hint">加载中…</p>';
  try {
    const r = await fetch('/api/users');
    if (!r.ok) {
      if (r.status === 403) { box.innerHTML = '<p class="hint">仅管理员可查看</p>'; return; }
      throw new Error('HTTP ' + r.status);
    }
    const data = await r.json();
    _userCache = data.users || [];
    if (!_userCache.length) { box.innerHTML = '<p class="hint">暂无用户</p>'; }
    else {
      const roleLabel = { admin: '管理员', developer: '开发者', viewer: '访客' };
      let html = `<table style="width:100%;border-collapse:collapse">
        <tr style="text-align:left;color:var(--text-muted)">
          <th style="padding:4px 6px">用户名</th><th style="padding:4px 6px">显示名</th>
          <th style="padding:4px 6px;width:100px">全局角色</th>
        </tr>`;
      for (const u of _userCache) {
        const rl = roleLabel[u.global_role] || u.global_role;
        const pill = u.global_role === 'admin' ? 'pill-green' : 'pill-gray';
        html += `<tr>
          <td style="padding:4px 6px;font-weight:600">${escapeHtml(u.username)}</td>
          <td style="padding:4px 6px">${escapeHtml(u.display_name || '')}</td>
          <td style="padding:4px 6px"><span class="pill ${pill}">${escapeHtml(rl)}</span></td>
        </tr>`;
      }
      html += '</table>';
      box.innerHTML = html;
    }
    // 同步成员指派的用户下拉
    const sel = $('pm-user');
    if (sel) {
      sel.innerHTML = '<option value="">选择用户…</option>' +
        _userCache.map(u => `<option value="${escapeHtml(u.id)}">${escapeHtml(u.username)}</option>`).join('');
    }
  } catch (e) {
    box.innerHTML = `<p class="hint" style="color:var(--orange)">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}

async function createUser() {
  const username = ($('nu-username')?.value || '').trim();
  const password = ($('nu-password')?.value || '').trim();
  const display = ($('nu-display')?.value || '').trim();
  const role = $('nu-role')?.value || 'developer';
  if (!username || !password) { showToast('用户名和密码必填', 'warning'); return; }
  if (password.length < 6) { showToast('密码至少 6 位', 'warning'); return; }
  const btn = $('btn-create-user');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/users', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, display_name: display, global_role: role }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    showToast(`用户 ${username} 已创建`, 'success');
    if ($('nu-username')) $('nu-username').value = '';
    if ($('nu-password')) $('nu-password').value = '';
    if ($('nu-display')) $('nu-display').value = '';
    await loadUserList();
  } catch (e) {
    showToast('创建失败: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function loadProjectOptions() {
  const sel = $('pm-project');
  if (!sel) return;
  try {
    const r = await fetch('/api/projects');
    if (!r.ok) return;
    const data = await r.json();
    const projects = data.projects || data || [];
    const cur = sel.value;
    sel.innerHTML = '<option value="">选择项目…</option>' +
      projects.map(p => {
        const pid = p.id || p.project_id || p.name;
        const name = p.name || p.id || pid;
        return `<option value="${escapeHtml(String(pid))}">${escapeHtml(String(name))}</option>`;
      }).join('');
    if (cur) sel.value = cur;
  } catch { /* ignore */ }
}

async function loadProjectMembers() {
  const box = $('member-list');
  const pid = $('pm-project')?.value || '';
  if (!box) return;
  if (!pid) { box.innerHTML = '<p class="hint">先选择项目</p>'; return; }
  box.innerHTML = '<p class="hint">加载中…</p>';
  try {
    const r = await fetch('/api/projects/' + encodeURIComponent(pid) + '/members');
    if (!r.ok) {
      if (r.status === 403) { box.innerHTML = '<p class="hint">无权查看该项目成员</p>'; return; }
      throw new Error('HTTP ' + r.status);
    }
    const data = await r.json();
    const members = data.members || [];
    if (!members.length) { box.innerHTML = '<p class="hint">该项目暂无成员</p>'; return; }
    const roleLabel = { owner: '项目管理员', developer: '成员', viewer: '访客', admin: '管理员' };
    let html = `<table style="width:100%;border-collapse:collapse">
      <tr style="text-align:left;color:var(--text-muted)">
        <th style="padding:4px 6px">用户</th><th style="padding:4px 6px;width:110px">项目角色</th>
        <th style="padding:4px 6px;width:70px">操作</th></tr>`;
    for (const m of members) {
      const uname = m.username || m.user_id || m.id;
      const rl = roleLabel[m.role] || m.role;
      const pill = m.role === 'owner' ? 'pill-green' : 'pill-gray';
      html += `<tr>
        <td style="padding:4px 6px">${escapeHtml(String(uname))}</td>
        <td style="padding:4px 6px"><span class="pill ${pill}">${escapeHtml(rl)}</span></td>
        <td style="padding:4px 6px"><button class="btn btn-danger btn-sm" onclick="removeProjectMember('${escapeHtml(String(m.user_id || m.id))}')">移除</button></td>
      </tr>`;
    }
    html += '</table>';
    box.innerHTML = html;
  } catch (e) {
    box.innerHTML = `<p class="hint" style="color:var(--orange)">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}

async function addProjectMember() {
  const pid = $('pm-project')?.value || '';
  const uid = $('pm-user')?.value || '';
  const role = $('pm-role')?.value || 'developer';
  if (!pid) { showToast('请选择项目', 'warning'); return; }
  if (!uid) { showToast('请选择用户', 'warning'); return; }
  const btn = $('btn-add-member');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch('/api/projects/' + encodeURIComponent(pid) + '/members', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: uid, role }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || 'HTTP ' + r.status);
    showToast('成员已加入/更新', 'success');
    await loadProjectMembers();
  } catch (e) {
    showToast('操作失败: ' + e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function removeProjectMember(userId) {
  const pid = $('pm-project')?.value || '';
  if (!pid || !userId) return;
  if (!confirm('移除该项目成员？')) return;
  try {
    const r = await fetch('/api/projects/' + encodeURIComponent(pid) + '/members/' + encodeURIComponent(userId), { method: 'DELETE' });
    if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || 'HTTP ' + r.status); }
    showToast('成员已移除', 'success');
    await loadProjectMembers();
  } catch (e) {
    showToast('移除失败: ' + e.message, 'error');
  }
}
