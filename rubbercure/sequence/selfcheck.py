"""顺序闸口、记录与自查。

四个顺序（硫化、开模、调度、停运）都共用同一套闸口与自查：

* **事前闸口**（``SequenceGuard.require``）：每个设备动作执行前，先核对它
  依赖的前置步骤是否已完成。缺前置就抛 :class:`SequenceBlocked`，动作根本
  不会发生，同时随异常给出前向补齐与（可选的）安全退回两条合规路径；
* **事中记录**（``SequenceRecorder.note``）：动作成功后才记录步骤并发布
  事件，因此记录里的每一步都对应一次真实成功的设备动作；
* **事后自查**（``verify_order``）：运行完成后按显式的 (先, 后, 说明)
  三元组再对一遍，作为闸口之外的第二道防线，任何一步提前仍会被指出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..errors import SequenceBlocked
from ..eventbus import Event, EventBus
from ..ns.topology import Topology

Rules = tuple[tuple[str, str, str], ...]
RecoveryBuilder = Callable[[str, list[str]], dict]


@dataclass(frozen=True)
class OrderStep:
    """一条已执行步骤。"""

    name: str
    component: str
    index: int
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "component": self.component,
            "index": self.index,
            "detail": self.detail,
        }


class SequenceGuard:
    """顺序闸口：按规则在动作执行前判定能否放行。

    规则是显式的 ``(先, 后, 说明)`` 三元组。闸口只认已完成步骤集合，
    因此顺序错（跳过前置、把后置提前）都会在设备动作之前被拦下。
    """

    def __init__(
        self,
        topic: str,
        rules: Rules,
        bus: EventBus,
        recovery: RecoveryBuilder | None = None,
    ) -> None:
        self._topic = topic
        self._rules = rules
        self._bus = bus
        self._recovery = recovery
        self._done: set[str] = set()

    @property
    def topic(self) -> str:
        return self._topic

    def missing_for(self, step: str) -> list[str]:
        """返回执行 ``step`` 前还没做的直接前置步骤。"""

        return [earlier for earlier, later, _ in self._rules if later == step and earlier not in self._done]

    def block(self, step: str, missing: list[str]) -> SequenceBlocked:
        """构造带恢复路径的阻断异常并发布告警事件。"""

        descriptions = [
            description
            for earlier, later, description in self._rules
            if later == step and earlier in missing
        ]
        head = descriptions[0] if descriptions else f"步骤 {step} 的前置条件未满足"
        recover: dict = {"forward": self.forward_path(step, missing)}
        if self._recovery is not None:
            retreat = self._recovery(step, missing)
            if retreat:
                recover["retreat"] = retreat
        event = Event.of(
            self._topic,
            "sequence",
            "sequence_blocked",
            subject=step,
            severity="alarm",
            attempted_step=step,
            missing_steps=list(missing),
            reason=head,
            recover=recover,
        )
        self._bus.publish(event)
        return SequenceBlocked(
            f"{head}：缺少前置步骤 {', '.join(missing)}，{step} 被闸口拦下",
            component="sequence",
            attempted=step,
            missing=missing,
            recover=recover,
        )

    def require(self, step: str) -> None:
        """设备动作前的放行检查；前置缺失则当场阻断。"""

        missing = self.missing_for(step)
        if missing:
            raise self.block(step, missing)

    def refuse(
        self,
        step: str,
        reason: str,
        missing: list[str] | None = None,
        forward: list[str] | None = None,
    ) -> SequenceBlocked:
        """设备态不满足时按闸口规格拒绝（带恢复路径与告警事件）。

        用于规则表表达不了、但属于"工艺没走到"的条件（例如计时根本没开始
        就想完成计时）。``forward`` 可显式给出前向路径，缺省用规则图推导。
        """

        missed = list(missing or [])
        recover: dict = {
            "forward": list(forward) if forward is not None else self.forward_path(step, missed)
        }
        if self._recovery is not None:
            retreat = self._recovery(step, missed)
            if retreat:
                recover["retreat"] = retreat
        self._bus.publish(
            Event.of(
                self._topic,
                "sequence",
                "sequence_blocked",
                subject=step,
                severity="alarm",
                attempted_step=step,
                missing_steps=missed,
                reason=reason,
                recover=recover,
            )
        )
        return SequenceBlocked(
            f"{reason}，{step} 被闸口拦下",
            component="sequence",
            attempted=step,
            missing=missed,
            recover=recover,
        )

    def mark(self, step: str) -> None:
        """登记一个已成功完成的步骤。"""

        self._done.add(step)

    def clear(self) -> None:
        self._done.clear()

    def forward_path(self, step: str, missing: list[str] | None = None) -> list[str]:
        """给出前向补齐路径：按依赖顺序列出缺失前置，最后是被拦步骤本身。

        沿规则图用后序遍历把传递前置也展开（例如 A 在 B 前、B 在 C 前，
        直接做 C 时返回 A、B、C），这样补齐顺序本身也合规，不会补一半又
        被拦。
        """

        roots = list(missing if missing is not None else self.missing_for(step))
        ordered: list[str] = []
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visited or node in self._done:
                return
            visited.add(node)
            for earlier, later, _ in self._rules:
                if later == node:
                    visit(earlier)
            ordered.append(node)

        for root in roots:
            visit(root)
        if step not in ordered:
            ordered.append(step)
        return ordered


class SequenceRecorder:
    """记录顺序步骤并发布事件，内部持有一个事前闸口。"""

    def __init__(
        self,
        bus: EventBus,
        topic: str,
        rules: Rules = (),
        component: str = "sequence",
        recovery: RecoveryBuilder | None = None,
    ) -> None:
        self._bus = bus
        self._topic = topic
        self._component = component
        self._steps: list[OrderStep] = []
        self.guard = SequenceGuard(topic, rules, bus, recovery=recovery)

    @property
    def topic(self) -> str:
        return self._topic

    def require(self, step: str) -> None:
        """动作执行前调用：缺前置步骤则当场阻断，设备不会动作。"""

        self.guard.require(step)

    def note(self, step: str, component: str, **detail: object) -> OrderStep:
        """记录一步（闸口登记已完成）并发布事件。

        仅在设备动作成功之后调用；想在动作前拦截，先调 :meth:`require`。
        """

        self.guard.mark(step)
        record = OrderStep(
            name=step,
            component=component,
            index=len(self._steps) + 1,
            detail=dict(detail),
        )
        self._steps.append(record)
        self._bus.publish(
            Event.of(
                self._topic,
                self._component,
                f"{self._topic}_step",
                subject=step,
                step_index=record.index,
                component_name=component,
                **detail,
            )
        )
        return record

    def names(self) -> list[str]:
        return [record.name for record in self._steps]

    def clear(self) -> None:
        self._steps.clear()
        self.guard.clear()

    def as_list(self) -> list[dict]:
        return [record.as_dict() for record in self._steps]


def verify_order(
    executed: list[str], rules: tuple[tuple[str, str, str], ...]
) -> list[str]:
    """按执行记录自查顺序约束，返回违规说明列表（空表示合规）。"""

    violations: list[str] = []
    for earlier, later, description in rules:
        if earlier not in executed or later not in executed:
            violations.append(f"{description}：步骤 {earlier} 或 {later} 未执行")
            continue
        if executed.index(earlier) > executed.index(later):
            violations.append(f"{description}：实际先执行了 {later}")
    return violations


def verify_topology(topology: Topology, rules: tuple[tuple[str, str], ...]) -> list[str]:
    """确认链路拓扑里确实声明了这些上下游关系。"""

    missing: list[str] = []
    for upstream, downstream in rules:
        if not topology.requires_before(upstream, downstream):
            missing.append(f"拓扑缺少 {upstream} -> {downstream}")
    return missing
