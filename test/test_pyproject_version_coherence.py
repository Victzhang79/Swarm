"""#29-4 T-3：python 版本假设**六处同源**机读闸。

治的是「多处版本假设各不相同且都不等于运行环境」。当前覆盖的六处：
  ① `.github/workflows/ci.yml` matrix（下界 + 至少两档）
  ② `[tool.ruff] target-version`
  ③ `[tool.pyright] pythonVersion`
  ④ `[project] requires-python`
  ⑤ `setup.sh` 的 `SWARM_PY_MIN_MINOR` + 候选解释器序
  ⑥ `Dockerfile` 的 `FROM python:X.Y`
外加两个 README 的 badge。原状态 py3.11/3.11/3.12 三种取值 + 本地实跑 3.14.6，
跨两个 minor，而**没有任何自动化在看它们是否一致**。

★为什么是六处而不是我最初写的"四处"★（复核 M-1/M-2）：
第一版只盖了 ①~④ 就把文件命名为"四处同源"。复核指出 `setup.sh`（README 推荐的安装
路径，下界还停在 3.11）与 `Dockerfile`（注释自称"对齐 CI"却无人守）都在版本声明面上。
**"N 处同源"这个说法本身必须由闸的覆盖面定义，不能由我当时想到几处定义** ——
否则闸的名字会给出比它实际覆盖更强的保证，这正是 #29 D-3 的教训本体。

★为什么这条闸必须存在（纪律「纪律条文与自动化脚本的清单必须是同一份」）★
CLAUDE.md 里写一句"四处同源"是没有牙的——#29 D-3 已实证同型事故：release.sh 的
`VERSION_FILES` 只有 5 项而纪律说 6 处，README_EN 每次发版必漂一版、滞后 5 版
才被发现。**枚举写在散文里=没有枚举**，所以此处把四个落点写成机读断言。

★本文件断的是「接线事实/单一事实源一致性」，不是实现字面量★（纪律 6 边界）：
它不 getsource 任何生产函数，只比较四份**配置声明**之间的相等关系。
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _ver_tuple(s: str) -> tuple[int, int]:
    """`"3.12"` / `">=3.12"` / `"py312"` → `(3, 12)`。

    ruff 的 `target-version` 用无点形态 `py312`，与另外三处的 `3.12` 不同形；这个
    形态差异本身就是四处容易漂开的原因之一（肉眼比对时 `py311` 与 `3.12` 不像同一个量）。
    """
    s = s.strip()
    m = re.fullmatch(r"py(\d)(\d+)", s)          # ruff: py312 / py3110 不合法，(\d)(\d+) 足够
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d+)\.(\d+)", s)            # 3.12 / >=3.12 / ^3.12
    assert m, f"取不出版本号: {s!r}"
    return (int(m.group(1)), int(m.group(2)))


def _ci_matrix_versions() -> list[str]:
    """从 ci.yml 取 matrix 的 python-version 列表。

    ★为什么用正则而不是 `yaml.safe_load`★：PyYAML 在本仓是**未声明的传递依赖**
    （`pip show` 的 Required-by 全是 langchain 系），且 `brain/smoke_derive.py:258`
    明确按"非声明依赖，try-import"裁决处理它。一条守护 CI 配置的闸不该把自己的
    可运行性押在一个随时可能随上游依赖树变动而消失的包上——那会让本闸从"红"
    退化成"collection error"，而这正是本批要治的「环境相关假绿」的镜像形态。
    """
    txt = CI_YML.read_text(encoding="utf-8")
    m = re.search(r"python-version:\s*\[([^\]]*)\]", txt)
    if not m:
        return []
    return [v.strip().strip("\"'") for v in m.group(1).split(",") if v.strip()]


def test_ci_declares_a_python_matrix():
    """前提自证：CI 必须真的有 matrix。

    ★没有这条，下面三条全是 vacuous★——matrix 键被删掉后 `_ci_matrix_versions()`
    返回 `[]`，`min([])` 抛错固然会红，但 `all(... for v in [])` 这类断言会**空集恒真**。
    本项目「空返回与真没有不可分」的形态，先在这里堵掉。
    """
    vers = _ci_matrix_versions()
    assert len(vers) >= 2, (
        f"ci.yml 的 test job 必须声明 ≥2 档 python matrix，实际 {vers!r}。"
        "单版本 CI 无法自动引爆跨版本 stdlib 行为漂移——本项目已四次实证该族"
        "（is_file/EACCES 两个生产真 bug、PATH 透传、写死路径），v0.9.72 CI 红 8 例即此。"
    )


def test_ruff_and_pyright_target_equals_ci_floor():
    """ruff/pyright 的语言层级 = CI matrix 的**最低**档。

    高于下界 ⇒ lint 放行下界档跑不了的语法（CI 那一档才炸，lint 白守）；
    低于下界 ⇒ lint 按更老的语言层级检查，新语法被误报/合法写法被禁。
    """
    cfg = _pyproject()
    ci_floor = min(_ver_tuple(v) for v in _ci_matrix_versions())
    ruff = _ver_tuple(cfg["tool"]["ruff"]["target-version"])
    pyright = _ver_tuple(cfg["tool"]["pyright"]["pythonVersion"])
    assert ruff == ci_floor, (
        f"[tool.ruff] target-version={ruff} ≠ CI matrix 下界 {ci_floor}。"
        "四处同源：ruff / pyright / requires-python / ci.yml matrix 下界。"
    )
    assert pyright == ci_floor, (
        f"[tool.pyright] pythonVersion={pyright} ≠ CI matrix 下界 {ci_floor}。"
    )


def test_setup_sh_floor_equals_ci_floor():
    """`setup.sh` 的版本下界 = CI 下界（复核 M-1）。

    README 推荐的安装路径就是 setup.sh，它的下界是用户撞到的**第一道门**。
    原状态：候选序含 `python3.11`、门槛 `minor -ge 11` ⇒ 只装 3.11 的机器判绿、
    建出 3.11 venv，到 `pip install -e .` 才炸（安装期报错替代了环境自检报错）。
    """
    sh = (ROOT / "setup.sh").read_text(encoding="utf-8")
    m = re.search(r"SWARM_PY_MIN_MINOR=(\d+)", sh)
    assert m, "setup.sh 里找不到 SWARM_PY_MIN_MINOR —— 版本下界必须是可机读的单一变量"
    floor_minor = int(m.group(1))
    ci_floor = min(_ver_tuple(v) for v in _ci_matrix_versions())
    assert (3, floor_minor) == ci_floor, (
        f"setup.sh 下界 3.{floor_minor} ≠ CI matrix 下界 {ci_floor}")
    # 候选解释器序里不许出现低于下界的（会被优先选中）
    cands = re.search(r"for cmd in ([^;]+); do", sh)
    assert cands, "setup.sh 的候选解释器循环没找到"
    for tok in cands.group(1).split():
        m2 = re.fullmatch(r"python3\.(\d+)", tok)
        if m2:
            assert int(m2.group(1)) >= floor_minor, (
                f"setup.sh 候选序含 {tok}，低于下界 3.{floor_minor} —— "
                "它会被优先选中并建出装不上本包的 venv")


def test_dockerfile_base_equals_ci_floor():
    """Dockerfile 基础镜像的 python 版本 = CI 下界（复核 M-2）。

    `Dockerfile:5` 的注释自称"对齐 CI"，但那句话**没有任何东西在守**。
    今天恰好相等纯属巧合；下次抬下界时它会静默留在旧版本 —— 正是
    「纪律条文与自动化脚本的清单必须是同一份」的原教训本体（#29 D-3）。
    """
    df = ROOT / "Dockerfile"
    # ★复核 N-4★ 原为 `if not df.exists(): pytest.skip(...)`。但 Dockerfile 是 **tracked**
    # 文件（`git ls-files --error-unmatch Dockerfile` 通过），而本批刚在夹具上立的原则是
    # 「tracked 文件不存在=仓库损坏，**绝不 skip 放过**」（三处硬断言）。同一原则同一批
    # 两种处理，且这是"六处同源"里唯一可被 skip 绕过的一处 —— 改成硬断言对齐。
    assert df.exists(), (
        "Dockerfile 不存在。它是 tracked 文件，缺失=仓库损坏，"
        "绝不 skip 放过（否则「六处同源」里这一处可被绕过）")
    vers = set(re.findall(r"FROM\s+python:(\d+\.\d+)", df.read_text(encoding="utf-8")))
    assert vers, "Dockerfile 里找不到 `FROM python:X.Y`"
    ci_floor = min(_ver_tuple(v) for v in _ci_matrix_versions())
    for v in sorted(vers):
        assert _ver_tuple(v) == ci_floor, (
            f"Dockerfile 的 `FROM python:{v}` ≠ CI matrix 下界 {ci_floor}")


def test_requires_python_floor_equals_ci_floor():
    """`requires-python` 声明的下界 = CI 实测下界。

    ★方向性很重要★：声明比实测**低**（原状态 >=3.11 而 CI 只跑 3.12）等于对用户
    承诺一个从未被验证的档位——唯一在守它的是 ruff target-version，而那是个可以被
    一行配置改掉的间接背书。声明比实测**高**则是把能跑的用户挡在外面。
    """
    cfg = _pyproject()
    declared = _ver_tuple(cfg["project"]["requires-python"])
    ci_floor = min(_ver_tuple(v) for v in _ci_matrix_versions())
    assert declared == ci_floor, (
        f"requires-python 下界 {declared} ≠ CI matrix 下界 {ci_floor}："
        "「支持某版本」必须有 CI 背书，否则是无验证声明。"
    )


def test_readme_badges_match_requires_python():
    """两个 README 的 python badge 与 requires-python 同源。

    ★为什么连 README 也要机读断★：#29 D-3 的教训本体就是「README_EN 漏进自动化清单
    ⇒ 每次发版必漂一版、滞后 5 版无人发现」。badge 是用户看到的第一手声明。
    """
    cfg = _pyproject()
    floor = _ver_tuple(cfg["project"]["requires-python"])
    want = f"{floor[0]}.{floor[1]}"
    for name in ("README.md", "README_EN.md"):
        p = ROOT / name
        if not p.exists():
            pytest.fail(f"{name} 不存在——发版版本号 6 处同源清单里有它")
        txt = p.read_text(encoding="utf-8")
        badges = re.findall(r"Python-(\d+\.\d+)%2B", txt)
        assert badges, f"{name} 里找不到 Python badge（形如 `Python-3.12%2B`）"
        for b in badges:
            assert b == want, (
                f"{name} 的 Python badge 写 {b}，但 requires-python 下界是 {want}"
            )
