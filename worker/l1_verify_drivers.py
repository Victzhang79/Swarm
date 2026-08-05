"""L1 验收命令归一的**栈驱动层**（X-M1；27 号文 B-5 BuildDriver 验收臂）。

## 治的是什么

`_reactorize_verify_command`（R65E8-T1）只归一 Maven——同型假阴性在其它栈全原样返回：

| 形态 | 死因 |
|---|---|
| `cd <子项目> && ./gradlew test` | gradlew wrapper 只在工程根 → 127 命令未找到 |
| workspace 根裸 `npm test` / `npm run <s>` | 根 package.json 无该 script → "Missing script" |
| go.work 根裸 `go test ./...`（根无 go.mod） | "current directory is not in a module" |

exit≠0 → 假阴性烧正确代码的重试预算 → abandon 连坐——正是 round65e8 在 Maven 上
坐实的死法，换栈复发（27 号文 X-M1 实测三形全原样）。

## 为什么是驱动层而不是「再加三条正则」

三栈的**证据源**完全不同（settings.gradle include / package.json scripts+锚点 /
go.work+根 go.mod 有无），但横切不变量只有一条，留在共用层：

★**只改写 positively-known 形态，其余一律原样**★——与 Maven 臂同契约（复合命令/
带选项/未注册目录/锚点不定 → 原样，绝不臆改）。改写必然是「等价的另一种写法」，
不是「看起来更好的写法」。

## Maven 不在本模块

`_reactorize_verify_command` 的 Maven 臂是唯一跑过 E2E 的栈，一字不动（与
l1_error_drivers 的 JVM 薄适配器同理）——本模块只收编**非 Maven** 命令，
由 `_reactorize_verify_command` 入口分派（调用点不变，签名不变）。

## IO 注入

本模块不直接 import l1_pipeline（反向依赖）：沙箱感知的文件读/存在性/锚点反查
由调用方注入（`VerifyIO`），与 l1_error_drivers 的 RunProbe 同构。本地盘模式
与沙箱模式同一条路径，零分叉。
"""
from __future__ import annotations

import json
import logging
import re
import shlex
from typing import Callable, NamedTuple, Protocol

logger = logging.getLogger(__name__)


class VerifyIO(NamedTuple):
    """沙箱感知 IO（由 l1_pipeline 注入，本地/沙箱同路径）。"""
    read_file: Callable[[str, str], "str | None"]       # (project_path, rel) -> 文本|None
    file_exists: Callable[[str, str], bool]             # (rel, project_path) -> bool
    anchor_for: Callable[[list, tuple, str], "str | None"]  # _manifest_dir_for：改动文件→最近清单目录


class VerifyDriver(Protocol):
    """一条验收命令归一臂。try_normalize 返回新命令；不适用/不确定 → None（原样）。"""
    name: str

    def try_normalize(self, command: str, project_path: str, pl_basis: list[str],
                      io: VerifyIO) -> "str | None": ...


def _norm_cd_dir(raw: str) -> "str | None":
    """cd 目标归一：剥引号/./尾斜杠、反斜杠归一；`..` 逃逸段 → None（绝不映射）。"""
    d = raw.strip().strip('"\'').replace("\\", "/").removeprefix("./").rstrip("/")
    if not d or ".." in d.split("/"):
        return None
    return d


# ── Gradle：cd <注册子项目> && ./gradlew <纯任务名…> → 根 ./gradlew :<proj>:<task>… ──

_CD_GRADLEW_RE = re.compile(
    r"^\s*cd\s+(?P<dir>[^\s&|;]+)\s*&&\s*(?P<rest>\./gradlew(?:\s+\S+)*)\s*$",
    re.DOTALL)
# 纯任务名（不含 -/-- 选项、不含 :——已带工程路径说明作者已知形态，不臆改）
_GRADLE_TASK_RE = re.compile(r"^[A-Za-z][\w.-]*$")


