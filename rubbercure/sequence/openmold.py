"""开模顺序。

开模顺序固定为：计时完成、蒸汽泄压、开模到位、取件、按模复位完成闩锁。
先泄压再开模是硬约束，取件之后才能复位闩锁——顺序错了要么顶裂制品，要么
让调度误判哪台机可以接下一模。每一步物理动作之前先过 :class:`OrderGuard`
闸门：顺序不对当场拦截并给出退回合规检查点的方案。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..clock import Clock
from ..eventbus import Event, EventBus
from ..lift.open import open_mold
from ..press.press import CuringPress
from ..press.transitions import PressPhase
from ..timer.done import complete_cure
from ..timer.latch import CureLatch
from .guard import OrderGuard, RecoveryPlan, SequenceBlocked
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
    guard: OrderGuard = field(init=False)
    _pending: SequenceBlocked | None = field(default=None, init=False)
    _mold_id: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.recorder = SequenceRecorder(self.bus, "openmold")
        self.guard = OrderGuard(
            "openmold",
            OPEN_ORDER,
            descriptions={
                "undo_cooling_start": "停冷却循环泵，冷却相位退回待机",
                "undo_steam_vent": "收起泄压凭据，蒸汽相位退回泄压前",
                "undo_lift_open": "重新合模，硫化机相位退回泄压完成",
                "undo_part_removed": "把模具重新挂回本机，尚未取件",
                "undo_latch_reset": "重新置位该模完成闩锁",
                "perform_steam_vent": "泄压到安全值以下并取得凭据",
                "perform_lift_open": "泄压后重新开模到取件位",
            },
        )

    def prepare(self) -> None:
        self.guard.reset()
        self.recorder.clear()
        self._pending = None
        self._mold_id = None

    def run(self) -> dict:
        """按开模顺序逐步执行，返回执行报告。"""

        self.prepare()
        for step in self.guard.order:
            self.advance(step)
        return self.report(self._mold_id or "")

    def advance(self, step: str) -> dict:
        """执行开模顺序中的一步；顺序不对当场被闸门拦住。"""

        if self._mold_id is None:
            self._mold_id = self.press.require_mold()
        self.guard.authorize(step)
        OPEN_STEPS[step](self, self._mold_id)
        return {"step": step, "executed": self.guard.executed()}

    def recover(self) -> dict:
        """沿被拦时给出的方案退回合规检查点，补齐缺失步并重试被拦动作。"""

        if self._pending is None:
            raise RuntimeError("当前没有被闸门拦截的动作，无需退回")
        mold_id = self._mold_id or self.press.press_id
        recovery = self._pending.plan
        rolled = self.guard.rollback_to_compliant(recovery)
        self.recorder.prune_to(self.guard.executed())
        for item in recovery.complete_missing:
            self.advance(item.step)
        retried = recovery.blocked_step
        self.advance(retried)
        self._pending = None
        self.bus.publish(
            Event.of(
                "openmold",
                "sequence",
                "sequence_recovered",
                subject=mold_id,
                severity="notice",
                blocked_step=retried,
                rolled_back=rolled,
                completed=[item.step for item in recovery.complete_missing],
                checkpoint=list(recovery.checkpoint),
            )
        )
        return {"blocked_step": retried, "rolled_back": rolled, "executed": self.guard.executed()}

    @property
    def blocked(self) -> SequenceBlocked | None:
        return self._pending

    def note_blocked(self, error: SequenceBlocked) -> None:
        """登记一次拦截并发布事件，供平台层审计与退回复用。"""

        self._pending = error
        self.bus.publish(
            Event.of(
                "openmold",
                "sequence",
                "sequence_blocked",
                subject=error.plan.blocked_step,
                severity="warning",
                executed=self.guard.executed(),
                **{"recovery": error.plan.as_dict()},
            )
        )

    def _do_timer_complete(self, mold_id: str) -> None:
        if self.press.phase in (PressPhase.CURING, PressPhase.COOLING):
            self.press.transition(PressPhase.VENTING)
        finished = complete_cure(self.press.timer, self.latch, self.bus)
        self.guard.mark_done(
            "timer_complete",
            lambda: (self.latch.revoke(mold_id), self.press.rollback_phase()),
        )
        self.recorder.note(
            "timer_complete",
            "timer",
            mold_id=mold_id,
            verdict=finished["verdict"],
            latch_set=finished["latch_set"],
        )

    def _do_cooling_start(self, mold_id: str) -> None:
        cooling = self.press.cooling.start_cooling(mold_id)
        self.guard.mark_done(
            "cooling_start", lambda: self.press.cooling.abort_cooling(mold_id)
        )
        self.recorder.note("cooling_start", "cool", mold_id=mold_id, flow_m3_h=cooling["flow_m3_h"])

    def _do_steam_vent(self, mold_id: str) -> None:
        vent = self.press.steam.vent_pressure(mold_id)
        self.guard.mark_done("steam_vent", lambda: self.press.steam.abort_vent(mold_id))
        self.recorder.note(
            "steam_vent", "steam", mold_id=mold_id, residual_kpa=round(vent.residual_kpa, 3)
        )

    def _do_lift_open(self, mold_id: str) -> None:
        if self.press.phase is PressPhase.VENTING:
            self.press.transition(PressPhase.OPENING)
        vent = self.press.steam.vent_token()
        opened = open_mold(self.press.lift, vent, self.bus, mold_id)

        def undo() -> None:
            self.press.lift.close_clamp(mold_id)
            self.press.rollback_phase()

        self.guard.mark_done("lift_open", undo)
        self.recorder.note(
            "lift_open", "lift", mold_id=mold_id, opening_pct=opened["opening_pct"]
        )

    def _do_part_removed(self, mold_id: str) -> None:
        self.press.clear_mold()
        self.guard.mark_done("part_removed", lambda: self.press.restore_mold(mold_id))
        self.recorder.note("part_removed", "press", mold_id=mold_id)

    def _do_latch_reset(self, mold_id: str) -> None:
        entry = self.latch.reset(mold_id, self.clock.now())
        self.guard.mark_done(
            "latch_reset",
            lambda: self.latch.set(mold_id, self.clock.now()),
        )
        self.recorder.note(
            "latch_reset",
            "timer",
            mold_id=mold_id,
            released=bool(entry and entry.released),
        )
        self.press.finalize_cycle()
        self.press.idle()

    def report(self, mold_id: str) -> dict:
        return {
            "mold_id": mold_id,
            "steps": self.guard.executed(),
            "violations": verify_order(self.guard.executed(), OPEN_ORDER),
            "latch": self.latch.snapshot(),
            "press_phase": self.press.phase.value,
        }


# 步骤名 → 物理动作的显式分发表（同硫化顺序）。
OPEN_STEPS: dict[str, object] = {
    "timer_complete": OpenSequence._do_timer_complete,
    "cooling_start": OpenSequence._do_cooling_start,
    "steam_vent": OpenSequence._do_steam_vent,
    "lift_open": OpenSequence._do_lift_open,
    "part_removed": OpenSequence._do_part_removed,
    "latch_reset": OpenSequence._do_latch_reset,
}
