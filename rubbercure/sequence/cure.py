"""硫化顺序。

硫化顺序按设计文档固定为：模具登记、传感器映射登记、合模前排气、合模锁模、
通汽升温、模温确认落盘、启动硫化计时。顺序约束不再只靠运行结束后的
``verify_order`` 自查：每一步物理动作**之前**都先过 :class:`OrderGuard`
闸门，前置步缺失或有越序步已经落地时当场抛 :class:`SequenceBlocked`，
机器不会往下走；异常携带退回方案，:meth:`CureSequence.recover` 按方案把
越序步逆序撤回合规检查点、补齐缺失步后自动重试被拦动作。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..batch.profile import BatchProfile
from ..clock import Clock
from ..config import PlatformConfig
from ..eventbus import Event, EventBus
from ..lift.close import close_mold
from ..mold.mapping import SensorMapBook
from ..mold.registry import MoldRegistry
from ..press.press import CuringPress
from ..press.transitions import PressPhase
from ..temp.read import read_mold_temp
from ..timer.curve import CurveBook
from ..timer.start import start_cure
from .guard import OrderGuard, RecoveryPlan, SequenceBlocked
from .selfcheck import SequenceRecorder, verify_order, verify_topology

CURE_ORDER: tuple[tuple[str, str, str], ...] = (
    ("mold_registered", "mapping_registered", "模具登记必须先于传感器映射登记"),
    ("mapping_registered", "cavity_purge", "排气必须按已登记的映射代际进行"),
    ("cavity_purge", "lift_close", "合模前必须先排气"),
    ("lift_close", "steam_open", "合模到位后才允许通汽升温"),
    ("steam_open", "temp_confirm", "通汽升温后才可能确认模温"),
    ("temp_confirm", "timer_start", "模温确认落盘后才允许启动硫化计时"),
    ("mold_registered", "lift_close", "模具登记必须先于合模"),
)

TOPOLOGY_RULES: tuple[tuple[str, str], ...] = (
    ("mold", "lift"),
    ("lift", "steam"),
    ("steam", "temp"),
    ("temp", "timer"),
)


@dataclass(frozen=True)
class CureRunPlan:
    """一次硫化的输入。"""

    mold_id: str
    product_code: str
    cavity_count: int
    thickness_mm: float
    sensors: dict[int, str]
    raw_readings: dict[str, float]
    target_kpa: float
    warmup_start_c: float
    temp_hold_s: float


@dataclass
class CureSequence:
    """硫化顺序执行器。"""

    clock: Clock
    bus: EventBus
    config: PlatformConfig
    registry: MoldRegistry
    maps: SensorMapBook
    curves: CurveBook
    press: CuringPress
    last_confirmation: dict = field(default_factory=dict)
    recorder: SequenceRecorder = field(init=False)
    guard: OrderGuard = field(init=False)
    plan: CureRunPlan | None = field(default=None, init=False)
    _pending: SequenceBlocked | None = field(default=None, init=False)
    _profile: BatchProfile | None = field(default=None, init=False)
    _mapping_generation: int = field(default=0, init=False)
    _warmup: dict = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.recorder = SequenceRecorder(self.bus, "cure")
        self.guard = OrderGuard(
            "cure",
            CURE_ORDER,
            descriptions={
                "undo_lift_close": "重新开模并把硫化机相位退回装模完成",
                "undo_steam_open": "关主蒸汽阀与疏水阀，相位退回合模",
                "undo_temp_confirm": "删除模温确认落盘并清内存确认",
                "undo_timer_start": "中止硫化计时，相位退回升温",
                "perform_cavity_purge": "按当前映射代际重新排气",
                "perform_lift_close": "排气后重新合模锁模",
                "perform_temp_confirm": "重新确认模温并落盘",
            },
        )

    def prepare(self, plan: CureRunPlan) -> None:
        """装入本次硫化计划并重置闸门与记录。"""

        self.plan = plan
        self.guard.reset()
        self.recorder.clear()
        self._pending = None
        self._profile = None
        self._mapping_generation = 0
        self._warmup = {}

    def run(self, plan: CureRunPlan) -> dict:
        """按硫化顺序逐步执行，返回执行报告。"""

        self.prepare(plan)
        for step in self.guard.order:
            self.advance(step)
        return self.report(plan)

    def advance(self, step: str) -> dict:
        """执行硫化顺序中的一步；顺序不对当场被闸门拦住。"""

        plan = self._require_plan()
        self.guard.authorize(step)
        performer = CURE_STEPS[step]
        performer(self, plan)
        return {"step": step, "executed": self.guard.executed()}

    def recover(self) -> dict:
        """沿被拦时给出的方案退回合规检查点，补齐缺失步并重试被拦动作。"""

        plan = self._require_plan()
        blocked = self._pending
        if blocked is None:
            raise RuntimeError("当前没有被闸门拦截的动作，无需退回")
        recovery = blocked.plan
        rolled = self.guard.rollback_to_compliant(recovery)
        self._sync_recorder()
        for item in recovery.complete_missing:
            self.advance(item.step)
        retried = recovery.blocked_step
        self.advance(retried)
        self._pending = None
        self.bus.publish(
            Event.of(
                "cure",
                "sequence",
                "sequence_recovered",
                subject=plan.mold_id,
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

    def _require_plan(self) -> CureRunPlan:
        if self.plan is None:
            raise RuntimeError("尚未装入硫化计划，先调用 prepare/run")
        return self.plan

    def _do_mold_registered(self, plan: CureRunPlan) -> None:
        now = self.clock.now()
        record = self.registry.register(
            plan.mold_id, plan.product_code, plan.cavity_count, now
        )
        self.guard.mark_done(
            "mold_registered",
            lambda: self.registry.withdraw_latest(plan.mold_id),
        )
        self.recorder.note(
            "mold_registered", "mold", mold_id=plan.mold_id, generation=record.generation
        )

    def _do_mapping_registered(self, plan: CureRunPlan) -> None:
        now = self.clock.now()
        mapping = self.maps.register(plan.mold_id, plan.sensors, now)
        self._mapping_generation = mapping.generation
        self.guard.mark_done(
            "mapping_registered",
            lambda: self.maps.withdraw_latest(plan.mold_id),
        )
        self.recorder.note(
            "mapping_registered", "mold", mold_id=plan.mold_id, generation=mapping.generation
        )

    def _do_cavity_purge(self, plan: CureRunPlan) -> None:
        if self._profile is None:
            self.press.load_mold(plan.mold_id)
            self._profile = BatchProfile.resolve(
                plan.mold_id, plan.thickness_mm, self.registry, self.curves
            )
        purge = self.press.steam.purge_cavity(
            plan.mold_id, generation=self._mapping_generation
        )
        self.guard.mark_done(
            "cavity_purge", lambda: self.press.steam.abort_purge(plan.mold_id)
        )
        self.recorder.note(
            "cavity_purge", "steam", mold_id=plan.mold_id, hold_s=round(purge.hold_s, 3)
        )

    def _do_lift_close(self, plan: CureRunPlan) -> None:
        if self.press.phase in (PressPhase.MOLD_LOADED,):
            self.press.transition(PressPhase.CLAMPING)
        purge = self.press.steam.purge_token()
        closed = close_mold(
            self.press.lift,
            purge,
            self.bus,
            plan.mold_id,
            now=self.clock.now(),
            max_age_s=self.config.steam.purge_token_max_age_s,
        )
        self.guard.mark_done("lift_close", lambda: self.press.reopen_after_close(plan.mold_id))
        self.recorder.note(
            "lift_close", "lift", mold_id=plan.mold_id, clamp_force_kn=closed["clamp_force_kn"]
        )

    def _do_steam_open(self, plan: CureRunPlan) -> None:
        if self.press.phase is PressPhase.CLAMPING:
            self.press.transition(PressPhase.HEATING)
        steam = self.press.steam.open_steam(plan.target_kpa)
        self.guard.mark_done("steam_open", self.press.abort_heating)
        self.recorder.note(
            "steam_open",
            "steam",
            mold_id=plan.mold_id,
            valve_opening_pct=steam["valve_opening_pct"],
            trap_opening_pct=steam["trap_opening_pct"],
        )

    def _do_temp_confirm(self, plan: CureRunPlan) -> None:
        warmup = self.press.temp_loop.warmup(plan.warmup_start_c, self.clock)
        self._warmup = warmup
        reading = read_mold_temp(
            plan.mold_id,
            self.maps.latest_for(plan.mold_id),
            plan.raw_readings,
            self.clock.now(),
        )
        confirmation = self.press.temp_loop.confirm(reading, plan.temp_hold_s)
        self.guard.mark_done(
            "temp_confirm", lambda: self.press.temp_persister.revoke(plan.mold_id)
        )
        self.recorder.note(
            "temp_confirm",
            "temp",
            mold_id=plan.mold_id,
            confirmed_c=round(confirmation.confirmed_c, 3),
            generation=confirmation.generation,
        )
        self.last_confirmation = confirmation.as_dict()

    def _do_timer_start(self, plan: CureRunPlan) -> None:
        profile = self._profile
        if profile is None:  # pragma: no cover - 由闸门保证排气已完成
            raise RuntimeError("缺少配方基线，计时启动前必须先完成排气")
        self.press.lift.require_closed()
        self.press.timer.set_clock_now(self.clock.now())
        started = start_cure(
            self.press.timer,
            self.press.temp_persister,
            plan.mold_id,
            profile.planned_s,
            self.bus,
            product_code=profile.product_code,
            generation=profile.curve_generation,
        )
        self.guard.mark_done("timer_start", lambda: self.press.abort_curing(plan.mold_id))
        self.recorder.note(
            "timer_start",
            "timer",
            mold_id=plan.mold_id,
            planned_s=started["planned_s"],
            curve_generation=started["curve_generation"],
            confirmed=self.press.temp_loop.is_confirmed(),
        )
        self.press.transition(PressPhase.CURING)

    def note_blocked(self, error: SequenceBlocked) -> None:
        """登记一次拦截并发布事件，供平台层审计与退回复用。"""

        self._pending = error
        self.bus.publish(
            Event.of(
                "cure",
                "sequence",
                "sequence_blocked",
                subject=error.plan.blocked_step,
                severity="warning",
                executed=self.guard.executed(),
                **{"recovery": error.plan.as_dict()},
            )
        )

    def _sync_recorder(self) -> None:
        """撤回后把记录器对齐到闸门当前已执行的步骤。"""

        self.recorder.prune_to(self.guard.executed())

    def report(self, plan: CureRunPlan, warmup: dict | None = None) -> dict:
        """汇总本次硫化执行情况。"""

        return {
            "mold_id": plan.mold_id,
            "steps": self.guard.executed(),
            "violations": verify_order(self.guard.executed(), CURE_ORDER),
            "trace": self.recorder.as_list(),
            "warmup": warmup if warmup is not None else self._warmup,
            "timer": self.press.timer.snapshot(),
        }

    def verify_topology(self, topology) -> list[str]:
        """确认链路拓扑里声明了硫化的上下游关系。"""

        return verify_topology(topology, TOPOLOGY_RULES)


# 步骤名 → 物理动作的显式分发表：顺序闸门按步骤授权，动作本体集中在这里，
# 既避免 getattr 动态分发绕过静态检查，也让"每一步对应哪个动作"一目了然。
CURE_STEPS: dict[str, object] = {
    "mold_registered": CureSequence._do_mold_registered,
    "mapping_registered": CureSequence._do_mapping_registered,
    "cavity_purge": CureSequence._do_cavity_purge,
    "lift_close": CureSequence._do_lift_close,
    "steam_open": CureSequence._do_steam_open,
    "temp_confirm": CureSequence._do_temp_confirm,
    "timer_start": CureSequence._do_timer_start,
}
