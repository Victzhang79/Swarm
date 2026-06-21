/* Swarm Web UI — core/auth module (split from app.js, shared global scope) */
'use strict';

const AUTH_TOKEN_KEY = 'swarm_auth_token';
// W3.1：登录返回的 token 到期时间（ISO8601）。空=永不过期。前端据此到期前清理并提示重登。
const AUTH_EXPIRES_KEY = 'swarm_auth_expires_at';

let currentUser = null;

function getAuthToken() {
  return localStorage.getItem(AUTH_TOKEN_KEY) || '';
}

function getTokenExpiresAt() {
  return localStorage.getItem(AUTH_EXPIRES_KEY) || '';
}

// W3.1：会话是否已过期（仅当 expires_at 存在且已早于当前时刻）。永不过期 token → false。
function isSessionExpired() {
  const iso = getTokenExpiresAt();
  if (!iso) return false;
  const exp = Date.parse(iso);
  if (isNaN(exp)) return false;
  return Date.now() >= exp;
}

// W3.1：会话过期则清理本地 token 并弹出重登框（带过期提示）。返回是否发生过期。
// 主动调用（轮询/请求前），不必等后端 401，体验更顺。
function enforceSessionExpiry() {
  if (!getAuthToken() || !isSessionExpired()) return false;
  localStorage.removeItem(AUTH_TOKEN_KEY);
  localStorage.removeItem(AUTH_EXPIRES_KEY);
  currentUser = null;
  updateAuthUI();
  showLoginModal('登录已过期，请重新登录');
  return true;
}

function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  const t = getAuthToken();
  if (t) h.Authorization = 'Bearer ' + t;
  return h;
}

// SSE 专用：浏览器原生 EventSource 无法携带自定义请求头，
// 故把 token 作为 query param 附加到 URL（后端 _extract_token 兜底解析）。
function sseUrl(path) {
  const t = getAuthToken();
  if (!t) return path;
  return path + (path.indexOf('?') === -1 ? '?' : '&') + 'token=' + encodeURIComponent(t);
}

function installAuthFetch() {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function swarmFetch(url, opts) {
    opts = opts || {};
    const path = typeof url === 'string' ? url : (url && url.url) || '';
    if (path.startsWith('/api/') && !path.startsWith('/api/auth/login') && !path.startsWith('/api/health')) {
      // W3.1：发请求前先查会话是否过期，过期则主动清理+弹重登（避免无谓 401）。
      enforceSessionExpiry();
      opts.headers = authHeaders(opts.headers);
    }
    return nativeFetch(url, opts).then(function (resp) {
      if (resp.status === 401 && path.startsWith('/api/') && !path.startsWith('/api/auth/login')) {
        showLoginModal();
      }
      return resp;
    });
  };
}

function updateAuthUI() {
  const badge = $('user-badge');
  const btnLogin = $('btn-login');
  const btnLogout = $('btn-logout');
  const hint = $('profile-user-hint');
  if (currentUser) {
    badge.textContent = currentUser.display_name || currentUser.username;
    badge.className = 'pill pill-green';
    btnLogin.classList.add('hidden');
    btnLogout.classList.remove('hidden');
    if (hint) hint.textContent = '用户 ' + currentUser.username + ' · 项目级画像（结构化表单或 Advanced JSON）';
  } else {
    badge.textContent = '未登录';
    badge.className = 'pill pill-gray';
    btnLogin.classList.remove('hidden');
    btnLogout.classList.add('hidden');
  }
  applyRoleVisibility();
}

// A2：按角色显隐导航 tab。系统/设置等管理类 tab 仅 admin 可见。
// RBAC-off 时 currentUser 为 anonymous admin → 全可见（开箱即用）。
function applyRoleVisibility() {
  const admin = !!(currentUser && currentUser.global_role === 'admin');
  document.querySelectorAll('[data-admin-only="1"]').forEach(function (el) {
    el.style.display = admin ? '' : 'none';
  });
  // 非 admin 若当前停留在管理类 tab，回退到项目工作台
  if (!admin) {
    const active = document.querySelector('.nav-tab-top.active[data-admin-only="1"]');
    if (active && typeof switchTopTab === 'function') switchTopTab('workspace');
  }
}

function showLoginModal(message) {
  $('login-overlay').classList.add('open');
  $('login-modal').classList.add('open');
  const errEl = $('login-error');
  // W3.1：过期等场景可带提示文案（如"登录已过期，请重新登录"）。
  if (message && errEl) {
    errEl.textContent = message;
    errEl.style.display = 'block';
  } else if (errEl) {
    errEl.style.display = 'none';
  }
  const userInput = $('login-username');
  if (userInput) setTimeout(function () { userInput.focus(); }, 100);
}

