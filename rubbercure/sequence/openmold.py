"""开模顺序。

开模顺序固定为：计时完成、蒸汽泄压、开模到位、取件、按模复位完成闩锁。
先泄压再开模是硬约束，取件之后才能复位闩锁——顺序错了要么顶裂制品，要么
让调度误判哪台机可以接下一模。每个设备动作执行前先过顺序闸口，缺前置就
当场拦下，并给出前向补齐或安全退回路径，而不是等走完再记账。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..clock import Clock
from ..eventbus import EventBus
from ..lift.open import open_mold
from ..press.press import CuringPress
from ..press.transitions import PressPhase
from ..timer.done import complete_cure
from ..timer.latch import CureLatch
from ..timer.state import TimerPhase
from .recovery import RecoveryPlanner
from .selfcheck import SequenceRecorder, verify_order

OPEN_ORDER: tuple[tuple[str, str, str], ...] = (
    ("timer_complete", "cooling_start", "计时完成后先启动模温冷却"),
    ("cooling_start", "steam_vent", "冷却启动后才允许泄压"),
    ("steam_vent", "lift_open", "先泄压再开模"),
    ("lift_open", "part_removed", "开模到位后才取件"),
    ("part_removed", "latch_reset", "取件后按模复位完成闩锁"),
)


@dataclass
class OpenSequence:
    """开模顺序执行器。"""

    clock: Clock
    bus: EventBus
    latch: CureLatch
    press: CuringPress
    recorder: SequenceRecorder = field(init=False)

    def __post_init__(self) -> None:
        self.recorder = SequenceRecorder(
            self.bus, "openmold", OPEN_ORDER, recovery=self._build_recovery
        )

    def _build_recovery(self, attempted: str, missing: list[str]) -> dict:
        """闸口拦下时，按硫化机真实相位给一条可执行的安全退回路径。"""

        return RecoveryPlanner(self.press, self.clock, self.bus, self.latch).plan()

    def run(self) -> dict:
        """按开模顺序执行，返回执行报告。"""

        self.recorder.clear()
        press = self.press
        mold_id = press.require_mold()

        self.recorder.require("timer_complete")
        if press.phase in (PressPhase.CURING, PressPhase.COOLING):
            press.transition(PressPhase.VENTING)
        timer_phase = press.timer.phase
        if timer_phase is TimerPhase.CURING:
            finished = complete_cure(press.timer, self.latch, self.bus)
            verdict = finished["verdict"]
            latch_set = finished["latch_set"]
        elif timer_phase is TimerPhase.COMPLETE:
            # 计时已由前向完工补完（闩锁已置位），直接据此继续开模。
            verdict = "already_complete"
            latch_set = self.latch.is_set(mold_id)
        else:
            # 计时根本没开始，不能把它"手动置为完成"；当场拦下并给出
            # 前向（补做硫化）与安全退回（卸模回待机）两条路径。
            raise self.recorder.guard.refuse(
                "timer_complete",
                f"模具 {mold_id} 尚未完成硫化计时（当前 {timer_phase.value}）",
                missing=["timer_start"],
                forward=[
                    "mold_registered",
                    "mapping_registered",
                    "cavity_purge",
                    "lift_close",
                    "steam_open",
                    "temp_confirm",
                    "timer_start",
                    "timer_complete",
                ],
            )
        self.recorder.note(
            "timer_complete",
            "timer",
            mold_id=mold_id,
            verdict=verdict,
            latch_set=latch_set,
        )

        self.recorder.require("cooling_start")
        cooling = press.cooling.start_cooling(mold_id)
        self.recorder.note("cooling_start", "cool", mold_id=mold_id, flow_m3_h=cooling["flow_m3_h"])

        self.recorder.require("steam_vent")
        vent = press.steam.vent_pressure(mold_id)
        self.recorder.note(
            "steam_vent", "steam", mold_id=mold_id, residual_kpa=round(vent.residual_kpa, 3)
        )

        self.recorder.require("lift_open")
        press.transition(PressPhase.OPENING)
        opened = open_mold(press.lift, vent, self.bus, mold_id)
        self.recorder.note(
            "lift_open", "lift", mold_id=mold_id, opening_pct=opened["opening_pct"]
        )

        self.recorder.require("part_removed")
        press.clear_mold()
        self.recorder.note("part_removed", "press", mold_id=mold_id)

        self.recorder.require("latch_reset")
        entry = self.latch.reset(mold_id, self.clock.now())
        self.recorder.note(
            "latch_reset",
            "timer",
            mold_id=mold_id,
            released=bool(entry and entry.released),
        )

        press.finalize_cycle()
        press.idle()
        return self.report(mold_id)

    def report(self, mold_id: str) -> dict:
        return {
            "mold_id": mold_id,
            "steps": self.recorder.names(),
            "violations": verify_order(self.recorder.names(), OPEN_ORDER),
            "latch": self.latch.snapshot(),
            "press_phase": self.press.phase.value,
        }
