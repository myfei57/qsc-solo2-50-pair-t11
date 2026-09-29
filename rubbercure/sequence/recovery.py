"""退回合规状态的可执行恢复路径。

顺序闸口拦下非法下一步时，现场需要两件事：一是"补齐哪些步骤才能继续"
（前向路径，由 :class:`~rubbercure.sequence.selfcheck.SequenceGuard` 给出），
二是"现在已经做了一半，怎么安全退回已知合规相位"。本模块按硫化机当前
真实相位生成退回计划并逐步执行：

* 已在硫化计时（CURING/COOLING）的制品不能物理撤销，唯一合规出路是
  继续硫化到计时完成、再走正常开模顺序取件，不允许带压倒退；
* 升温（HEATING/VENTING）可以停汽、放残压、开模、取件，退回 IDLE；
* 只合模（CLAMPING）时尚未通汽，直接开模卸模即可；
* 只装模（MOLD_LOADED）直接卸模。

每一步都复用设备原语（泄压凭据、开模联锁等），所以退回动作本身同样受
联锁保护，不会出现"为了恢复反而违规"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..clock import Clock
from ..errors import SequenceBlocked
from ..eventbus import Event, EventBus
from ..lift.open import open_mold
from ..press.transitions import PressPhase
from ..timer.done import complete_cure
from ..timer.latch import CureLatch

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免环
    from ..press.press import CuringPress
    from ..steam.vent import VentToken


@dataclass(frozen=True)
class RecoveryAction:
    """退回路径上的一步。"""

    key: str
    instruction: str
    phase_after: str

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "instruction": self.instruction,
            "phase_after": self.phase_after,
        }


# 各相位的退回动作：(动作键, 说明, 执行后的硫化机相位)。
# CURING/COOLING 走前向完工，不在这里倒退。
_RETREAT_TABLE: dict[PressPhase, tuple[tuple[str, str, PressPhase], ...]] = {
    PressPhase.MOLD_LOADED: (
        ("unload_mold", "卸下在机模具，回到待机", PressPhase.IDLE),
    ),
    PressPhase.CLAMPING: (
        ("open_clamp", "尚未通汽，直接开模", PressPhase.MOLD_LOADED),
        ("unload_mold", "卸下在机模具，回到待机", PressPhase.IDLE),
    ),
    PressPhase.HEATING: (
        ("vent_steam", "停主蒸汽阀并把残压放到安全值", PressPhase.VENTING),
        ("open_clamp", "凭泄压凭据安全开模", PressPhase.OPENING),
        ("remove_part", "取件并清在机模具", PressPhase.OPENING),
        ("reset_latch", "按模复位完成闩锁", PressPhase.OPENING),
        ("finalize", "结束本模次并回到待机", PressPhase.IDLE),
    ),
    PressPhase.VENTING: (
        ("vent_steam", "确认残压放到安全值", PressPhase.VENTING),
        ("open_clamp", "凭泄压凭据安全开模", PressPhase.OPENING),
        ("remove_part", "取件并清在机模具", PressPhase.OPENING),
        ("reset_latch", "按模复位完成闩锁", PressPhase.OPENING),
        ("finalize", "结束本模次并回到待机", PressPhase.IDLE),
    ),
    PressPhase.OPENING: (
        ("remove_part", "完成取件并清在机模具", PressPhase.OPENING),
        ("reset_latch", "按模复位完成闩锁", PressPhase.OPENING),
        ("finalize", "结束本模次并回到待机", PressPhase.IDLE),
    ),
}

# 已进硫化的相位只能前向完工。
_FORWARD_FINISH_PHASES = (PressPhase.CURING, PressPhase.COOLING)


class RecoveryPlanner:
    """按硫化机真实相位生成并执行退回路径。"""

    def __init__(self, press: "CuringPress", clock: Clock, bus: EventBus, latch: CureLatch) -> None:
        self.press = press
        self.clock = clock
        self.bus = bus
        self.latch = latch

    def plan(self) -> dict:
        """生成退回计划（不执行任何动作）。"""

        phase = self.press.phase
        mold_id = self.press.mold_id or self.press.press_id
        if phase in _FORWARD_FINISH_PHASES:
            return {
                "press_id": self.press.press_id,
                "phase": phase.value,
                "mold_id": mold_id,
                "mode": "finish_forward",
                "reason": "已进入硫化计时，制品不可物理撤销，必须硫化完成后正常开模取件",
                "actions": [
                    RecoveryAction("finish_cure", "推进计时到计划硫化时长并完成判定", "venting").as_dict(),
                    RecoveryAction("vent_steam", "泄压到安全值", "opening").as_dict(),
                    RecoveryAction("open_clamp", "安全开模取件", "done").as_dict(),
                    RecoveryAction("idle", "回到待机接下一模", "idle").as_dict(),
                ],
            }

        rows = _RETREAT_TABLE.get(phase, ())
        return {
            "press_id": self.press.press_id,
            "phase": phase.value,
            "mold_id": mold_id,
            "mode": "retreat",
            "reason": "按设备联锁顺序退回待机",
            "actions": [
                RecoveryAction(key, instruction, target.value).as_dict()
                for key, instruction, target in rows
            ],
        }

    def execute_retreat(self) -> dict:
        """执行退回路径，返回每一步的结果。"""

        phase = self.press.phase
        if phase is PressPhase.IDLE or phase is PressPhase.DONE:
            return {"press_id": self.press.press_id, "plan": self.plan(), "results": []}
        if phase in _FORWARD_FINISH_PHASES:
            raise _cannot_retreat(self.press, phase)

        mold_id = self.press.require_mold()
        plan = self.plan()
        vent_token: "VentToken | None" = None
        results: list[dict] = []
        for action in plan["actions"]:
            key = action["key"]
            if key == "vent_steam":
                vent_token = self.press.steam.vent_pressure(mold_id)
                if self.press.phase in (PressPhase.HEATING, PressPhase.CURING, PressPhase.COOLING):
                    self.press.transition(PressPhase.VENTING)
                results.append(
                    {"key": key, "ok": True, "residual_kpa": round(vent_token.residual_kpa, 3)}
                )
            else:
                results.append(self._run(key, mold_id, vent_token))
        self.bus.publish(
            Event.of(
                "sequence",
                "sequence",
                "retreat_completed",
                subject=self.press.press_id,
                severity="notice",
                press_id=self.press.press_id,
                steps=[item["key"] for item in results],
                final_phase=self.press.phase.value,
            )
        )
        return {"press_id": self.press.press_id, "plan": plan, "results": results}

    def finish_forward(self, *, advance_s: float | None = None) -> dict:
        """已进硫化时的合规出路：补齐计时后走正常开模顺序。

        ``advance_s`` 给出需要补推进的秒数（测试/可推进时钟下用）；不传时
        按计时器剩余时间推进。调用方随后可直接执行开模顺序取件。
        """

        from ..timer.cycle import advance_cure

        press = self.press
        mold_id = press.require_mold()
        remaining = max(0.0, press.timer.planned_s - press.timer.elapsed_s)
        dt = remaining if advance_s is None else max(0.0, float(advance_s))
        if dt > 0:
            press.timer.set_clock_now(self.clock.now())
            advance_cure(press.timer, dt, self.bus, measured_c=0.0)
        finished = complete_cure(press.timer, self.latch, self.bus)
        return {
            "press_id": press.press_id,
            "mold_id": mold_id,
            "advanced_s": round(dt, 3),
            "finish": finished,
            "next": "执行开模顺序（泄压 → 开模 → 取件 → 复位闩锁）回到待机",
        }

    def _run(self, key: str, mold_id: str, vent_token: "VentToken | None") -> dict:
        press = self.press
        if key == "unload_mold":
            press.transition(PressPhase.IDLE)
            press.clear_mold()
        elif key == "open_clamp":
            if press.phase is PressPhase.CLAMPING:
                # 尚未通汽，不存在残压；先退回装模相位再开模。
                press.transition(PressPhase.MOLD_LOADED)
                opening = press.lift.open_clamp(mold_id)
            else:
                token = vent_token or press.steam.vent_token()
                if token is None or not token.is_safe():  # pragma: no cover - 计划已保证先泄压
                    raise SequenceBlocked(
                        "缺少安全泄压凭据，禁止开模",
                        component="sequence",
                        attempted="open_clamp",
                        missing=["vent_steam"],
                        recover={"forward": ["vent_steam", "open_clamp"]},
                    )
                press.transition(PressPhase.OPENING)
                opened = open_mold(press.lift, token, self.bus, mold_id)
                opening = opened["opening_pct"]
            return {"key": key, "ok": True, "opening_pct": round(opening, 3)}
        elif key == "remove_part":
            press.clear_mold()
        elif key == "reset_latch":
            entry = self.latch.reset(mold_id, self.clock.now())
            return {"key": key, "ok": True, "released": bool(entry and entry.released)}
        elif key == "finalize":
            press.finalize_cycle()
            press.idle()
        return {"key": key, "ok": True, "phase": press.phase.value}


def _cannot_retreat(press: "CuringPress", phase: PressPhase) -> SequenceBlocked:
    return SequenceBlocked(
        f"硫化机 {press.press_id} 已在 {phase.value}，不能带硫化倒退，请先补齐计时完工",
        component="sequence",
        attempted="retreat",
        missing=["finish_cure"],
        recover={
            "forward": ["finish_cure", "steam_vent", "lift_open", "part_removed", "latch_reset"],
        },
    )
