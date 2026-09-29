"""顺序闸口当场拦截与合规恢复路径。"""

from __future__ import annotations

import pytest

from rubbercure.errors import SequenceBlocked
from rubbercure.press.transitions import PressPhase
from rubbercure.sequence.selfcheck import SequenceGuard, SequenceRecorder

RULES = (
    ("a", "b", "a 必须先于 b"),
    ("b", "c", "b 必须先于 c"),
)


def test_guard_blocks_missing_prerequisite_before_action(platform):
    recorder = SequenceRecorder(platform.bus, "probe", RULES)
    with pytest.raises(SequenceBlocked) as caught:
        recorder.require("b")
    error = caught.value
    assert error.attempted == "b"
    assert error.missing == ["a"]
    # 动作发生前就拦下：记录为空，设备不会往下走。
    assert recorder.names() == []


def test_blocked_error_carries_forward_path(platform):
    guard = SequenceGuard("probe", RULES, platform.bus)
    with pytest.raises(SequenceBlocked) as caught:
        guard.require("c")
    # 传递前置也展开，补齐顺序本身合规。
    assert caught.value.recover["forward"] == ["a", "b", "c"]


def test_guard_allows_step_after_prerequisite_marked(platform):
    guard = SequenceGuard("probe", RULES, platform.bus)
    guard.mark("a")
    guard.require("b")  # 不抛
    guard.mark("b")
    guard.require("c")  # 不抛


def test_real_open_sequence_blocked_when_cure_never_ran(platform):
    unit = platform.press("press-01")
    platform.register_mold("mold-Q", cavity_count=2)
    unit.load_mold("mold-Q")
    response = _open_via_api(platform, "press-01")
    assert response["ok"] is False
    error = response["error"]
    assert error["subject"] == "SequenceBlocked"
    assert error["severity"] == "alarm"
    # 当场拦下，设备停在装模相位，闩锁未置位。
    assert unit.phase is PressPhase.MOLD_LOADED
    assert not platform.latch.is_set("mold-Q")
    recovery = error["recover"]
    assert "timer_start" in recovery["forward"]
    assert recovery["retreat"]["mode"] == "retreat"


def test_block_is_recorded_as_alarm_with_recovery(platform):
    platform.register_mold("mold-R", cavity_count=2)
    platform.press("press-01").load_mold("mold-R")
    _open_via_api(platform, "press-01")
    blocked = [
        item for item in platform.audit.alarms() if item["subject"] == "SequenceBlocked"
    ]
    assert blocked
    assert "recover" in blocked[-1]["detail"]


def test_retreat_from_loaded_phase_returns_to_idle(platform):
    unit = platform.press("press-01")
    platform.register_mold("mold-L", cavity_count=2)
    unit.load_mold("mold-L")
    result = platform.run_recovery("press-01")
    assert result["phase_after"] == "idle"
    assert [step["key"] for step in result["results"]] == ["unload_mold"]


def test_retreat_from_clamping_opens_before_unloading(platform):
    unit = platform.press("press-01")
    platform.register_mold("mold-C", cavity_count=2)
    unit.load_mold("mold-C")
    unit.transition(PressPhase.CLAMPING)
    unit.lift.close_clamp("mold-C")
    result = platform.run_recovery("press-01")
    assert result["phase_after"] == "idle"
    assert [step["key"] for step in result["results"]] == ["open_clamp", "unload_mold"]


def test_retreat_from_heating_vents_before_opening(platform):
    unit = platform.press("press-01")
    platform.register_mold("mold-H", cavity_count=2)
    unit.load_mold("mold-H")
    mapping = platform.maps.latest_for("mold-H")
    unit.steam.purge_cavity("mold-H", generation=mapping.generation)
    unit.transition(PressPhase.CLAMPING)
    unit.lift.close_clamp("mold-H")
    unit.transition(PressPhase.HEATING)
    unit.steam.open_steam(platform.config.steam.target_pressure_kpa)
    result = platform.run_recovery("press-01")
    keys = [step["key"] for step in result["results"]]
    assert keys == ["vent_steam", "open_clamp", "remove_part", "reset_latch", "finalize"]
    assert result["phase_after"] == "idle"
    # 退回后残压在安全值以内。
    assert unit.steam.pressure_kpa <= 12.0


def test_curing_phase_cannot_retreat_must_finish_forward(platform):
    platform.run_cure("mold-F", cavity_count=4)
    unit = platform.press("press-01")
    assert unit.phase is PressPhase.CURING
    plan = platform.recovery_plan("press-01")
    assert plan["mode"] == "finish_forward"
    with pytest.raises(SequenceBlocked) as caught:
        platform.run_recovery("press-01")
    assert "finish_cure" in caught.value.recover["forward"]


def test_finish_forward_then_open_returns_to_idle(platform):
    platform.run_cure("mold-G", cavity_count=4)
    finished = platform.finish_forward("press-01")
    assert finished["finish"]["latch_set"] is True
    opened = platform.run_open("press-01")
    assert opened["violations"] == []
    assert opened["press_phase"] == "idle"


def test_stop_sequence_blocks_draining_before_steam_stop(platform):
    # 直接对闸口验证：未泄压（含未停汽）就排冷却水会被当场拦下。
    from rubbercure.sequence.stop import STOP_ORDER

    recorder = SequenceRecorder(platform.bus, "stop", STOP_ORDER)
    with pytest.raises(SequenceBlocked) as caught:
        recorder.require("coolant_drain")
    assert caught.value.missing == ["steam_vent"]
    assert recorder.names() == []


def _open_via_api(platform, press_id: str) -> dict:
    from rubbercure.console.api import ConsoleAPI

    return ConsoleAPI(platform).open_mold({"press_id": press_id})
