/* Swarm Web UI — core/auth module (split from app.js, shared global scope) */
'use strict';

const AUTH_TOKEN_KEY = 'swarm_auth_token';

let currentUser = null;

function getAuthToken() {
  return localStorage.getItem(AUTH_TOKEN_KEY) || '';
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
}

function showLoginModal() {
  $('login-overlay').classList.add('open');
  $('login-modal').classList.add('open');
  $('login-error').style.display = 'none';
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
    currentUser = data.user;
    hideLoginModal();
    updateAuthUI();
    showToast('欢迎，' + (currentUser.display_name || currentUser.username), 'success');
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

function logoutUser() {
  localStorage.removeItem(AUTH_TOKEN_KEY);
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
