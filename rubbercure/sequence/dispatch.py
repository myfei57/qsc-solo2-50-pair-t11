"""机群调度顺序。

调度顺序固定为：完成上报、顺序确认、下一模分配、写审计。完成上报来自各台
硫化机，顺序确认把并发上报排成可复现顺序，再据此挑空闲机位分配下一模，
最后把这次派工写进审计。顺序确认必须先于分配，否则下一模会跟着线程调度
随机挑机。每一步物理动作之前先过 :class:`OrderGuard` 闸门，顺序不对当场
拦截并给出退回方案。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..batch.batch import BatchScheduler
from ..batch.ordering import order_completions, ordering_keys
from ..batch.schedule import schedule_next
from ..clock import Clock
from ..eventbus import Event, EventBus
from ..mold.registry import MoldRegistry
from ..press.fleet import PressFleet
from ..press.press import CuringPress
from .guard import OrderGuard, RecoveryPlan, SequenceBlocked
from .selfcheck import SequenceRecorder, verify_order

DISPATCH_ORDER: tuple[tuple[str, str, str], ...] = (
    ("completion_report", "order_confirm", "完成上报后才做顺序确认"),
    ("order_confirm", "mold_assigned", "顺序确认后才分配下一模"),
    ("mold_assigned", "audit_note", "分配之后才写调度审计"),
)


@dataclass
class DispatchSequence:
    """机群调度顺序执行器。"""

    clock: Clock
    bus: EventBus
    registry: MoldRegistry
    scheduler: BatchScheduler
    fleet: PressFleet
    press: CuringPress
    recorder: SequenceRecorder = field(init=False)
    guard: OrderGuard = field(init=False)
    _pending: SequenceBlocked | None = field(default=None, init=False)
    _mold_id: str = field(default="", init=False)
    _assignment: object = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.recorder = SequenceRecorder(self.bus, "dispatch")
        self.guard = OrderGuard(
            "dispatch",
            DISPATCH_ORDER,
            descriptions={
                "undo_completion_report": "撤回最近一条完成上报",
                "undo_mold_assigned": "把分配任务退回队列并从机位摘下",
                "perform_order_confirm": "先对完成上报做顺序确认",
                "perform_mold_assigned": "确认后重新挑空闲机位分配下一模",
                "perform_audit_note": "分配后补写调度审计",
            },
        )

    def prepare(self) -> None:
        self.guard.reset()
        self.recorder.clear()
        self._pending = None
        self._mold_id = ""
        self._assignment = None

    def run(self) -> dict:
        """按调度顺序逐步执行，返回执行报告。"""

        self.prepare()
        timer = self.press.timer.snapshot()
        self._mold_id = str(timer.get("mold_id") or self.press.press_id)
        for step in self.guard.order:
            self.advance(step)
        return self.report(self._mold_id, self._assignment)

    def advance(self, step: str) -> dict:
        """执行调度顺序中的一步；顺序不对当场被闸门拦住。"""

        if not self._mold_id:
            timer = self.press.timer.snapshot()
            self._mold_id = str(timer.get("mold_id") or self.press.press_id)
        self.guard.authorize(step)
        DISPATCH_STEPS[step](self, self._mold_id)
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
                "dispatch",
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
                "dispatch",
                "sequence",
                "sequence_blocked",
                subject=error.plan.blocked_step,
                severity="warning",
                executed=self.guard.executed(),
                **{"recovery": error.plan.as_dict()},
            )
        )

    def _do_completion_report(self, mold_id: str) -> None:
        timer = self.press.timer.snapshot()
        product_code = str(timer.get("product_code") or "")
        generation = int(timer.get("curve_generation") or 0)
        report = self.scheduler.record_report(
            self.press.press_id,
            mold_id,
            product_code,
            self.clock.now(),
            generation,
        )

        def undo() -> None:
            self.scheduler.withdraw_last_report()

        self.guard.mark_done("completion_report", undo)
        self.recorder.note(
            "completion_report",
            "batch",
            press_id=self.press.press_id,
            mold_id=mold_id,
            sequence=report.sequence,
        )

    def _do_order_confirm(self, mold_id: str) -> None:
        ordered = order_completions(
            self.scheduler.recent_reports(),
            self.scheduler.limits.dispatch_window_s,
        )
        self.guard.mark_done("order_confirm", None)
        self.recorder.note(
            "order_confirm", "batch", confirmed=len(ordered),
            window_s=self.scheduler.limits.dispatch_window_s,
        )

    def _do_mold_assigned(self, mold_id: str) -> None:
        assignment = schedule_next(
            self.scheduler,
            self.fleet,
            self.scheduler.queue,
            self.registry,
            self.bus,
            self.clock.now(),
        )
        self._assignment = assignment
        if assignment is not None:
            self.guard.mark_done(
                "mold_assigned",
                lambda: self.scheduler.return_assignment(assignment, self.fleet),
            )
        else:
            self.guard.mark_done("mold_assigned", None)
        self.recorder.note(
            "mold_assigned",
            "batch",
            press_id=assignment.press_id if assignment else "",
            mold_id=assignment.mold_id if assignment else "",
        )

    def _do_audit_note(self, mold_id: str) -> None:
        ordered = order_completions(
            self.scheduler.recent_reports(),
            self.scheduler.limits.dispatch_window_s,
        )
        assignment = self._assignment
        self.bus.publish(
            Event.of(
                "batch",
                "batch",
                "dispatch_ordered",
                subject=mold_id,
                severity="info",
                ordered=[item.mold_id for item in ordered],
                ordering=ordering_keys(ordered),
                assigned=(assignment.as_dict() if assignment else None),
            )
        )
        self.guard.mark_done("audit_note", None)
        self.recorder.note("audit_note", "audit", mold_id=mold_id)

    def report(self, mold_id: str, assignment) -> dict:
        return {
            "mold_id": mold_id,
            "steps": self.guard.executed(),
            "violations": verify_order(self.guard.executed(), DISPATCH_ORDER),
            "queued": self.scheduler.queue.entries(),
            "assigned": assignment.as_dict() if assignment else None,
            "scheduler_phase": self.scheduler.phase.value,
        }


# 步骤名 → 物理动作的显式分发表（同硫化顺序）。
DISPATCH_STEPS: dict[str, object] = {
    "completion_report": DispatchSequence._do_completion_report,
    "order_confirm": DispatchSequence._do_order_confirm,
    "mold_assigned": DispatchSequence._do_mold_assigned,
    "audit_note": DispatchSequence._do_audit_note,
}
