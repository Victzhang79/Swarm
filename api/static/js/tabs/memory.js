/* Swarm Web UI — tabs/memory module (split from app.js, shared global scope) */
'use strict';

function learningTrendBadge(trend) {
  if (trend === 'improving') {
    return '<span class="pill pill-green" title="近 30 天错题少于前 30 天">改善中</span>';
  }
  if (trend === 'learning') {
    return '<span class="pill pill-teal" title="暂无错题，已积累成功模式（健康）">学习中</span>';
  }
  if (trend === 'stable') {
    return '<span class="pill pill-amber" title="近 30 天错题与前 30 天持平">稳定</span>';
  }
  if (trend === 'regressing') {
    return '<span class="pill pill-red" title="近 30 天错题多于前 30 天">退步</span>';
  }
  return '<span class="pill pill-gray" title="暂无任务数据">未知</span>';
}

function loadAllMemories(projectId) {
  loadProfile(projectId);
  loadMistakes(projectId);
  loadSuccesses(projectId);
  loadSummaries(projectId);
}

function _profileSplitList(text) {
  return (text || '').split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
}

function _profileJoinList(items) {
  if (!Array.isArray(items)) return '';
  return items.filter(Boolean).join('\n');
}

function _profileSplitCsv(text) {
  return (text || '').split(',').map(function (s) { return s.trim(); }).filter(Boolean);
}

function _profileJoinCsv(items) {
  if (!Array.isArray(items)) return '';
  return items.filter(Boolean).join(', ');
}

function profileJsonToForm(profile) {
  profile = profile || {};
  var identity = profile.identity || {};
  var workflow = profile.workflow || {};
  var prefs = profile.preferences || {};
  var tech = profile.tech_stack || {};
  var qb = profile.quality_bar || {};

  var setVal = function (id, val) { var el = $(id); if (el) el.value = val || ''; };
  var setChk = function (id, val) { var el = $(id); if (el) el.checked = !!val; };

  setVal('profile-identity-name', identity.display_name);
  setVal('profile-identity-role', identity.role);
  setVal('profile-responsibilities', workflow.responsibilities || profile.responsibilities || '');
  setChk('profile-wf-review', workflow.review_before_apply);
  setChk('profile-wf-incremental', workflow.prefer_incremental_changes);
  setChk('profile-wf-parallel', workflow.parallel_subtasks);
  setVal('profile-wf-merge-conflict', workflow.on_merge_conflict);
  setVal('profile-wf-test-failure', workflow.on_test_failure);
  setVal('profile-pref-language', prefs.language);
  setVal('profile-pref-test-fw', prefs.test_framework);
  setVal('profile-pref-coding-style', prefs.coding_style);
  setVal('profile-pref-diff-scope', prefs.diff_scope);
  setVal('profile-pref-commit-style', prefs.commit_message_style);
  setVal('profile-comm-response-lang', prefs.response_language);
  var density = $('profile-comm-comment-density');
  if (density) density.value = prefs.comment_density || 'minimal';
  setChk('profile-qb-tests', qb.require_tests_for_logic_changes);
  setChk('profile-qb-lint', qb.lint_before_commit);
  setChk('profile-qb-secrets', qb.no_secrets_in_code);
  setVal('profile-tech-backend', _profileJoinCsv(tech.backend));
  setVal('profile-tech-frontend', _profileJoinCsv(tech.frontend));
  setVal('profile-tech-database', _profileJoinCsv(tech.database));
  setVal('profile-tech-infra', _profileJoinCsv(tech.infra));
  setVal('profile-instructions-brain', _profileJoinList(profile.instructions_for_brain));
  setVal('profile-instructions-worker', _profileJoinList(profile.instructions_for_worker));
  setVal('profile-notes', typeof profile.notes === 'string' ? profile.notes : '');

  var jsonEl = $('profile-json');
  if (jsonEl) {
    jsonEl.value = Object.keys(profile).length ? JSON.stringify(profile, null, 2) : '';
  }
}