class _GradleCdWrapperDriver:
    """`cd <sub> && ./gradlew <tasks>`：wrapper 脚本只在根 → 127。

    归一=剥 cd、任务逐个挂 `:<sub>:` 前缀在根跑（与 Maven 臂 `cd mod && mvn` →
    `mvn -pl mod` 同构）。证据=根 settings.gradle(.kts) 的 include 注册表
    （`manifest_member_probes` 三面同源单一事实源）：cd 目标【不是】注册子项目
    （独立工程/脚本目录，可能自带 wrapper）→ 原样。
    """
    name = "gradle_cd_wrapper"

    def try_normalize(self, command, project_path, pl_basis, io):
        m = _CD_GRADLEW_RE.match(command)
        if m is None:
            return None
        d = _norm_cd_dir(m.group("dir"))
        if d is None:
            return None
        tokens = m.group("rest").split()
        tasks = tokens[1:]  # 剥 ./gradlew
        if not tasks or any(not _GRADLE_TASK_RE.match(t) for t in tasks):
            return None  # 带选项/已带工程路径/无任务 → 不臆改
        from swarm.worker.workspace_manifest import manifest_member_probes
        settings = settings_name = None
        for name in ("settings.gradle", "settings.gradle.kts"):
            settings = io.read_file(project_path, name)
            if settings is not None:
                settings_name = name
                break
        if settings is None:
            return None
        probes = manifest_member_probes(settings_name, settings)
        proj_token = next(
            (tok for tok, probe in probes if probe == d), None)
        if proj_token is None:
            # settings 在场但 probes 空（动态 include/多 token 行不收）与「cd 目标真未
            # 注册」在返 None 上不可分——留 debug 痕（证据不足静默，fail-closed 方向）。
            if not probes:
                logger.debug(
                    "[L1.3.5] X-M1 gradle：%s 解析不出静态 include（动态/多 token 行）"
                    "→ %r 注册性不可证，原样", settings_name, command)
            return None  # 非注册子项目/不可证 → 原样（血规：绝不猜工程路径）
        scoped = "./gradlew " + " ".join(f":{proj_token}:{t}" for t in tasks)
        logger.info(
            "[L1.3.5] X-M1 验收命令归一（gradle）：%r → %r"
            "（cd 子项目裸 ./gradlew=127，wrapper 只在工程根）", command, scoped)
        return scoped


# ── npm：workspace 根裸 npm test/run <s>，根无该 script + 锚点包有 → --prefix ──

_NPM_BARE_RE = re.compile(r"^\s*npm\s+(?P<form>test|run\s+(?P<script>[\w:.-]+))\s*$")


def _pkg_scripts(text: "str | None") -> "set[str] | None":
    """package.json 的 scripts 键集；解析失败 → None（与「没有 scripts」可辨，fail-closed）。"""
    if text is None:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    scripts = obj.get("scripts") if isinstance(obj, dict) else None
    return set(scripts) if isinstance(scripts, dict) else set()


