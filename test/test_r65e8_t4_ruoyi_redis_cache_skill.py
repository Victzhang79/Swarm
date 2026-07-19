"""R65E8-T4（round65e8 st-56 实锤·RedisCache 方法级幻觉）：ruoyi-redis-cache 技能落库 + 可选中。

死因：既有 `_detect_infra_symbols`（stack_detect）只给 worker 缓存类的**类级 FQN**（RedisCache 存在），
但 worker 在包装器上调**裸 RedisTemplate 的 set/get** 签名（`redisCache.set(k,v,ttl,unit)`）→
`cannot find symbol` 死循环。class 级 grounding 挡不住 method 级幻觉。

治本（本 T4）：补方法级经验技能——RedisCache 包装器正确 API（setCacheObject/getCacheObject/
deleteObject）+ 幻觉黑名单（禁 .set/.get/.del）+ 经典 CacheUtils 分支 + "先读同仓样板照抄"。
"""
from __future__ import annotations

from swarm.config.settings import PROJECT_ROOT
from swarm.experience.library import load_skills
from swarm.experience.selector import select_skills

_LIB = PROJECT_ROOT / "skills_library"
_SKILL_ID = "redis-cache-conventions"


def _docs():
    return load_skills(_LIB)


def _skill():
    for d in _docs():
        if d.id == _SKILL_ID:
            return d
    return None


def test_skill_loads_with_expected_routing():
    d = _skill()
    assert d is not None, f"{_SKILL_ID} 未被 load_skills 加载（frontmatter/命名问题）"
    assert d.imported is False, "应为 native（显式路由）"
    assert "java" in d.applies_to_stacks
    assert "worker" in d.target
    assert set(d.applies_to_intents) & {"create", "modify", "debug"}
    assert set(d.applies_to_phases) & {"code", "produce"}
    assert d.body.strip() and len(d.body) <= 4000


def test_skill_selectable_for_java_cache_subtask():
    """java 栈 + code 阶段 worker 应能选中本技能（否则 worker 永不触达）。"""
    docs = _docs()
    picked = select_skills(
        docs, stack_langs={"java"}, intent="create", phase="code",
        target="worker", budget_chars=10**9, max_k=50,
    )
    assert _SKILL_ID in {p.id for p in picked}, "java/code/worker 未选中 ruoyi-redis-cache"


def test_skill_not_selectable_for_python():
    """栈隔离：python 项目不该被喂 RuoYi 缓存技能。"""
    docs = _docs()
    picked = select_skills(
        docs, stack_langs={"python"}, intent="create", phase="code",
        target="worker", budget_chars=10**9, max_k=50,
    )
    assert _SKILL_ID not in {p.id for p in picked}


def test_skill_body_has_correct_api_and_blacklist():
    d = _skill()
    body = d.body
    # 正确方法级 API（三种抽象都覆盖）
    for good in ("setCacheObject", "getCacheObject", "deleteObject", "CacheUtils", "opsForValue"):
        assert good in body, f"缺正确 API {good}"
    # 幻觉黑名单点名裸 set/get（本轮真死因签名）
    assert "redisCache.set(" in body and "redisCache.get(" in body, \
        "应点名 redisCache.set/get 为幻觉（round65e8 真死因签名）"
    # 读样板纪律
    assert "照抄" in body or "样板" in body


def test_skill_is_detection_first_not_hardcoded_variant():
    """★correctness 核心★ E2E 基线 workspace/RuoYi 是经典 Shiro+EhCache（无 Redis）。
    技能绝不能硬判"若依=RedisCache"（会误导 worker 写不存在的类）——必须探测优先，
    且明确点出"经典基线无 Redis、别引入"这一 round65e8 真死因。"""
    body = _skill().body
    # 探测优先：要求先 grep 真实缓存类
    assert "grep" in body.lower(), "必须指示先 grep 探测真实缓存类"
    # 明确"没有 Redis / 别引入不存在的抽象"这一真死因边界
    assert ("没有 Redis" in body or "没有 RedisCache" in body or "绝不引入" in body
            or "别引入" in body), "必须点明经典基线无 Redis、别硬塞（round65e8 真死因）"
    # EhCache 变体的真实 API（对齐基线 CacheUtils 真实签名）
    assert "removeAll" in body or "put(cacheName" in body, "应含 EhCache CacheUtils 真实签名"


def test_no_duplicate_id_after_adding_skill():
    ids = [d.id for d in _docs()]
    assert ids.count(_SKILL_ID) == 1, "新技能 id 重复"
    assert len(ids) == len(set(ids)), "库内存在重复 id"


def test_skill_does_not_overmatch_noncache_ruoyi_subtasks():
    """★回归锁★ id/title/description 绝不含 'ruoyi'——否则 com/ruoyi/... 路径令本技能命中
    【每个】子任务（同 java 语言词零区分度问题），把 springboot-security/java-coding-standards
    挤出 top-k（本 T4 开发中实证）。用 push 路径验证：非缓存子任务不得选中本技能。"""
    from swarm.experience.service import select_worker_push_pull
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskIntent

    stack = {"backend": "Spring Boot (java)", "language": "java", "build": "maven"}

    def _push(desc, files):
        st = SubTask(id="st-x", description=desc, intent=TaskIntent.CREATE,
                     difficulty=SubTaskDifficulty.MEDIUM,
                     scope=FileScope(create_files=files))
        return {d.id for d in select_worker_push_pull(st, stack)[0]}

    security = _push("实现 2FA 认证与登录 security",
                     ["alarm-security/src/main/java/com/ruoyi/alarm/security/Google2FAService.java"])
    persist = _push("实现告警任务持久化：Mapper 与实体映射",
                    ["alarm-task/src/main/java/com/ruoyi/alarm/mapper/AlarmTaskMapper.java"])
    assert _SKILL_ID not in security, f"缓存技能不该进安全子任务（ruoyi 路径过匹配）: {security}"
    assert _SKILL_ID not in persist, f"缓存技能不该进持久化子任务（ruoyi 路径过匹配）: {persist}"

    # 反向：真缓存子任务必须选中（否则治本失效）
    cache = _push("实现登录 token 的 Redis 缓存存取：写 setCacheObject/getCacheObject",
                  ["alarm-framework/src/main/java/com/ruoyi/framework/redis/TokenCacheService.java"])
    assert _SKILL_ID in cache, f"真缓存子任务必须选中本技能: {cache}"