function profileFormToJson() {
  var getVal = function (id) { var el = $(id); return el ? el.value.trim() : ''; };
  var getChk = function (id) { var el = $(id); return el ? el.checked : false; };

  var profile = { version: 1 };
  var name = getVal('profile-identity-name');
  var role = getVal('profile-identity-role');
  if (name || role) {
    profile.identity = { display_name: name || undefined, role: role || undefined };
  }

  var workflow = {};
  var resp = getVal('profile-responsibilities');
  if (resp) workflow.responsibilities = resp;
  if (getChk('profile-wf-review')) workflow.review_before_apply = true;
  if (getChk('profile-wf-incremental')) workflow.prefer_incremental_changes = true;
  if (getChk('profile-wf-parallel')) workflow.parallel_subtasks = true;
  var mergeConflict = getVal('profile-wf-merge-conflict');
  if (mergeConflict) workflow.on_merge_conflict = mergeConflict;
  var testFailure = getVal('profile-wf-test-failure');
  if (testFailure) workflow.on_test_failure = testFailure;
  if (Object.keys(workflow).length) profile.workflow = workflow;

  var prefs = {};
  var lang = getVal('profile-pref-language');
  if (lang) prefs.language = lang;
  var testFw = getVal('profile-pref-test-fw');
  if (testFw) prefs.test_framework = testFw;
  var codingStyle = getVal('profile-pref-coding-style');
  if (codingStyle) prefs.coding_style = codingStyle;
  var diffScope = getVal('profile-pref-diff-scope');
  if (diffScope) prefs.diff_scope = diffScope;
  var commitStyle = getVal('profile-pref-commit-style');
  if (commitStyle) prefs.commit_message_style = commitStyle;
  var responseLang = getVal('profile-comm-response-lang');
  if (responseLang) prefs.response_language = responseLang;
  var densityEl = $('profile-comm-comment-density');
  if (densityEl && densityEl.value) prefs.comment_density = densityEl.value;
  if (Object.keys(prefs).length) profile.preferences = prefs;

  var qb = {};
  if (getChk('profile-qb-tests')) qb.require_tests_for_logic_changes = true;
  if (getChk('profile-qb-lint')) qb.lint_before_commit = true;
  if (getChk('profile-qb-secrets')) qb.no_secrets_in_code = true;
  if (Object.keys(qb).length) profile.quality_bar = qb;

  var tech = {};
  var backend = _profileSplitCsv(getVal('profile-tech-backend'));
  if (backend.length) tech.backend = backend;
  var frontend = _profileSplitCsv(getVal('profile-tech-frontend'));
  if (frontend.length) tech.frontend = frontend;
  var database = _profileSplitCsv(getVal('profile-tech-database'));
  if (database.length) tech.database = database;
  var infra = _profileSplitCsv(getVal('profile-tech-infra'));
  if (infra.length) tech.infra = infra;
  if (Object.keys(tech).length) profile.tech_stack = tech;

  var brainIns = _profileSplitList(getVal('profile-instructions-brain'));
  if (brainIns.length) profile.instructions_for_brain = brainIns;
  var workerIns = _profileSplitList(getVal('profile-instructions-worker'));
  if (workerIns.length) profile.instructions_for_worker = workerIns;

  var notes = getVal('profile-notes');
  if (notes) profile.notes = notes;

  return profile;
}

function toggleProfileAdvanced() {
  var toggle = $('profile-advanced-toggle');
  var structured = $('profile-form-structured');
  var advanced = $('profile-form-advanced');
  if (!toggle || !structured || !advanced) return;
  var isAdvanced = toggle.checked;
  if (isAdvanced) {
    var profile = profileFormToJson();
    var jsonEl = $('profile-json');
    if (jsonEl) jsonEl.value = JSON.stringify(profile, null, 2);
    structured.classList.add('hidden');
    advanced.classList.remove('hidden');
  } else {
    var jsonEl = $('profile-json');
    if (jsonEl && jsonEl.value.trim()) {
      try {
        profileJsonToForm(JSON.parse(jsonEl.value.trim()));
      } catch (e) {
        showToast('JSON 无效，无法切回表单: ' + e.message, 'error');
        toggle.checked = true;
        return;
      }
    }
    advanced.classList.add('hidden');
    structured.classList.remove('hidden');
  }
}

async function loadProfile(projectId) {
  if (!projectId) return;
  if (!getAuthToken()) return;
  try {
    var resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/memories/profile');
    if (resp.status === 401) return;
    if (resp.status === 403) {
      profileJsonToForm({});
      showToast('无权限访问用户画像，请联系项目管理员添加成员', 'warning');
      return;
    }
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    var data = await resp.json();
    var profile = data.profile_json || {};
    profileJsonToForm(profile);
  } catch (e) {
    profileJsonToForm({});
    showToast('用户画像加载失败: ' + (e.message || e), 'error');
  }
}

async function saveProfile() {
  if (!selectedProjectId) { showToast('请先选择项目', 'warning'); return; }
  var toggle = $('profile-advanced-toggle');
  var profileJson = {};
  if (toggle && toggle.checked) {
    var el = $('profile-json');
    if (!el) return;
    var raw = el.value.trim();
    if (raw) {
      try {
        profileJson = JSON.parse(raw);
        if (typeof profileJson !== 'object' || profileJson === null || Array.isArray(profileJson)) {
          throw new Error('必须是 JSON 对象');
        }
      } catch (e) {
        showToast('JSON 格式无效: ' + e.message, 'error');
        return;
      }
    }
  } else {
    profileJson = profileFormToJson();
  }
  try {
    var resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/memories/profile', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ profile_json: profileJson }),
    });
    if (!resp.ok) throw new Error('保存失败');
    showToast('用户画像已保存', 'success');
    await loadProfile(selectedProjectId);
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function loadMistakes(projectId) {
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/memories/mistakes');
    const data = await resp.json();
    renderMistakeList(data.mistakes || data || []);
  } catch {
    $('mistake-list').innerHTML = '<p style="font-size:12px;color:var(--text-muted)">加载失败</p>';
  }
}

