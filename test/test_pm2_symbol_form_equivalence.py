"""P-M2（27 号文）：basename_symbol_match 补 snake_case/kebab-case 形态归一。

治前：`user_service`↔`UserService` 恒 -1（只认 CamelCase 惯例：I 前缀/Impl 后缀/
大小写词边界装饰）→ Go/Python/Rust/Ruby(snake)、JS(kebab) 的契约符号**文件通道
恒不匹配**，C1 owner 防线从「语料+文件」两条降成一条。

治法定案：词序列全等归 **tier 1（惯例等价精确档）**，绝不另立 tier 3——
消费契约审计（血规 10③）：
- dispatch/plan_validator 消歧按 (tier,-len) 升序，"精确>等价>装饰"三档已固化；
- symbol_provenance 把 t>1 当「装饰前缀弱通道」走改名 reconcile——另立 tier 3
  会让 `user_service.py`（蛇形正名）被当装饰变体"修"成 UserService.py。
"""
from __future__ import annotations

from swarm.brain.plan_validator import (
    _symbol_words,
    basename_owns_symbol,
    basename_symbol_match,
)


def test_snake_stem_matches_camel_symbol():
    """主治锁：Python/Go/Rust/Ruby 文件名 ↔ CamelCase 契约符号。"""
    assert basename_symbol_match("user_service", "UserService") == 1
    assert basename_symbol_match("get_user_report", "GetUserReport") == 1
    assert basename_owns_symbol("user_service", "UserService")


def test_kebab_stem_matches_camel_symbol():
    """JS 生态 kebab-case 文件名 ↔ CamelCase 契约符号。"""
    assert basename_symbol_match("user-service", "UserService") == 1
    assert basename_owns_symbol("fetch-report", "FetchReport")


def test_reverse_direction_symbol_snake_stem_camel():
    """反向同样归一（python 契约符号是 snake、文件名是 PascalCase 类名文件）。"""
    assert basename_symbol_match("UserService", "user_service") == 1


def test_word_order_and_plural_still_distinguish():
    """归一不放宽语义：词序不同/单复数不同 ≠ 同一符号（误配=豁免半径失控 F3 族）。"""
    assert basename_symbol_match("service_user", "UserService") == -1
    assert basename_symbol_match("users_service", "UserService") == -1
    assert basename_symbol_match("user_service", "UserRepository") == -1


def test_snake_decorated_prefix_not_matched():
    """★刻意不认★ snake 装饰变体（alarm_user_service ↔ UserService）——tier 2 的
    大小写词边界在 snake 里不存在，放宽就是 F3 的豁免半径失控；交 C1 打回求明示。"""
    assert basename_symbol_match("alarm_user_service", "UserService") == -1


def test_exact_tiers_unchanged():
    """既有分档逐字节不变：精确(0)/I 前缀(1)/装饰(2) 序不动（R43 F1：精确必须赢等价）。"""
    assert basename_symbol_match("UserService", "UserService") == 0
    assert basename_symbol_match("IUserService", "UserService") == 1
    assert basename_symbol_match("UserServiceImpl", "UserService") == 0
    assert basename_symbol_match("AlarmUserService", "UserService") == 2
    assert basename_symbol_match("AlarmUserService", "UserService",
                                 decorated_prefix=False) == -1


def test_words_helper_known_boundary():
    """登记边界：全大写缩写连写不可切（OAuth2Client）——方向=漏配（保守），
    绝不为多配放宽成子串。"""
    assert _symbol_words("user_service") == ["user", "service"]
    assert _symbol_words("UserService") == ["user", "service"]
    assert _symbol_words("user-service") == ["user", "service"]
    assert _symbol_words("userService") == ["user", "service"]
    assert _symbol_words("") == []
    # 缩写连写不归一（漏配方向=保守，登记）
    assert _symbol_words("OAuth2Client") != _symbol_words("oauth2_client")
