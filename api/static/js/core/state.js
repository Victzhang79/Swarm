/* Swarm Web UI — core/state module (split from app.js, shared global scope) */
'use strict';

const COMPONENT_DEFS = [
  { name: 'Brain 状态机' },
  { name: 'Worker 执行器' },
  { name: '知识库' },
  { name: '记忆系统' },
  { name: '远程沙箱' },
  { name: '模型路由' },
  { name: 'PostgreSQL' },
  { name: 'Qdrant' },
];

const PIPELINE_NODES = ['analyze', 'plan', 'dispatch', 'merge', 'verify', 'deliver', 'learn'];

const NODE_MAP = {
  analyze: 'analyze',
  plan: 'plan',
  validate_plan: 'plan',
  confirm: 'plan',
  confirm_plan: 'plan',
  dispatch: 'dispatch',
  monitor: 'dispatch',
  revision: 'dispatch',
  handle_failure: 'dispatch',
  merge: 'merge',
  verify_l2: 'verify',
  deliver: 'deliver',
  learn_success: 'learn',
  learn_failure: 'learn',
};

const TASK_STATUS_PILLS = {
  SUBMITTED: 'pill-gray',
  ANALYZING: 'pill-blue',
  PLANNING: 'pill-blue',
  VALIDATING_PLAN: 'pill-blue',
  CONFIRMING: 'pill-amber',
  DISPATCHING: 'pill-blue',
  MONITORING: 'pill-blue',
  HANDLING_FAILURE: 'pill-red',
  MERGING: 'pill-purple',
  VERIFYING_L2: 'pill-purple',
  VERIFYING_RUNTIME: 'pill-purple',
  DELIVERING: 'pill-green',
  IN_REVISION: 'pill-orange',
  LEARNING_SUCCESS: 'pill-teal',
  LEARNING_FAILURE: 'pill-teal',
  FAILED: 'pill-red',
  CANCELLED: 'pill-gray',
  DONE: 'pill-green',
};

const ACTIVE_STATUSES = new Set([
  'SUBMITTED', 'PENDING', 'ANALYZING', 'CLARIFYING', 'DESIGN_REVIEW',
  'PLANNING', 'VALIDATING_PLAN', 'CONFIRMING', 'DISPATCHING',
  'MONITORING', 'HANDLING_FAILURE', 'MERGING', 'VERIFYING_L2', 'VERIFYING_RUNTIME', 'VERIFYING_L3',
  'DELIVERING', 'IN_REVISION', 'LEARNING_SUCCESS', 'LEARNING_FAILURE',
]);

// 任务状态 → 当前所处的 pipeline 阶段（用于选中任务时回放进度，不依赖实时 SSE）。
// 值为该状态对应「正在进行」的 pipeline 节点；其之前的节点都标记为 done。
const STATUS_TO_PIPELINE_NODE = {
  SUBMITTED: 'analyze',
  ANALYZING: 'analyze',
  PLANNING: 'plan',
  VALIDATING_PLAN: 'plan',
  CONFIRMING: 'plan',
  DISPATCHING: 'dispatch',
  MONITORING: 'dispatch',
  HANDLING_FAILURE: 'dispatch',
  IN_REVISION: 'dispatch',
  MERGING: 'merge',
  VERIFYING_L2: 'verify',
  VERIFYING_RUNTIME: 'verify',
  DELIVERING: 'deliver',
  LEARNING_SUCCESS: 'learn',
  LEARNING_FAILURE: 'learn',
};

// 终态：全部 7 步亮起（done）或失败定位。
const TERMINAL_DONE_STATUSES = new Set(['DONE']);
const TERMINAL_FAIL_STATUSES = new Set(['FAILED', 'CANCELLED']);

// ─── State ───────────────────────────────────────────────

let statusInterval = null;

let pollersStarted = false;

let taskEventSource = null;

// taskEventSource 当前所属 task id（P1-4：双活跃任务切换须按"流所属任务"判断重开/关闭，
// 仅判 truthy 会让旧任务的流继续往共享面板写造成跨任务串扰）
let taskSSETaskId = null;

let workerEventSource = null;

let workerRunId = null;

let preprocessSSE = null;


let originalConfig = {};

let modelLists = { siliconflow: [], local: [], byProvider: {}, all: [] };

let projects = [];

let selectedProjectId = null;

let tasks = [];

let selectedTaskId = null;

let selectedTaskDetail = null;

let currentTab = 'tasks';

let currentDetailTab = 'overview';

let reviseTargetTaskId = null;

let logEntries = [];

let selectedSandboxId = null;

let sandboxCurrentPath = '/workspace';

let sandboxSelectedFile = null;

let workerLastDiff = '';

let systemStatsInterval = null;

// 通知铃铛状态
let notifPanelOpen = false;
let notifUnreadCount = 0;

const PROJECT_STORAGE_KEY = 'swarm_selected_project_id';

let sseRefreshTimer = null;

const PREPROCESS_PHASE_ORDER = ['scanning', 'indexing', 'embedding', 'analyzing', 'complete'];

const ROUTING_TIER_DEFS = [
  { key: 'trivial', label: '简单 trivial', hint: '改配置 / 小修复 → 本地小模型' },
  { key: 'medium', label: '中等 medium', hint: '单模块开发 → 本地代码模型' },
  { key: 'complex', label: '复杂 complex', hint: '跨模块 / 架构 → 云端大模型' },
  { key: 'multimodal', label: '多模态 multimodal', hint: '看图 / UI → 视觉模型' },
];

let normEditingId = null;
