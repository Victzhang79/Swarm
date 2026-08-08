"""W-8b（#29-5 批外单列，reviewer R2 实测）：npm `remove` 块重叠逃逸。

机制（与 N1 行尾 `\\s` 吞缩进不同根因）：npm 末条目的 block 含【前一条的 `,\\n`
前缀】（块字符级重叠——`parse_deps` 对无尾随逗号的末条目带前导逗号，而该逗号
同时是前一条 block 的尾随逗号）⇒ 前一条被删后，末条目 block 在演变中文本里
永不匹配 ⇒ enforce「块已不在」静默 continue ⇒ **判了 prune 却没动手=幻影逃逸**
（末两条同幻影时第二条必逃，npm 常态形态）。

治法（enforce 层、栈中立，非 npm 专属补丁）：块在【原文】在、在【演变中】文本
不在时，`_refind_block` 按 namespace+name+version 三元从当前文本重取新鲜 block
再处置；重定位也失败=响亮 WARNING，绝不静默。
★夹具前提自查纠正★：最初以为「同名 dup 跨 section 旧形态第二个跳过」也是本
兜底的覆盖面——实跑 HEAD 旧代码证伪（remove count=1 逐次命中字面相同 block，
旧形态本就两条都剪）；旧 docstring 的「第二个按已被带走跳过」只在重叠失配时
成立。test 3 从「行为变更锁」改定位为「回归锁」，措辞已同步。
"""
import json
import logging

from swarm.worker.dep_legality import DRIVERS, enforce

_MEMBERS = {"demo"}


def _reg(known: dict):
    """npm 仓库桩：known → 版本列表；未收录 → []（确证查无=幻影）。"""
    return lambda ns, name: known.get(name, [])


