"""B-4b V-H4：断言 evidence 语料供给侧补 Go/PHP/Rails/C#（27 号文 §3.3 HIGH）。

`assertion_to_probe_cmd` / `evaluate_probe_result` 本身真栈中立，破防全在**语料侧**：
`_accept_design_context` 用 `ROUTE_EVIDENCE_MARKERS` 挑"路由承载段"喂给断言生成 LLM，
挑不中 → 无据可回指 → 合法断言被防臆造闸**确定性判成臆造** → 降 manual →
`acceptance_passed=None` → 不阻断交付（第四道确定性闸对该栈整体失效）。

★用**真实框架源码形态**造 diff 段喂生产函数本体★，不断言 marker 表的字面量（纪律 6）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from swarm.brain.nodes.verify import _accept_design_context  # noqa: E402


def _diff(path: str, body: str) -> str:
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            + "\n".join("+" + ln for ln in body.strip().splitlines()) + "\n")


# 噪声段：**该栈的真源码后缀**且确实不含路由定义（夹具形状要让命题唯一——
# 用 .md/.txt 当噪声会让"只是过滤了非代码文件"这个更弱的命题也成立）
NOISE = {
    "go": _diff("internal/util/str.go",
                "package util\nfunc Trim(s string) string { return s }"),
    "php": _diff("app/Models/User.php",
                 "<?php\nclass User extends Model { protected $table = 'users'; }"),
    "ruby": _diff("app/models/user.rb",
                  "class User < ApplicationRecord\n  validates :email, presence: true\nend"),
    "csharp": _diff("Models/User.cs",
                    "namespace Api.Models;\npublic class User { public int Id { get; set; } }"),
}

ROUTE_SEGS = {
    # ★路径刻意不含 `router.`★ 原写 internal/router/router.go —— 该路径本身命中既有
    # marker `router.`，于是删掉全部 Go marker 这条照旧绿（突变 T1 实证）。
    "go-gin": _diff("internal/server/setup.go",
                    'r := gin.Default()\nr.GET("/api/users", handlers.ListUsers)\n'
                    'r.POST("/api/users", handlers.CreateUser)'),
    "go-echo": _diff("server/routes.go",
                     'e := echo.New()\ne.GET("/api/orders", ListOrders)'),
    "go-chi": _diff("api/mux.go",
                    'r := chi.NewRouter()\nr.Get("/api/items", listItems)'),
    "go-nethttp": _diff("cmd/server/main.go",
                        'http.HandleFunc("/api/health", healthHandler)'),
    # 仅建路由组、方法注册在别文件（真实拆分形态）——故**只有** `.group("/` 能命中；
    # 原夹具带 `v1.GET("/ping")` 会被 `.get("/` 顺带命中，证不了 group token（突变 T7）
    "go-gin-group": _diff("internal/api/groups.go",
                          'v1 := r.Group("/api/v1")\nregisterUserRoutes(v1)'),
    "php-laravel": _diff("routes/api.php",
                         "<?php\nRoute::get('/users', [UserController::class, 'index']);\n"
                         "Route::post('/users', [UserController::class, 'store']);"),
    "rails": _diff("config/routes.rb",
                   "Rails.application.routes.draw do\n  resources :users\n"
                   "  namespace :api do\n    resources :orders\n  end\nend"),
    "csharp-attr": _diff("Controllers/UserController.cs",
                         '[ApiController]\n[Route("api/[controller]")]\npublic class '
                         'UserController : ControllerBase {\n  [HttpGet]\n'
                         '  public IActionResult List() => Ok();\n}'),
    # 约定式路由控制器：**没有** `[Route(...)]`，故只能靠新增的 `[httpget` 命中
    # （上面那条 attr 夹具含 `[Route(` → 被既有 marker `route(` 命中，证不了新 token）
    "csharp-attr-only": _diff("Controllers/OrderController.cs",
                              'public class OrderController : ControllerBase {\n'
                              '  [HttpGet("orders")]\n'
                              '  public IActionResult List() => Ok();\n}'),
    "csharp-minimal": _diff("Program.cs",
                            'app.MapGet("/api/users", () => Results.Ok());'),
}


# 路由段 → 同栈噪声段的键（显式映射，别从名字前缀推——`rails` 的噪声键是 `ruby`）
NOISE_KEY = {"go-gin": "go", "go-echo": "go", "go-chi": "go", "go-nethttp": "go",
             "go-gin-group": "go", "php-laravel": "php", "rails": "ruby",
             "csharp-attr": "csharp", "csharp-attr-only": "csharp",
             "csharp-minimal": "csharp"}


@pytest.mark.parametrize("name", sorted(ROUTE_SEGS))
def test_route_segment_is_selected_into_evidence(name):
    """★V-H4 本尊★ 各栈真实路由定义段必须被选进 evidence（且走"优选"分支，非盲切回退）。

    夹具刻意把**噪声段放在前面**：若路由段选不中，`_picked` 为空 → 回退 `diff[:4000]` 盲切，
    大 diff 下路由定义就落在 4000 字符之外 = 防臆造纪律空转（原病）。
    突变判据：删掉该栈的 marker，对应参数化用例必红。
    """
    lang = NOISE_KEY[name]
    diff = NOISE[lang] * 3 + ROUTE_SEGS[name]
    ctx = _accept_design_context({"merged_diff": diff}, None)
    assert "路由/接口承载段优选" in ctx, (
        f"{name} 路由段未被选中 → 回退盲切（断言将无据可回指）\n{ctx[:400]}")
    assert "diff --git" in ctx


@pytest.mark.parametrize("lang", sorted(NOISE))
def test_pure_noise_does_not_hit_route_markers(lang):
    """★对照臂（区分力）★ 同栈的**非路由**源码段不得命中 marker。

    没有这条，"marker 表匹配一切"的错实现也能满足上面全部用例——而那会把真路由段挤出
    6000 预算（误杀方向，比漏更隐蔽）。
    """
    ctx = _accept_design_context({"merged_diff": NOISE[lang] * 3}, None)
    assert "路由/接口承载段优选" not in ctx, (
        f"{lang} 纯噪声段命中了路由 marker（表过宽 → 真路由段会被挤出预算）")


def test_route_segment_wins_budget_against_many_noise_segments():
    """预算竞争：噪声再多也不许把路由段挤出——`_route_segs` 先过滤再吃预算。"""
    diff = "".join(NOISE.values()) * 6 + ROUTE_SEGS["rails"]
    ctx = _accept_design_context({"merged_diff": diff}, None)
    assert "路由/接口承载段优选" in ctx
    assert "resources :users" in ctx, "路由段被噪声挤出了 evidence"


def test_jvm_markers_still_work():
    """对照臂：JVM（唯一跑过 E2E 的栈）原有 marker 一条都没被本批改动破坏。"""
    diff = _diff("src/main/java/com/x/UserController.java",
                 '@RestController\n@RequestMapping("/api/users")\npublic class '
                 'UserController {\n  @GetMapping\n  public List<User> list() { return null; }\n}')
    ctx = _accept_design_context({"merged_diff": diff}, None)
    assert "路由/接口承载段优选" in ctx


# ══════════════════════════════════════════════
# F-1 / F-7（hunter 复核）：命中型噪声才会真的抢预算
# ══════════════════════════════════════════════

# ★这些**命中** marker 却不是路由定义★——HTTP 客户端调用与路由注册字面同形
# （区别在接收者是路由器还是 http client，靠字面无法区分）。原"预算竞争"用例用的噪声
# （Model 类/util 函数）命中零个 marker，在过滤阶段就被剔掉、从没进过预算循环，
# 所以它证明的是"过滤在预算之前"，不是"噪声挤不出路由段"。F-1 正是从这个缺口漏过去的。
HITTING_NOISE = {
    "axios": _diff("src/api/userClient.ts",
                   "\n".join(f'export const get{i} = () => axios.get("/api/thing{i}");'
                             for i in range(60))),
    "supertest": _diff("tests/api/user.spec.ts",
                       "\n".join(f"await request(app).get('/users/{i}');"
                                 for i in range(60))),
    # 每段必须 > 单段预算上限 2000，三段才吃满 6000——原来 httpx 段只 1829 字符，
    # 剩 171 字节仍够塞进 118 字符的路由段，于是突变 H1a 照旧绿（夹具没造出真实压力）。
    "httpx": _diff("tests/test_api.py",
                   "\n".join(f'r = client.get("/items/{i}", timeout=30)  # case {i}'
                             for i in range(60))),
}


def test_client_call_noise_does_not_crowd_out_real_route_segment():
    """★F-1（HIGH 误杀，实证）★ 客户端调用段吃光预算 → 真路由段一个字进不去 evidence。

    段序按路径：`src/api/` < `src/routes/`、`tests/` 亦在前 → 先到先得下路由段饿死
    → 断言无据可回指 → 合法断言被判臆造 → 第四道闸对该任务整体 fail-open。
    治法＝特异档优先吃预算、宽档补位。
    突变判据：把 `_specific + _broad_only` 改回单一 `any(... ROUTE_EVIDENCE_MARKERS)`，本条必红。
    """
    diff = "".join(HITTING_NOISE.values()) + ROUTE_SEGS["csharp-minimal"]
    ctx = _accept_design_context({"merged_diff": diff}, None)
    assert "路由/接口承载段优选" in ctx
    # ★无 `or` 逃生门★ 原写 `'…/api/orders' in ctx or "mapget" in ctx.lower()`——夹具里是
    # `/api/users`，第一子句恒 False，整条靠 or 右支恒真（H-2 同一个坑，突变 H1a 当场暴露）。
    assert 'app.MapGet("/api/users"' in ctx, (
        f"真路由段被客户端调用段挤出了 evidence（F-1 复发）；ctx 长度={len(ctx)}")


def test_specific_markers_outrank_broad_ones_in_budget():
    """特异档（`mapget(`/`resources :`/`@getmapping`）必须排在宽档（`.get("/`）之前。

    夹具：命中型噪声 ×3（只命中宽档）+ Rails 路由段（只命中特异档，且排在最后）。
    """
    diff = "".join(HITTING_NOISE.values()) + ROUTE_SEGS["rails"]
    ctx = _accept_design_context({"merged_diff": diff}, None)
    assert "resources :users" in ctx, "特异档没有优先吃到预算"


def test_broad_markers_still_recall_gin_when_no_competition():
    """宽档不是被删掉、只是排在后面：无竞争时 Gin 的 `r.GET("/x")` 照旧进 evidence。"""
    ctx = _accept_design_context({"merged_diff": ROUTE_SEGS["go-gin"]}, None)
    assert "路由/接口承载段优选" in ctx
    assert 'r.GET("/api/users"' in ctx


def test_specific_tier_holds_only_low_ambiguity_tokens():
    """★I-1（reviewer 复核）★ 反向：真路由段只命中**宽档**、噪声只命中**特异档**时也不许饿死。

    我第一版把整张旧表原封不动塞进"特异档"，但其中 `path(`/`path:`/`router.`/`route(`/
    `routes:` 并非零歧义（`Path(__file__)`、Go `ConfigPath:`、`router.push('/login')`、
    Laravel 的 `route('users.index')` 全命中）→ config/util 段抢在 Gin/Echo/Chi 之前吃满预算
    → **恰好抵消 V-H4 对这些栈的修复**。原 `test_specific_markers_outrank_broad_ones_in_budget`
    是反方向（路由段在特异档、噪声在宽档），测不到这一面。
    突变判据：把那 5 个 token 移回 `_ROUTE_MARKERS_SPECIFIC`，本条必红。
    """
    # 噪声：Go 配置段，只命中（曾经的）特异档 `path:`，且每段撑过 2000 字符
    noise = "".join(
        _diff(f"internal/config/cfg{n}.go",
              # ★字面必须是 `Path:`★ 原写 `ConfigPath{i}:` → 小写后是 `configpath0:`，
              # **数字把 `path:` 这个 token 隔断了**，夹具从未命中它要命中的东西（突变 R-I1b 证实）
              "\n".join(f'\tConfigPath: "/etc/app/conf{i}.yaml",  // entry {i}'
                        for i in range(70)))
        for n in range(3))
    # 真路由段：Gin，只命中宽档 `.get("/`
    diff = noise + ROUTE_SEGS["go-gin"]
    ctx = _accept_design_context({"merged_diff": diff}, None)
    assert "路由/接口承载段优选" in ctx
    assert 'r.GET("/api/users"' in ctx, (
        f"真 Gin 路由段被配置段（含 `Path:`）挤出 evidence —— V-H4 对 Gin/Echo/Chi 的修复被"
        f"分档抵消（I-1 复发）；ctx 长度={len(ctx)}")


def test_ambiguous_legacy_tokens_are_not_in_specific_tier():
    """接线事实：那 5 个歧义 token 必须不在特异档（断的是档位归属，不是字面量清单）。"""
    from swarm.brain.nodes.verify import _ROUTE_MARKERS_SPECIFIC as SP
    for probe in ["Path(__file__).parent", 'ConfigPath: "/etc/app"',
                  "router.push('/login')", "route('users.index')"]:
        assert not [m for m in SP if m in probe.lower()], (
            f"特异档命中了非路由代码：{probe} → {[m for m in SP if m in probe.lower()]}")
