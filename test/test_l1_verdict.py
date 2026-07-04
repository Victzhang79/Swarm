"""A0/A1: L1 通过判定单一事实源 shared.l1_passed 的行为契约。

这是【行为测试】——只断言 l1_passed(out) 对各形态输入的返回值，
不断言实现结构（禁 inspect.getsource），改实现不挂、改行为才挂。

背景：round24 前 L1 判定散在 4 处副本（runner._passed / nodes 三处），
其中 nodes 三处用 isinstance(WorkerOutput) 严判，runner 用 getattr 鸭子判（超集）。
对真实输入（WorkerOutput / dict / None）四者等价；归一取鸭子判超集（最鲁棒）。
"""
from swarm.brain.nodes.shared import l1_passed
from swarm.types import WorkerOutput


def _wo(passed: bool) -> WorkerOutput:
    return WorkerOutput(subtask_id="s1", diff="", summary="", l1_passed=passed)


class TestL1Passed:
    def test_worker_output_true(self):
        assert l1_passed(_wo(True)) is True

    def test_worker_output_false(self):
        assert l1_passed(_wo(False)) is False

    def test_worker_output_default_is_false(self):
        # WorkerOutput.l1_passed 默认 False
        assert l1_passed(WorkerOutput(subtask_id="s1", diff="", summary="")) is False

    def test_dict_true(self):
        assert l1_passed({"l1_passed": True}) is True

    def test_dict_false(self):
        assert l1_passed({"l1_passed": False}) is False

    def test_dict_missing_key_is_false(self):
        assert l1_passed({"summary": "x"}) is False

    def test_none_is_false(self):
        assert l1_passed(None) is False

    def test_empty_dict_is_false(self):
        assert l1_passed({}) is False

    def test_truthy_coerced_to_bool(self):
        # dict 里放非布尔真值 → 归一为 True（bool 强制）
        assert l1_passed({"l1_passed": 1}) is True
        assert l1_passed({"l1_passed": 0}) is False

    def test_duck_typed_object_with_attr(self):
        # 鸭子判超集：任意带 l1_passed 属性的对象都识别（非仅 WorkerOutput）
        class _Duck:
            l1_passed = True

        assert l1_passed(_Duck()) is True

    def test_object_without_attr_is_false(self):
        assert l1_passed(object()) is False
