"""停运顺序。

停运顺序固定为：停主蒸汽阀、放残压、排冷却水、停冷却循环。先停汽再停冷却
是硬约束：带着蒸汽停冷却会让模具局部骤冷、产生内应力。每一步物理动作之前
先过 :class:`OrderGuard` 闸门，任何提前停冷却的操作当场被拦截，并给出撤回
越序步、补齐缺失步的退回方案。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..clock import Clock
from ..eventbus import Event, EventBus
from ..cool.stop import stop_cooling
from ..press.press import CuringPress
from .guard import OrderGuard, RecoveryPlan, SequenceBlocked
from .selfcheck import SequenceRecorder, verify_order

STOP_ORDER: tuple[tuple[str, str, str], ...] = (
    ("steam_stop", "steam_vent", "先停主蒸汽阀再放残压"),
    ("steam_vent", "coolant_drain", "残压放净后才排冷却水"),
    ("coolant_drain", "cool_stop", "排空之后才停冷却循环"),
    ("steam_stop", "cool_stop", "停汽必须先于停冷却"),
)


@dataclass
class StopSequence:
    """停运顺序执行器。"""

    clock: Clock
    bus: EventBus
    press: CuringPress
    recorder: SequenceRecorder = field(init=False)
    guard: OrderGuard = field(init=False)
    _pending: SequenceBlocked | None = field(default=None, init=False)
    _mold_id: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.recorder = SequenceRecorder(self.bus, "stop")
        self.guard = OrderGuard(
            "stop",
            STOP_ORDER,
            descriptions={
                "undo_steam_stop": "重新打开疏水阀与主蒸汽阀，恢复停运前蒸汽相位",
                "undo_steam_vent": "收起泄压凭据，蒸汽相位退回泄压前",
                "undo_coolant_drain": "冷却相位退回循环，按规程重新建立水循环",
                "perform_steam_vent": "重新放残压到安全值以下",
                "perform_coolant_drain": "重新排空冷却管路",
                "perform_cool_stop": "排空并停泵后重新执行停冷却",
            },
        )

    def prepare(self) -> None:
        self.guard.reset()
        self.recorder.clear()
        self._pending = None

    def run(self) -> dict:
        """按停运顺序逐步执行，返回执行报告。"""

        self.prepare()
        self._mold_id = self.press.mold_id or self.press.press_id
        for step in self.guard.order:
            self.advance(step)
        return self.report(self._mold_id)

    def advance(self, step: str) -> dict:
        """执行停运顺序中的一步；顺序不对当场被闸门拦住。"""

        if not self._mold_id:
            self._mold_id = self.press.mold_id or self.press.press_id
        self.guard.authorize(step)
        STOP_STEPS[step](self, self._mold_id)
        return {"step": step, "executed": self.guard.executed()}

    def recover(self) -> dict:
        """沿被拦时给出的方案退回合规检查点，补齐缺失步并重试被拦动作。"""

        if self._pending is None:
            raise RuntimeError("当前没有被闸门拦截的动作，无需退回")
        recovery = self._pending.plan
        mold_id = self._mold_id
        rolled = self.guard.rollback_to_compliant(recovery)
        self.recorder.prune_to(self.guard.executed())
        for item in recovery.complete_missing:
            self.advance(item.step)
        retried = recovery.blocked_step
        self.advance(retried)
        self._pending = None
        self.bus.publish(
            Event.of(
                "stop",
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
                "stop",
                "sequence",
                "sequence_blocked",
                subject=error.plan.blocked_step,
                severity="warning",
                executed=self.guard.executed(),
                **{"recovery": error.plan.as_dict()},
            )
        )

    def _do_steam_stop(self, mold_id: str) -> None:
        closed = self.press.steam.close_steam()
        target = self.press.steam.limits.target_pressure_kpa

        def undo() -> None:
            self.press.steam.open_steam(target)

        self.guard.mark_done("steam_stop", undo)
        self.recorder.note(
            "steam_stop", "steam", mold_id=mold_id,
            valve_opening_pct=closed["valve_opening_pct"],
        )

    def _do_steam_vent(self, mold_id: str) -> None:
        vent = self.press.steam.vent_pressure(mold_id)
        self.guard.mark_done("steam_vent", lambda: self.press.steam.abort_vent(mold_id))
        self.recorder.note(
            "steam_vent", "steam", mold_id=mold_id, residual_kpa=round(vent.residual_kpa, 3)
        )

    def _do_coolant_drain(self, mold_id: str) -> None:
        drained = self.press.cooling.drain()
        self.guard.mark_done("coolant_drain", self.press.cooling.abort_drain)
        self.recorder.note(
            "coolant_drain", "cool", mold_id=mold_id, residual_m3=drained["residual_m3"]
        )

    def _do_cool_stop(self, mold_id: str) -> None:
        stopped = stop_cooling(self.press.cooling, self.press.steam, self.bus)
        self.guard.mark_done("cool_stop", self.press.cooling.abort_stop)
        self.recorder.note("cool_stop", "cool", mold_id=mold_id, flow_m3_h=stopped["flow_m3_h"])

    def report(self, mold_id: str) -> dict:
        return {
            "mold_id": mold_id,
            "steps": self.guard.executed(),
            "violations": verify_order(self.guard.executed(), STOP_ORDER),
            "steam_pressure_kpa": round(self.press.steam.pressure_kpa, 3),
            "cooling": self.press.cooling.snapshot(),
        }


# 步骤名 → 物理动作的显式分发表（同硫化顺序）。
STOP_STEPS: dict[str, object] = {
    "steam_stop": StopSequence._do_steam_stop,
    "steam_vent": StopSequence._do_steam_vent,
    "coolant_drain": StopSequence._do_coolant_drain,
    "cool_stop": StopSequence._do_cool_stop,
}