class _NpmBareScriptDriver:
    """workspace 根裸 `npm test`/`npm run <s>`：根 package.json 无该 script → Missing script。

    归一=`npm --prefix <锚点包> run <s>`。锚点=改动文件向上最近的 package.json 目录
    （`_manifest_dir_for`，与 build_cmd 派生同源的锚定判据）——positively-known，
    绝不枚举 workspace 成员猜「谁是主包」。根有该 script / 锚点不定 / 锚点包也没有
    → 原样。
    """
    name = "npm_bare_script"

    def try_normalize(self, command, project_path, pl_basis, io):
        m = _NPM_BARE_RE.match(command)
        if m is None:
            return None
        script = m.group("script") or "test"
        root_text = io.read_file(project_path, "package.json")
        root_scripts = _pkg_scripts(root_text)
        if root_scripts is None:
            # 「根清单缺席/不可读」与「根清单在场但 JSON 损坏」分档（R1 hunter F4：
            # 证据不足静默≠形态不适用静默）——后者 WARNING（证据坏了，可能漏归一），
            # 前者 debug（非 npm 工程是合法形态）。
            if root_text is not None:
                logger.warning(
                    "[L1.3.5] X-M1 npm：根 package.json JSON 解析失败→script 有无"
                    "不可证，命令原样（可能漏归一）: %r", command)
            else:
                logger.debug("[L1.3.5] X-M1 npm：根 package.json 缺席/不可读→原样: %r",
                             command)
            return None
        if script in root_scripts:
            return None  # 根本来就能跑 → 原样
        anchor = io.anchor_for(list(pl_basis or []), ("package.json",), project_path)
        if not anchor:
            return None  # 锚点不定/在根 → 不猜
        member_scripts = _pkg_scripts(io.read_file(project_path, f"{anchor}/package.json"))
        if not member_scripts or script not in member_scripts:
            return None
        scoped = f"npm --prefix {anchor} run {script}"
        logger.info(
            "[L1.3.5] X-M1 验收命令归一（npm）：%r → %r"
            "（根 package.json 无 %r script=Missing script 假阴性）", command, scoped, script)
        return scoped


# ── Go：go.work 根（无根 go.mod）裸 go test [flags] ./... → cd <锚点模块> && ──

_GO_TEST_HEAD_RE = re.compile(r"^\s*go\s+test\s+(?P<tail>.*)$", re.DOTALL)

# bare flag 取值/布尔全表——★权威来源：pkg.go.dev/cmd/go（Testing flags + Build flags）
# 与 `go help testflag`（Go 1.21+ 稳定集）★（纪律：声称穷举必须指出权威来源）。
# `-flag=value` 自足形不在此限（任意 flag 名都收——改写是【逐字复制】原命令，
# 不臆判 flag 语义）；bare 形必须分清取值/布尔——取值 flag 误判成布尔会把它的值
# 当包模式错位。诚实边界：表外 bare flag（含未来 Go 版本新增）→ 原样 fail-closed，
# 代价=覆盖面损失，绝不是错误改写。
_GO_VALUE_FLAGS = frozenset({
    # go help testflag 取值档
    "-bench", "-benchtime", "-blockprofile", "-blockprofilerate", "-count",
    "-coverprofile", "-cpu", "-fuzz", "-fuzzminimizetime", "-fuzztime", "-list",
    "-memprofile", "-memprofilerate", "-mutexprofile", "-mutexprofilefraction",
    "-outputdir", "-parallel", "-run", "-shuffle", "-skip", "-timeout", "-trace",
    # go test 命令取值档
    "-exec", "-o", "-vet",
    # build flags 取值档（test 共享）。★诚实边界★ `-C <dir>` 改变 go 进程工作目录，
    # 与归一前缀 `cd <anchor>` 叠加后语义可能偏移——验收命令带 -C 极罕见，
    # 且逐字复制绝不改坏原命令（R2 reviewer/hunter 共同登记）。
    "-C", "-p", "-asmflags", "-buildmode", "-buildvcs", "-compiler",
    "-covermode", "-coverpkg", "-gccgoflags", "-gcflags", "-installsuffix",
    "-ldflags", "-mod", "-modfile", "-overlay", "-pgo", "-pkgdir", "-tags",
    "-toolexec",
})
_GO_BOOL_FLAGS = frozenset({
    # go help testflag 布尔档
    "-failfast", "-fullpath", "-paniconexit0", "-short", "-v",
    # go test 命令布尔档
    "-c", "-json",
    # build flags 布尔档（test 共享）
    "-a", "-asan", "-cover", "-linkshared", "-modcacherw", "-msan", "-n",
    "-race", "-trimpath", "-work", "-x",
})