function hideLoginModal() {
  $('login-overlay').classList.remove('open');
  $('login-modal').classList.remove('open');
}

async function submitLogin() {
  const userEl = $('login-username');
  const passEl = $('login-password');
  const errEl = $('login-error');
  const username = ((userEl && userEl.value) || 'admin').trim();
  const password = (passEl && passEl.value) || '';
  errEl.style.display = 'none';
  if (!username) {
    errEl.textContent = '请输入用户名';
    errEl.style.display = 'block';
    if (userEl) userEl.focus();
    return;
  }
  if (!password) {
    errEl.textContent = '请输入密码';
    errEl.style.display = 'block';
    if (passEl) passEl.focus();
    return;
  }
  try {
    const resp = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(function () { return {}; });
      errEl.textContent = data.detail || ('登录失败 (HTTP ' + resp.status + ')');
      errEl.style.display = 'block';
      return;
    }
    const data = await resp.json();
    localStorage.setItem(AUTH_TOKEN_KEY, data.token);
    // W3.1：记录到期时间（永不过期则清除旧值），供前端到期前主动清理重登。
    if (data.expires_at) localStorage.setItem(AUTH_EXPIRES_KEY, data.expires_at);
    else localStorage.removeItem(AUTH_EXPIRES_KEY);
    currentUser = data.user;
    hideLoginModal();
    updateAuthUI();
    showToast('欢迎，' + (currentUser.display_name || currentUser.username), 'success');
    // 12.19：默认弱密码 admin 强制改密。后端仅在使用默认密码时置标志；
    // RBAC 关闭时后端仍可能返回 false（开箱即用不受影响）。
    if (data.must_change_password) {
      await forceChangePassword();
    }
    // 登录成功：触发首屏加载（与 init() 已登录路径一致）
    if (typeof startInitialLoad === 'function') startInitialLoad();
    else await loadProjects();
    if (selectedProjectId && currentTab === 'memory') {
      loadAllMemories(selectedProjectId);
    }
  } catch (e) {
    errEl.textContent = '登录请求失败: ' + (e.message || e);
    errEl.style.display = 'block';
  }
}

// 12.19：强制改密 — 循环直到成功修改（不可取消），保证默认弱密码被替换。
async function forceChangePassword() {
  showToast('检测到默认密码，请立即修改以保证安全', 'warning');
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const oldPw = window.prompt('安全提示：请输入当前密码（默认为 swarm）');
    if (oldPw === null) { showToast('必须修改默认密码后才能继续使用', 'warning'); continue; }
    const newPw = window.prompt('请输入新密码（至少 6 位，不能与旧密码相同）');
    if (newPw === null) { showToast('必须修改默认密码后才能继续使用', 'warning'); continue; }
    if (!newPw || newPw.length < 6) { showToast('新密码至少 6 位', 'error'); continue; }
    try {
      const r = await fetch('/api/auth/change-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_password: oldPw, new_password: newPw }),
      });
      if (r.ok) { showToast('密码已修改，请妥善保管', 'success'); return; }
      const err = await r.json().catch(() => ({}));
      showToast('修改失败: ' + (err.detail || r.status), 'error');
    } catch (e) {
      showToast('修改请求失败: ' + (e.message || e), 'error');
    }
  }
}

function logoutUser() {
  localStorage.removeItem(AUTH_TOKEN_KEY);
  localStorage.removeItem(AUTH_EXPIRES_KEY);
  currentUser = null;
  updateAuthUI();
  showLoginModal();
}

async function refreshCurrentUser() {
  if (!getAuthToken()) {
    currentUser = null;
    updateAuthUI();
    return false;
  }
  try {
    const resp = await fetch('/api/auth/me');
    if (!resp.ok) {
      localStorage.removeItem(AUTH_TOKEN_KEY);
      localStorage.removeItem(AUTH_EXPIRES_KEY);
      currentUser = null;
      updateAuthUI();
      return false;
    }
    currentUser = await resp.json();
    updateAuthUI();
    return true;
  } catch (_) {
    return false;
  }
}

// ─── Constants ───────────────────────────────────────────

// A2：当前用户是否系统管理员（global admin）。RBAC-off 时后端 /api/auth/me 返回
// anonymous admin，故这里也为 true（开箱即用，UI 全功能可见）。
function isAdmin() {
  return !!(currentUser && currentUser.global_role === 'admin');
}
