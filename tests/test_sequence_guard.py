"""顺序闸门：动作前拦截 + 给出退回合规状态的路径。

旧实现只在顺序跑完后用 ``verify_order`` 记账，走错了机器照样往下走。这里
覆盖三件事：规范顺序、动作前拦截（物理动作不发生）、按方案退回合规检查点
（越序步真的撤回、缺失步补齐后重试）。
"""

from __future__ import annotations

import pytest

from rubbercure.sequence.cure import CURE_ORDER
from rubbercure.sequence.dispatch import DISPATCH_ORDER
from rubbercure.sequence.guard import (
    OrderGuard,
    SequenceBlocked,
    canonical_order,
)
from rubbercure.sequence.openmold import OPEN_ORDER
from rubbercure.sequence.stop import STOP_ORDER


def test_canonical_order_matches_documented_cure_chain():
    assert canonical_order(CURE_ORDER) == [
        "mold_registered",
        "mapping_registered",
        "cavity_purge",
        "lift_close",
        "steam_open",
        "temp_confirm",
        "timer_start",
    ]


@pytest.mark.parametrize(
    "rules,expected",
    [
        (
            OPEN_ORDER,
            [
                "timer_complete",
                "cooling_start",
                "steam_vent",
                "lift_open",
                "part_removed",
                "latch_reset",
            ],
        ),
        (
            STOP_ORDER,
            ["steam_stop", "steam_vent", "coolant_drain", "cool_stop"],
        ),
        (
            DISPATCH_ORDER,
            ["completion_report", "order_confirm", "mold_assigned", "audit_note"],
        ),
    ],
)
def test_canonical_order_for_every_sequence(rules, expected):
    assert canonical_order(rules) == expected


def test_authorize_passes_when_prerequisites_done():
    guard = OrderGuard("cure", CURE_ORDER)
    for step in ["mold_registered", "mapping_registered", "cavity_purge"]:
        guard.authorize(step)
        guard.mark_done(step)
    # 到此不抛错即放行
    guard.authorize("lift_close")


def test_missing_prerequisite_is_blocked_before_action_runs():
    side_effects: list[str] = []
    guard = OrderGuard("cure", CURE_ORDER)

    with pytest.raises(SequenceBlocked) as caught:
        guard.authorize("lift_close")

    # 关键：物理动作根本没有被调用
    assert side_effects == []
    plan = caught.value.plan
    assert plan.blocked_step == "lift_close"
    assert plan.mode == "complete_prerequisites"
    missing = [step.step for step in plan.complete_missing]
    assert "cavity_purge" in missing
    assert plan.rollback == ()
    assert any("合模前必须先排气" in msg for msg in plan.violations)


def test_duplicate_step_is_blocked():
    guard = OrderGuard("cure", CURE_ORDER)
    guard.mark_done("mold_registered")
    guard.mark_done("mapping_registered")
    with pytest.raises(SequenceBlocked) as caught:
        guard.authorize("mold_registered")
    assert caught.value.plan.mode == "duplicate"


def test_overreached_step_is_rolled_back_to_compliant_checkpoint():
    physical = {"steam": "off"}
    guard = OrderGuard(
        "cure",
        CURE_ORDER,
        undo={"steam_open": lambda: physical.__setitem__("steam", "off")},
    )
    for step in ["mold_registered", "mapping_registered", "cavity_purge"]:
        guard.mark_done(step)
    # 越序现状：没合模却已经通了汽
    physical["steam"] = "on"
    guard.mark_done("steam_open", lambda: physical.__setitem__("steam", "off"))

    with pytest.raises(SequenceBlocked) as caught:
        guard.authorize("lift_close")

    plan = caught.value.plan
    assert plan.mode == "rollback_and_redo"
    assert [step.step for step in plan.rollback] == ["steam_open"]

    rolled = guard.rollback_to_compliant(plan)
    assert rolled == ["steam_open"]
    # 物理设备真的退回去了
    assert physical["steam"] == "off"
    assert guard.executed() == [
        "mold_registered",
        "mapping_registered",
        "cavity_purge",
    ]


def test_rollback_without_registered_undo_raises():
    guard = OrderGuard("cure", CURE_ORDER)
    for step in ["mold_registered", "mapping_registered", "cavity_purge"]:
        guard.mark_done(step)
    guard.mark_done("steam_open")  # 没给撤销原语
    with pytest.raises(SequenceBlocked) as caught:
        guard.authorize("lift_close")
    with pytest.raises(Exception):
        guard.rollback_to_compliant(caught.value.plan)


def test_recovery_plan_is_serializable():
    guard = OrderGuard("stop", STOP_ORDER)
    with pytest.raises(SequenceBlocked) as caught:
        guard.authorize("cool_stop")
    payload = caught.value.plan.as_dict()
    assert payload["retry"] == "cool_stop"
    assert payload["checkpoint"] == []
    assert {step["step"] for step in payload["complete_missing"]} == {
        "steam_stop",
        "steam_vent",
        "coolant_drain",
    }
    assert caught.value.as_record()["recovery"]["blocked_step"] == "cool_stop"


def test_unknown_step_is_rejected():
    guard = OrderGuard("cure", CURE_ORDER)
    with pytest.raises(Exception):
        guard.authorize("not_a_step")
