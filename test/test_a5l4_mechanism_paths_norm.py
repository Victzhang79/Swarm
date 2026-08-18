"""32 号文 A5-L4：`_mechanism_paths` 的 `lstrip("./")` 归一（判据 C 同族最后一处漏网）。

**病根**：`lstrip("./")` 剥的是**字符集合**而非前缀 ⇒ `.mvn/wrapper/x` → `mvn/wrapper/x`。
本处的串直接喂 provenance 匹配（告诉 reviewer"这些路径是确定性修复层写的、不是 worker 写的"）
⇒ 归一错就匹配不上 ⇒ 该文件被当成 worker 产出 ⇒ **正是 `_mechanism_paths` 当初要治的
四轮冤杀 + 死循环**。所以这条虽登记为 LOW，后果落在它自己要防的那件事上。

★为什么锁在这里而不是只改代码★ 判据 C 是个**族**（全仓活代码 23 处 / 7 文件，机器分类过），
本处治完只是第一处。给它配锁，让"下一个维护者把它改回 lstrip"这件事必红。
"""
from __future__ import annotations

from swarm.types import Confidence, WorkerOutput


def _wo(paths: list[str]) -> WorkerOutput:
    return WorkerOutput(
        subtask_id="st-1", diff="", files_changed=[], summary="",
        confidence=Confidence.HIGH,
        l1_details={"repaired_file_paths": paths},
    )


def test_dot_prefixed_dirs_survive_normalization():
    """★核心锁★ 前导点目录（`.mvn/` `.github/` `.yarn/`）不得被剥成普通目录。

    这三个在真实工程里都是**承重**目录：`.mvn/wrapper/maven-wrapper.properties` 决定用哪个
    Maven 版本、`.yarn/releases` 决定 yarn 版本。被剥掉首字符后路径在盘上不存在，
    provenance 匹配失败。
    """
    from swarm.brain.nodes.adversarial import _mechanism_paths

    out = _mechanism_paths(_wo([
        ".mvn/wrapper/maven-wrapper.properties",
        ".github/workflows/ci.yml",
        ".yarn/releases/yarn-3.6.0.cjs",
    ]))
    assert out == [
        ".mvn/wrapper/maven-wrapper.properties",
        ".github/workflows/ci.yml",
        ".yarn/releases/yarn-3.6.0.cjs",
    ], (
        "★前导点被剥掉了★ `lstrip('./')` 剥字符集合而非前缀。"
        f"实得 {out}"
    )


def test_leading_dotslash_is_still_stripped():
    """`./` 前缀仍必须剥掉（这是本函数原本的正当职责，别治过头）。

    ★配对锁★ 只有上一条时，把归一整块删掉也能全绿——那样 `./a/b.java` 与 `a/b.java`
    会被当成两个不同路径，provenance 同样匹配不上（反方向的同一个 bug）。
    """
    from swarm.brain.nodes.adversarial import _mechanism_paths

    out = _mechanism_paths(_wo(["./src/A.java", ".//src/B.java"]))
    assert out == ["src/A.java", "src/B.java"], (
        f"前导 `./` 必须剥（含重复形态 `.//`），实得 {out}"
    )


def test_backslash_normalized_and_dedup_preserved():
    """反斜杠归一与去重（既有行为，改动不得破坏）。"""
    from swarm.brain.nodes.adversarial import _mechanism_paths

    out = _mechanism_paths(_wo(["src\\A.java", "src/A.java", "./src/A.java"]))
    assert out == ["src/A.java"], f"三种写法应归一成同一条并去重，实得 {out}"


def test_empty_and_missing_details_tolerated():
    """缺键 / 空清单 / 脏值不得抛（本函数在 reviewer 组装路径上，抛了就断链）。"""
    from swarm.brain.nodes.adversarial import _mechanism_paths

    assert _mechanism_paths(_wo([])) == []
    _bare = WorkerOutput(subtask_id="st-1", diff="", files_changed=[], summary="",
                         confidence=Confidence.HIGH)
    assert _mechanism_paths(_bare) == []
