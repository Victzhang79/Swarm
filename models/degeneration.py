"""R63-T7：流式输出复读退化探测器（纯文本、栈无关、零依赖）。

round63 实锤（3 个 worker，全部 Qwopus3.6-27B-v2-NVFP4 本地 4-bit）：LLM 在正文里
陷入 token 级复读——同标识符洪泛（`IllegalArgumentEx` 预览窗 ×12）或整句循环
（「我意识到我一直在犯一个循环错误。让我停下来仔细思考」）。此时 chunk 持续产出：
  · stall 双超时看不出（间隔正常）；
  · R55 思考预算看不出（复读在正文，content_seen 后判据短路；且本地 worker 关 thinking）；
  · max_tokens 要吐满 8192 才截断。
唯一兜底是 900s 墙钟/迭代上限——st-2-1-1-2 连烧 3×900s，且复读产物把截断类名写进
源码毒化下游编译。本模块在【chunk 经手点】做内容级探测，触发即中止流。

判据设计（register 原文「同标识符/句 ≥3× 即中止」按证据修正——round63 语料里
`public` 出现 2995 次，纯计数在真实代码上必误杀）：
  · word_flood（词洪泛）：滑窗内同一长词高密度（count+字符覆盖率）且全窗词多样性
    极低、且它出现的【上下文句模板】寥寥无几（区分合法 import 块：springframework
    重复 26 次但每行是新类名 → 上下文模板数=出现数）；
  · segment_loop（句循环）：字面完全相同的自然语句（含 CJK 或 ≥3 空白分词——排除
    pom/XML 的 <groupId>…</groupId> 类结构行）重复 ≥N 次且合计覆盖率高。
两通道阈值都用 test_r63_t7 的 round63 实锤样本与合法高重复代码（import 块/常量文件/
pom 依赖表/stub 方法）双向锁死。近似变体循环（每轮措辞微变）低于阈值时仍由既有
迭代上限/墙钟兜底——已知边界，宁缺勿滥。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_SEG_SPLIT_RE = re.compile(r"[\n。；;！!？?]+")
_CJK_RE = re.compile(r"[一-鿿]")

# 覆盖率/多样性阈值（模块级常量：与 count 类阈值不同，改动需重跑双向校准测试，
# 不开 env 面——防运维只调一半把误杀/漏杀平衡打破）。
_WORD_MIN_LEN = 6          # 短词（public/return/String）天然高频，不作洪泛载体
_WORD_COVERAGE = 0.25      # 单词字符覆盖率下限（st-2-1-1-2 实测 ~0.4-0.6）
_MAX_DIVERSITY = 0.30      # 全窗 distinct/total 词多样性上限（复读窗实测 ~0.1）
_MAX_CONTEXT_RATIO = 0.5   # 含该词的【去重句模板数/出现次数】上限（import 块=1.0；
#                            严格小于——SQL 种子行内双列复用同值恰为 0.5，复核 R-F2 实锤）
_SEG_MIN_LEN = 10          # 句循环最短句长（strip 后）
_SEG_COVERAGE = 0.30       # 重复句合计字符覆盖率下限（st-8 实测 ~0.5-0.8）
_MIN_WORDS = 12            # 窗内词数下限（不足不判，防超短窗噪声）
# 复核 R-F1 实锤（HIGH）：【句多样性】总闸——真复读窗里几乎没有新句（st-8 实测
# distinct/total ≈ 0.12），而合法高重复（规划 JSON 共享验收句 / PRD 共享措辞 / stub 共享
# 中文 Javadoc / SQL 种子）重复句之间必然穿插大量新句（实测 ≥0.38）。CJK 资格判据本身
# 拦不住中文合法重复，必须靠这道闸。仅在窗内句数足够（≥_MIN_SEGS_FOR_DIVERSITY）时
# 生效——单行无标点的纯洪泛只有 1 句，多样性无意义，退回 word 通道自身判据。
_MAX_SEG_DIVERSITY = 0.25
_MIN_SEGS_FOR_DIVERSITY = 6


@dataclass
class DegenerationVerdict:
    """复读退化判定证据（进日志与 StreamDegenerationError.evidence）。"""

    channel: str      # "word_flood" | "segment_loop"
    needle: str       # 复读载体（标识符 / 句子，截断）
    count: int        # 窗内重复次数
    coverage: float   # 字符覆盖率
    diversity: float  # 全窗词多样性（segment_loop 通道恒 0.0，不参与判定）
    sample: str       # 窗口尾部样本（截断，供人工复核）


class StreamRepetitionDetector:
    """滑窗式流式复读探测。feed() 增量喂文本，触发返回 DegenerationVerdict。

    每积累 check_every 字符扫一次窗口（O(窗口) 正则+计数，相对 LLM 解码零成本）；
    未达 min_chars 绝不判（短回复天然重复度高）。R56-1 关 thinking 重开流时必须
    配套 reset()（新流旧窗不共账）。
    """

    def __init__(
        self,
        *,
        window_chars: int = 1200,
        check_every: int = 160,
        min_chars: int = 320,
        word_repeats: int = 8,
        seg_repeats: int = 4,
    ) -> None:
        self.window_chars = max(200, int(window_chars))
        self.check_every = max(40, int(check_every))
        self.min_chars = max(120, int(min_chars))
        self.word_repeats = max(2, int(word_repeats))
        self.seg_repeats = max(2, int(seg_repeats))
        self._buf = ""
        self._total = 0
        self._since_check = 0

    def reset(self) -> None:
        self._buf = ""
        self._total = 0
        self._since_check = 0

    def feed(self, text: str) -> DegenerationVerdict | None:
        if not text:
            return None
        self._buf = (self._buf + text)[-self.window_chars:]
        self._total += len(text)
        self._since_check += len(text)
        if self._total < self.min_chars or self._since_check < self.check_every:
            return None
        self._since_check = 0
        return self._scan(self._buf)

    # ── 内部 ────────────────────────────────────────────────
    @staticmethod
    def _segments(win: str) -> list[str]:
        return [s.strip() for s in _SEG_SPLIT_RE.split(win) if s.strip()]

    def _scan(self, win: str) -> DegenerationVerdict | None:
        words = _WORD_RE.findall(win)
        if len(words) < _MIN_WORDS:
            return None

        # 句多样性总闸（复核 R-F1）：新句占比高 = 有信息在产出，两通道都不许判复读。
        # 真复读窗几乎没有新句（st-8 ≈0.12）；合法高重复（规划 JSON/PRD/共享 Javadoc/
        # SQL 种子）重复句间必然穿插大量新句（实测 ≥0.38）。句数太少（如单行无标点的
        # 纯洪泛）时多样性无意义 → 闸不生效，交由 word 通道自身判据。
        segs = self._segments(win)
        if (len(segs) >= _MIN_SEGS_FOR_DIVERSITY
                and len(set(segs)) / len(segs) > _MAX_SEG_DIVERSITY):
            return None

        # 通道①：词洪泛（st-2-1-1-2 形态）
        diversity = len(set(words)) / len(words)
        if diversity <= _MAX_DIVERSITY:
            cand = [w for w in words if len(w) >= _WORD_MIN_LEN]
            if cand:
                top, cnt = Counter(cand).most_common(1)[0]
                coverage = cnt * len(top) / max(1, len(win))
                if cnt >= self.word_repeats and coverage >= _WORD_COVERAGE:
                    # 上下文模板数：合法结构性重复（import 块）每次出现都在【新句】里；
                    # 复读洪泛只在寥寥几个句模板里打转。严格小于（复核 R-F2：SQL 种子
                    # 行内 create_time/update_time 双列复用同值恰好打到 0.5 临界）。
                    ctx = {s for s in segs if top in s}
                    if len(ctx) < max(2, cnt * _MAX_CONTEXT_RATIO):
                        return DegenerationVerdict(
                            channel="word_flood", needle=top, count=cnt,
                            coverage=round(coverage, 3), diversity=round(diversity, 3),
                            sample=win[-240:],
                        )

        # 通道②：句循环（st-8 形态）。资格句 = 长度够 + 像自然语句
        # （含 CJK，或 ≥3 个空白分词——排除 </dependency> 这类单 token 结构行）。
        eligible = [
            s for s in segs
            if len(s) >= _SEG_MIN_LEN and (_CJK_RE.search(s) or len(s.split()) >= 3)
        ]
        if eligible:
            counts = Counter(eligible)
            repeated = {s: c for s, c in counts.items() if c >= self.seg_repeats}
            if repeated:
                covered = sum(len(s) * c for s, c in repeated.items()) / max(1, len(win))
                if covered >= _SEG_COVERAGE:
                    top_s, top_c = max(repeated.items(), key=lambda kv: kv[1] * len(kv[0]))
                    return DegenerationVerdict(
                        channel="segment_loop", needle=top_s[:80], count=top_c,
                        coverage=round(covered, 3), diversity=0.0,
                        sample=win[-240:],
                    )
        return None
