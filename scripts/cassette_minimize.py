#!/usr/bin/env python3
"""#29-4 T-5：把 live 卡带瘦身成**可入库的最小 plan 夹具**。

## 为什么需要它

`cassettes/` 整个 gitignore（本地排障件，含绝对路径/项目 id/PRD 原文），于是三条
真 plan 回归守卫在 CI 上**全员静默 skip**：
  · `test_plan_validator.py`      G1 必须打回 round62 的 alarm-api 双落点
  · `test_r64_aux_evidence_hierarchy.py`  round64 死因（顶层 sql 弱证据分根）必须已治
  · `test_r67b_plan_fixes.py`     跨模块 create 重规范化必须把工具类归位

这三条守的都是**真实 E2E 死因**，是最贵的一类回归；靠"本机恰好有卡带"背书等于没有。
本仓已有成熟先例：`test/benchmark/plan_quality/fixtures/` 的夹具就是 tracked 的。

## 瘦身法：白名单，不是黑名单

只保留判据真正消费的字段。**不是**逐个剥离大字段——那样每加一个新字段就默认入库，
且"剥掉后判据没变"不代表"这个字段不承重"（可能只是当前夹具恰好不触发）。

实测（★数字一律按 `st_size` 真字节，不按 `len(str)` 字符数★，见 L-2 教训）：
  4.76 MB → **510 KiB**（240,280 + 241,330 + 40,772 B；各 12.9% / 15.2% / 2.7%）
判据保真：`issues` **与** `warnings` 都与真卡带**逐条集合相等**（复核 H-2 之后的口径 ——
原先只比 `issues` 就宣称"判据逐字不变"，而 warnings 当时是 14→8 / 7→4 且 r64 还凭空造
一条假警告）。三个最小夹具在治法被改坏时**红得和原卡带完全一样**（区分力等价，突变实证）。

## 用法

    python scripts/cassette_minimize.py            # 重新生成全部（幂等）
    python scripts/cassette_minimize.py --check     # 只校验现有夹具与卡带仍一致

`--check` 供本地用：卡带在开发机上才有，CI 上没有卡带故不跑校验（夹具已入库）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASS_DIR = ROOT / "cassettes"
OUT_DIR = ROOT / "test" / "fixtures" / "plan_cassettes"

# subtask 白名单：id/description/difficulty/modality/depends_on + scope 的写入面。
#   description **必须保留**：G1 ③d 考卷对账要读它（实测剥掉全部 description 后
#   round64 夹具的 issues 1→0，判据当场变了）。
#   ★depends_on 必须保留（复核 H-2 CONFIRMED，我第一版漏了）★：
#   `validate_module_coherence` 的 R67-12 全图汇聚点检查读它。漏掉的后果不在 issues
#   （那个确实逐字未变）而在 **warnings**：r62 14→8、r64 7→4，丢的全是
#   「纯辅助产物却依赖 52/83 个子任务=全图汇聚点」那一族。
#
# ★刻意不做的一步瘦身（留痕，含实测数据）★
# 逐条置空实测：r62 的 83 条 description **零条承重**，r64 的 107 条里**只有 st-1 承重**。
# 把非承重条全置成占位可再压到 39%/43%（123KB→48KB、135KB→58KB），业务原文入库面也更小。
# **但我没有这么做**，理由是这一步会把夹具的命题**悄悄变窄**：
#   今天"不承重"只说明**当前**判据不读它。日后若给 G1 加一条读 description 的检查
#   （考卷对账本身就是这么长出来的），置空过的夹具会**静默无法触发**它——夹具还在、
#   还绿着，但守护面已经缩了，而没有任何信号。这正是本战役反复治的假绿形状
#   （血规 10②：夹具形状决定被测命题唯一性）。
# 真 plan 夹具的价值恰在"忠实于真实死因现场"，**510 KiB**（三份合计，真字节）换这个
# 忠实度是值的。★这个数字必须与磁盘一致★：复核 N-5 指出它一度有三个世代混在一起
# （295 KB 字符口径 / 391 KB 加 depends_on 前 / 384 KB 中间态），而"值不值"这个**裁决**
# 正是记在本段的决策档案 —— 下一个人重新权衡时读到过期数字就会算错代价。
_ST_KEEP = ("id", "description", "difficulty", "modality", "depends_on")
_FP_KEEP = ("module", "path", "action")

# ★`plan.shared_contract` 必须**整段**保留（复核 H-2 CONFIRMED）★
# 判据从 `plan.shared_contract` 读（不是卡带顶层那个同名键——两者实测内容相同，但
# `TaskPlan` 只认 plan 内的那个；写错位置＝白写）。它贡献**模块宇宙**（`want`）的一半。
# 漏掉的后果不只是少几条 warning：r64 夹具会**凭空造出**一条真 plan 从未有过的
# `1 个声明的模块在计划里无任何代码落点…['ruoyi-admin']`（zero-dir 软 warn）——
# 那是最坏的一种夹具不忠实：**假红**。将来 zero-dir 若从 warn 硬化成 REJECT，
# 排查的人会拿着这条假红去改本来正确的生产代码。
#
# ★为什么不给它也编一张 key 白名单★：那正是 H-2 的错误往下再犯一层。
# 我原本对 subtask 编白名单，结果漏了 `depends_on`；若再对 shared_contract 编一张
# （比如只留 6 个我今天看得见的 key），下一次判据开始读 `pruned_artifacts` 时
# 同样的漏法会重演一遍。整段留 —— 它只占 46~50 KB，换的是"未来判据读什么都还在"。

# (输出名, 源卡带名, 说明)
TARGETS = [
    ("round62_alarm_api_double_root.json", "01520400_final.json",
     "G1 必须打回：模块 alarm-app/alarm-log 各落多个物理目录"),
    ("round64_toplevel_sql_weak_evidence.json",
     "f1e0f7b5-3be8-438e-8c07-fef2dc5588a6.json",
     "round64 死因已治：顶层 sql/*.sql 不得被当第二物理根"),
    ("round67b_cross_module_create.json",
     "251e05f3-7460-4578-850c-63f445766eb1.json",
     "跨模块 create 重规范化：AesUtils/TotpUtils 归 ruoyi-common"),
]

# 需要 plan 的夹具（r67b 只用 file_plan）
_NEEDS_PLAN = {"round62_alarm_api_double_root.json",
               "round64_toplevel_sql_weak_evidence.json"}

# 脱敏自检：这些串一个都不许出现在入库夹具里
_FORBIDDEN = ("/Users/", "/home/", "zhangyanrui", "5d0e9db8", "0d42679")


def minimize(cass: dict, *, with_plan: bool) -> dict:
    out: dict = {
        "schema": "swarm-plan-fixture/v1",
        "_note": "由 scripts/cassette_minimize.py 从 live 卡带瘦身而来，勿手改",
    }
    if with_plan:
        sts = []
        for st in (cass.get("plan") or {}).get("subtasks") or []:
            n = {k: st[k] for k in _ST_KEEP if k in st}
            sc = st.get("scope") or {}
            n["scope"] = {
                "writable": sc.get("writable") or [],
                "readable": [],
                "create_files": sc.get("create_files") or [],
            }
            sts.append(n)
        src_plan = cass.get("plan") or {}
        out["plan"] = {
            "subtasks": sts,
            "parallel_groups": src_plan.get("parallel_groups") or [],
            # 整段保留，且**取 plan 内那个**（判据只认它）
            "shared_contract": src_plan.get("shared_contract") or {},
        }
    out["file_plan"] = [{k: e[k] for k in _FP_KEEP if k in e}
                        for e in (cass.get("file_plan") or []) if isinstance(e, dict)]
    return out


def _sanity(name: str, small: dict) -> list[str]:
    """脱敏 + 前提自检。返回问题列表（空=通过）。"""
    problems = []
    blob = json.dumps(small, ensure_ascii=False)
    for bad in _FORBIDDEN:
        if bad in blob:
            problems.append(f"仍含敏感串 {bad!r}")
    if not small.get("file_plan"):
        problems.append("file_plan 为空——夹具会空转（血规 10④）")
    if name in _NEEDS_PLAN and not (small.get("plan") or {}).get("subtasks"):
        problems.append("plan.subtasks 为空")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="只校验已入库夹具与本机卡带一致，不写文件")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rc = 0
    for out_name, cass_name, desc in TARGETS:
        src = CASS_DIR / cass_name
        dst = OUT_DIR / out_name
        if not src.exists():
            print(f"[skip] {out_name}: 本机无卡带 {cass_name}"
                  f"（正常——卡带是本地排障件，夹具已入库）")
            continue
        small = minimize(json.loads(src.read_text()),
                         with_plan=out_name in _NEEDS_PLAN)
        problems = _sanity(out_name, small)
        if problems:
            print(f"[FAIL] {out_name}: {'; '.join(problems)}")
            rc = 1
            continue
        blob = json.dumps(small, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        if args.check:
            if not dst.exists():
                print(f"[FAIL] {out_name}: 夹具不存在，先跑一次不带 --check")
                rc = 1
            elif dst.read_text(encoding="utf-8") != blob:
                print(f"[FAIL] {out_name}: 与卡带重新生成的结果不一致（夹具被手改？）")
                rc = 1
            else:
                print(f"[ok]   {out_name}: 与卡带一致（{len(blob.encode('utf-8')):,} B）")
        else:
            dst.write_text(blob, encoding="utf-8")
            # ★复核 L-2★ 必须量**字节**：`len(str)` 是字符数，中文 UTF-8 占 3 字节，
            # 拿字符数去除真字节数（st_size）得到的比例偏小 —— 我三处 docstring 的
            # "295 KB / 6.6%" 就是这么来的，实际磁盘 510 KiB。
            nbytes = len(blob.encode("utf-8"))
            pct = 100 * nbytes / src.stat().st_size
            print(f"[写入] {out_name}: {src.stat().st_size:,} → {nbytes:,} B "
                  f"({pct:.1f}%)  {desc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