function renderMistakeList(mistakes) {
  const list = $('mistake-list');
  if (!mistakes.length) {
    list.innerHTML = '<p style="font-size:12px;color:var(--text-muted);padding:8px">暂无错题</p>';
    return;
  }
  list.innerHTML = mistakes.map(m => `
    <div class="card">
      <div class="card-head">
        <h4 class="card-title">${escapeHtml(m.error_type || '错误')}</h4>
        <button class="btn btn-danger btn-sm" onclick="deleteMistake('${m.id}')">删</button>
      </div>
      <div class="card-body">${escapeHtml(m.description || '')}</div>
    </div>`).join('');
}

function toggleAddMistakeForm() { $('add-mistake-form').classList.toggle('hidden'); }

async function submitAddMistake() {
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/memories/mistakes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        error_type: $('mistake-error-type').value.trim(),
        description: $('mistake-description').value.trim(),
        context: $('mistake-context').value.trim(),
        fix_description: $('mistake-fix').value.trim(),
      }),
    });
    if (!resp.ok) throw new Error('提交失败');
    showToast('已添加', 'success');
    toggleAddMistakeForm();
    loadMistakes(selectedProjectId);
  } catch (e) { showToast(e.message, 'error'); }
}

async function deleteMistake(mid) {
  if (!confirm('确定删除？')) return;
  await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/memories/mistakes/' + encodeURIComponent(mid), { method: 'DELETE' });
  loadMistakes(selectedProjectId);
}

async function loadSuccesses(projectId) {
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/memories/successes');
    const data = await resp.json();
    renderSuccessList(data.successes || data || []);
  } catch {
    $('success-list').innerHTML = '<p style="font-size:12px;color:var(--text-muted)">加载失败</p>';
  }
}

function renderSuccessList(successes) {
  const list = $('success-list');
  if (!successes.length) {
    list.innerHTML = '<p style="font-size:12px;color:var(--text-muted);padding:8px">暂无成功模式</p>';
    return;
  }
  list.innerHTML = successes.map(s => `
    <div class="card">
      <div class="card-head">
        <h4 class="card-title">${escapeHtml(s.pattern_name || '模式')}</h4>
        <button class="btn btn-danger btn-sm" onclick="deleteSuccess('${s.id}')">删</button>
      </div>
      <div class="card-body">${escapeHtml(s.description || '')}</div>
    </div>`).join('');
}

function toggleAddSuccessForm() { $('add-success-form').classList.toggle('hidden'); }

async function submitAddSuccess() {
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/memories/successes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        pattern_name: $('success-pattern-name').value.trim(),
        description: $('success-description').value.trim(),
        approach: $('success-approach').value.trim(),
        applicable_when: $('success-applicable').value.trim(),
      }),
    });
    if (!resp.ok) throw new Error('提交失败');
    showToast('已添加', 'success');
    toggleAddSuccessForm();
    loadSuccesses(selectedProjectId);
  } catch (e) { showToast(e.message, 'error'); }
}

async function deleteSuccess(sid) {
  if (!confirm('确定删除？')) return;
  await fetch('/api/projects/' + encodeURIComponent(selectedProjectId) + '/memories/successes/' + encodeURIComponent(sid), { method: 'DELETE' });
  loadSuccesses(selectedProjectId);
}

async function loadSummaries(projectId) {
  try {
    const resp = await fetch('/api/projects/' + encodeURIComponent(projectId) + '/memories/summaries');
    const data = await resp.json();
    renderSummaryList(data.summaries || data || []);
  } catch {
    $('summary-list').innerHTML = '<p style="font-size:12px;color:var(--text-muted)">加载失败</p>';
  }
}

function renderSummaryList(summaries) {
  const list = $('summary-list');
  if (!summaries.length) {
    list.innerHTML = '<p style="font-size:12px;color:var(--text-muted);padding:8px">暂无任务摘要</p>';
    return;
  }
  list.innerHTML = summaries.map(s => `
    <div class="card">
      <div class="card-head">
        <span class="pill pill-gray">#${escapeHtml(String(s.task_id || '').substring(0, 8))}</span>
        ${s.outcome ? `<span class="pill ${s.outcome === 'success' ? 'pill-green' : 'pill-red'}">${escapeHtml(s.outcome)}</span>` : ''}
      </div>
      <div class="card-body">${escapeHtml(s.summary || '')}</div>
      ${s.lessons_learned ? `<p style="font-size:12px;color:var(--purple);margin:8px 0 0">${escapeHtml(s.lessons_learned)}</p>` : ''}
    </div>`).join('');
}

// ─── Init ────────────────────────────────────────────────────