class TestW8bNpmBlockOverlapEscape:
    def test_last_phantom_no_longer_escapes_after_prev_pruned(self):
        """主回归锁：末两条同幻影——前一条（ghost-a）的 block 含尾随逗号，末条
        （ghost-b）的 block 含【同一个逗号】前缀。旧形态删 ghost-a 后 ghost-b 的
        block 失配 ⇒ 静默逃逸；现必须两条都剪且 JSON 合法、真依赖不动。"""
        pkg = ('{\n  "name": "demo",\n  "dependencies": {\n'
               '    "react": "^18.2.0",\n'
               '    "ghost-a": "^1.0.0",\n'
               '    "ghost-b": "^2.0.0"\n'
               '  }\n}\n')
        # 夹具自检：两条幻影的 block 必须真的字符级重叠（ghost-b 的 block 以
        # ghost-a 的尾随逗号开头）——形状不对=没测到目标机制
        deps = {d["name"]: d for d in DRIVERS["npm"].parse_deps(pkg)}
        assert deps["ghost-b"]["block"].startswith(",")
        assert deps["ghost-a"]["block"].rstrip().endswith(",")

        new_texts, actions = enforce(
            {"package.json": pkg}, root_text=pkg, namespace=None,
            workspace_members=_MEMBERS, registry_versions=_reg({"react": ["18.2.0"]}),
            driver=DRIVERS["npm"])
        parsed = json.loads(new_texts["package.json"])  # JSON 合法性=硬断言（抛=红）
        assert "ghost-a" not in parsed["dependencies"]
        assert "ghost-b" not in parsed["dependencies"], \
            "末条幻影绝不容逃逸（旧形态：block 重叠失配 → 静默 continue）"
        assert parsed["dependencies"]["react"] == "^18.2.0", "真依赖绝不被连坐"
        assert sum("ghost-b" in a for a in actions) == 1, \
            "处置账必须有 ghost-b 一条（逃逸形态下 actions 里查无此包=谎报面）"

    def test_last_phantom_still_pruned_when_prev_legal(self):
        """对照：末条幻影、前条合法——前条不处置 ⇒ 末条 block 原样命中（不走兜底），
        行为与旧形态逐字一致（锁正常路径零回归）。"""
        pkg = ('{\n  "name": "demo",\n  "dependencies": {\n'
               '    "react": "^18.2.0",\n'
               '    "ghost-b": "^2.0.0"\n'
               '  }\n}\n')
        new_texts, actions = enforce(
            {"package.json": pkg}, root_text=pkg, namespace=None,
            workspace_members=_MEMBERS, registry_versions=_reg({"react": ["18.2.0"]}),
            driver=DRIVERS["npm"])
        parsed = json.loads(new_texts["package.json"])
        assert "ghost-b" not in parsed["dependencies"]
        assert parsed["dependencies"]["react"] == "^18.2.0"
        assert sum("ghost-b" in a for a in actions) == 1

    def test_dup_across_sections_both_pruned(self):
        """回归锁（非行为变更——已实跑 HEAD 旧代码证伪原前提）：同名同版本 dup
        同现 dependencies 与 devDependencies（各自段内唯一条目 ⇒ block 字面相同），
        remove count=1 逐次命中 ⇒ 两条都必须剪（防未来有人把 remove 改成
        「按段限位」或 block 带段上下文后行为退化而不自知）。"""
        pkg = ('{\n  "name": "demo",\n'
               '  "dependencies": {\n    "ghost": "^1.0.0"\n  },\n'
               '  "devDependencies": {\n    "ghost": "^1.0.0"\n  }\n}\n')
        new_texts, actions = enforce(
            {"package.json": pkg}, root_text=pkg, namespace=None,
            workspace_members=_MEMBERS, registry_versions=_reg({}),
            driver=DRIVERS["npm"])
        parsed = json.loads(new_texts["package.json"])
        assert "ghost" not in parsed["dependencies"]
        assert "ghost" not in parsed["devDependencies"]
        assert sum("ghost" in a for a in actions) == 2, "dup 两条都必须进处置账"

    def test_refind_exhausted_reports_loudly_and_never_lies(self, caplog):
        """fail-honest 锁：重定位兜底也找不到 ⇒ 响亮 WARNING + 处置账绝不谎报。

        注：三条全同 dup 走不到这一支——字面相同的 block 在 cur 里始终「在」，
        remove 逐次摘除即可（已实测验证）。本分支的可达形态=条目被前一条处置
        【整体】带走（连名字都解析不出），夹具=stub driver：首解析给两条同 block
        的 dep，兜底重解析返空。"""
        class _StubDrv:
            stack = "stub"
            namespace_mandatory = False
            self_hosted_prefixes = ()
            probe_without_namespace = True

            def __init__(self):
                self.parses = 0

            def managed_names(self, t):
                return set()

            def managed_unknown(self, t):
                return False

            def parse_deps(self, text):
                self.parses += 1
                if self.parses == 1:
                    return [
                        {"namespace": "", "name": "g1", "version": "1", "block": "XBLK"},
                        {"namespace": "", "name": "g2", "version": "1", "block": "XBLK"}]
                return []   # 兜底重解析：条目已被前一条处置整体带走

            def remove(self, text, block):
                return text.replace(block, "", 1)

        with caplog.at_level(logging.WARNING, logger="swarm.worker.dep_legality"):
            new_texts, actions = enforce(
                {"m": "XBLK"}, root_text="XBLK", namespace=None,
                workspace_members=_MEMBERS, registry_versions=_reg({}),
                driver=_StubDrv())
        assert new_texts["m"] == "", "第一条照常处置"
        assert len(actions) == 1 and "g1" in actions[0], \
            "第二条未处置 ⇒ 处置账里绝不准出现 g2（谎报=逃逸的第二形态）"
        assert any("重定位失败" in r.message for r in caplog.records), \
            "重定位兜底耗尽必须响亮报告（缺席可辨），绝不静默 skip"

    def test_refind_parse_exception_never_cascades(self, caplog):
        """连坐防护锁（hunter R1 实测：无 guard 时 parse_deps raise 会炸掉整条
        enforce）：兜底重解析抛异常 ⇒ 归并为「重定位失败」——WARNING 留痕 +
        该条跳过 + 绝不向调用方传播（单条 manifest/未来 driver 不得连坐全批）。
        夹具=stub driver：首解析给两条同 block 的 dep，兜底重解析直接 raise。"""
        class _BoomDrv:
            stack = "boom"
            namespace_mandatory = False
            self_hosted_prefixes = ()
            probe_without_namespace = True

            def __init__(self):
                self.parses = 0

            def managed_names(self, t):
                return set()

            def managed_unknown(self, t):
                return False

            def parse_deps(self, text):
                self.parses += 1
                if self.parses == 1:
                    return [
                        {"namespace": "", "name": "g1", "version": "1", "block": "XBLK"},
                        {"namespace": "", "name": "g2", "version": "1", "block": "XBLK"}]
                raise RuntimeError("未来 driver 对半截文本炸了")

            def remove(self, text, block):
                return text.replace(block, "", 1)

        with caplog.at_level(logging.WARNING, logger="swarm.worker.dep_legality"):
            new_texts, actions = enforce(
                {"m": "XBLK"}, root_text="XBLK", namespace=None,
                workspace_members=_MEMBERS, registry_versions=_reg({}),
                driver=_BoomDrv())   # 无 guard 时这里直接 RuntimeError 炸出来
        assert new_texts["m"] == "", "第一条照常处置"
        assert len(actions) == 1, "第二条未处置 ⇒ 处置账不谎报"
        assert any("parse_deps 异常" in r.message for r in caplog.records), \
            "兜底重解析异常必须有 WARNING 留痕（层内自吞=外层永远收不到）"

    def test_refind_matches_full_coordinate_not_bare_name(self):
        """三元判据锁（防过宽兜底冤杀）：同名不同版本共存时，重定位必须按
        namespace+name+version 全三元匹配——只按名字会把先出现的【合法同名
        条目】的 block 错当目标 ⇒ file: 协议引用的合法条目被误剪。
        夹具：合法 file: 的 x 在前、幻影 y（非末条）居中、幻影 x(^9.9.9) 末条——
        删 y 后末条 block 失配触发兜底，名字撞车正在此刻发生。
        （断言走原文不走 json.loads：dup key 下 json 只留最后一条，会吃掉命题。）"""
        pkg = ('{\n  "name": "demo",\n  "dependencies": {\n'
               '    "x": "file:../tools/x",\n'
               '    "y": "^1.0.0",\n'
               '    "x": "^9.9.9"\n'
               '  }\n}\n')
        new_texts, actions = enforce(
            {"package.json": pkg}, root_text=pkg, namespace=None,
            workspace_members=_MEMBERS, registry_versions=_reg({}),
            driver=DRIVERS["npm"])
        out = new_texts["package.json"]
        assert "file:../tools/x" in out, \
            "合法 file: 同名条目绝不被误剪（重定位只按名字=过宽兜底=冤杀）"
        assert '"y"' not in out, "幻影 y 必须被剪"
        assert "^9.9.9" not in out, "末条幻影 x 必须经兜底重定位被剪（不逃逸）"