def _go_test_flags_ok(tokens: list[str]) -> bool:
    """`go test` 与末尾 `./...` 之间的 token 是否全是【可辨识】flag（逐字复制才安全）。"""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if not t.startswith("-") or t == "-args":
            return False  # 包模式/-args 后全交测试二进制 → 不臆改
        if "=" in t:
            i += 1          # -flag=value 自足
            continue
        if t in _GO_VALUE_FLAGS:
            i += 2          # 取值 flag：值在下一 token（缺值=go 自身会报错 → 不臆改）
            if i > len(tokens):
                return False
            continue
        if t in _GO_BOOL_FLAGS:
            i += 1
            continue
        return False      # 未知 bare flag → fail-closed
    return i == len(tokens)


class _GoWorkspaceTestDriver:
    """go.work workspace 根（根无 go.mod）裸 `go test [flags] ./...` → 目录不在任何模块内。

    归一=`cd <锚点模块> && <原命令逐字>`。锚点=改动文件向上最近的 go.mod
    目录（positively-known：go.mod 真实存在才锚）；根有 go.mod（普通单模块工程
    顺手带个 go.work）/锚点不定/flag 形态不可辨识 → 原样。
    """
    name = "go_workspace_test"

    def try_normalize(self, command, project_path, pl_basis, io):
        m = _GO_TEST_HEAD_RE.match(command)
        if m is None:
            return None
        try:
            # shlex 分词（R2 reviewer F-5）：`-ldflags '-X main.v=1'` /
            # `-tags "integration e2e"` 带引号空格取值是生产常见形态，str.split
            # 会把值切碎→误判「非 flag token」→ fail-closed 漏改。只改识别路径，
            # 输出仍是原命令逐字。
            tokens = shlex.split(m.group("tail"))
        except ValueError:
            return None  # 引号不配对等畸形 → 不臆改
        if not tokens or tokens[-1] != "./..." or not _go_test_flags_ok(tokens[:-1]):
            return None
        if not io.file_exists("go.work", project_path):
            return None
        if io.file_exists("go.mod", project_path):
            return None  # 根是模块 → 裸跑本就合法
        anchor = io.anchor_for(list(pl_basis or []), ("go.mod",), project_path)
        if not anchor:
            return None
        scoped = f"cd {anchor} && {command.strip()}"  # 原命令逐字复制，绝不重排 flag
        logger.info(
            "[L1.3.5] X-M1 验收命令归一（go）：%r → %r"
            "（go.work 根无 go.mod，./... 解析不到模块=假阴性）", command, scoped)
        return scoped


# ★驱动注册表=分派单一事实源（纪律 1/6：栈相关行为走 registry，测试断注册表）★
_VERIFY_DRIVERS: tuple = (
    _GradleCdWrapperDriver(),
    _NpmBareScriptDriver(),
    _GoWorkspaceTestDriver(),
)


def normalize_verify_command(command: str, project_path: str, pl_basis: list[str],
                             io: VerifyIO, details: dict | None = None) -> str:
    """非 Maven 验收命令归一入口：逐驱动试配，首个命中的改写胜出；全不适用 → 原样。

    异常永不外抛（读文件/解析失败=「不确定」→ 原样），绝不炸 L1 主链；但异常会写入
    `details["verify_driver_exceptions"]`，使"驱动失效"与"驱动不适用"机读可辨（W-2）。
    """
    if not command:
        return command
    for driver in _VERIFY_DRIVERS:
        try:
            out = driver.try_normalize(command, project_path, pl_basis, io)
        except Exception as exc:  # noqa: BLE001 — 驱动内部故障=不确定 → 原样（fail-open 方向安全）
            logger.warning("[L1.3.5] X-M1 驱动 %s 异常，命令原样放行: %r",
                           driver.name, command, exc_info=True)
            if details is not None:
                details.setdefault("verify_driver_exceptions", []).append({
                    "driver": driver.name,
                    "command": command,
                    "exception": f"{type(exc).__name__}: {exc}",
                })
            continue
        if out is not None:
            return out
    return command
