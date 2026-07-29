"""★方法论硬检查（round67m2 深扫元结论）——本仓唯一【入库】的落点★

为什么在测试里写散文：本仓的 `CLAUDE.md` 与 `docs/**/*.md` **全部被 gitignore**
（纪律"内部文档绝不入库"）。于是"下一个人一定会读到"的地方只剩两处：代码注释与测试。
四条检查本身是跨模块的方法论，没有单一归属代码文件，故独立成这个测试模块——
它会随仓库走，也会在 CI 里被执行到。

────────────────────────────────────────────────────────────────
本轮的元问题不是某个 bug，而是 **治本自己也会静默失效**，且每条都有多个实例。

① 接线覆盖 ≠ 机制存在
   新原语造对了却只接主调用点。已发生：
   · A3 凭据可观测挂在 `ModelInvocationLogger.on_llm_error` 上，而 brain 三处
     `get_chat_model` 全不传 callbacks——**事故正发生在 brain**，2h20m 零 WARNING；
   · B8-F2 出站端点闸只做【键名】分类器，而两条写入路径把端点藏在【值】里；
   · `contract_owner_ledger_block` 只扫 `interfaces`，而同文件的权威侧早已扫全 section。
   ⇒ **加机制先数调用点，一个不落地列出来。**

② 测试要证"被接上了"，不是"实现正确"
   假绿的典型：
   · 构造出生产代码从不产生的取值（手工造 `role="brain"`，而生产只产 `worker/*`）；
   · 用不触发目标分支的夹具（非 git 目录测 `git archive` 路径——那条路径**恒不执行**）；
   · `getsource` 断字面量（删掉机制、留着注释里的同名串，测试照绿——本轮突变实验实证）。
   ⇒ **判据：把被测机制整块删掉/改坏，这条测试会不会红？** 不确定就真做一次突变实验。
   断"接线事实/单一事实源"可以，断实现细节不行（硬性纪律 6）。

③ 复用单一事实源 ≠ 复用其消费契约
   共享表可以共享，**后果不同就必须分档**。已发生：
   · 密钥模式表的 HIGH 档是刻意 warn-only（前提＝下游 MERGE 有人工复核），被接到
     "拒绝即删存量向量"的入库闸上 → 当场冤杀（第一个受害者就是当年裁定的实证本尊）；
   · 知识库入库闸的"隐藏目录＝噪声"语义搬去做镜像 tarball 剔除 → `.mvn/wrapper`、
     `.yarn/releases` 被剔没，用 mvnw / yarn Berry 的项目沙箱里直接构建失败。
   ⇒ **改共享表之前先问：新消费者的后果和老消费者一样吗？** 不一样就分档/分函数。

④ "空返回/缺席"必须机读可辨
   `return []` 与"真没有"不可分时，那一层可以死很久没人知道：
   · knowledge 的 norms 层实测恒 0 **持续 12 天跨 5+ 轮 live**，零信号；
   · Layer B 层内自吞异常 `return []` → 外层 `stats["semantic_error"]` **永远收不到**，
     Qdrant 全宕与"该项目无相关知识"在 prompt 里逐字不可分。
   ⇒ 层内绝不自吞后返回空；降级路径至少一个机读键 + 一次 WARNING；
     **且该键必须有人消费——新账没有消费者＝没造。**
────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_four_checks_travel_with_the_repo():
    """★这四条必须留在入库的文件里★
    CLAUDE.md 与 docs/**/*.md 都被 gitignore，只有代码与测试会随仓库走。
    本条守着上面那段散文不被顺手删掉。"""
    doc = pathlib.Path(__file__).read_text()
    for anchor in ("接线覆盖 ≠ 机制存在",
                   "测试要证\"被接上了\"",
                   "复用单一事实源 ≠ 复用其消费契约",
                   "空返回/缺席\"必须机读可辨"):
        assert anchor in doc, f"缺方法论硬检查项：{anchor}"


def test_internal_docs_stay_out_of_the_repo():
    """★纪律 4 的可执行版本★：内部文档绝不入库。
    本条同时解释了上面那条为什么必须存在——纪律文档本身不在库里。"""
    import subprocess
    for path in ("CLAUDE.md", "docs/CODING_STANDARDS.md"):
        r = subprocess.run(["git", "check-ignore", "-q", path],
                           cwd=_ROOT, capture_output=True)
        assert r.returncode == 0, f"{path} 应当被 gitignore（内部文档绝不入库）"
