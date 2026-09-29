"""顺序闸门：动作前拦截而不是动作后记账。

旧实现把工艺顺序约束放在运行结束后的 ``verify_order`` 自查里：走错了机器
照样往下走，事后只在报告里多一条字符串，现场不知道怎么退。闸门把同一套
显式规则 (先, 后, 说明) 三元组挪到**每一步动作之前**：

* :meth:`OrderGuard.authorize` 在动作执行前调用。前置步缺失或有越序步已经
  落地时，直接抛 :class:`SequenceBlocked`，这一步的物理动作不会被执行；
* 异常里带 :class:`RecoveryPlan`——先按逆序撤回哪些已执行步（每步绑定一个
  物理撤销原语），再补齐哪些缺失步，然后重试被拦的这一步。退回的终点一定
  是一个规则全部满足的合规检查点；
* 规则同时给出工艺步骤的规范全序（按规则声明顺序做稳定拓扑排序），报告里
  依然保留 :func:`verify_order` 做事后复核，闸门是它的动作期版本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..errors import SequenceError

OrderRule = tuple[str, str, str]
"""一条顺序规则：(必须先执行的步, 必须后执行的步, 违规说明)。"""

UndoAction = Callable[[], None]
"""一步的物理撤销原语：把这一步对设备造成的改动退回到执行前。"""


@dataclass(frozen=True)
class RecoveryStep:
    """退回合规状态路径上的一个动作。"""

    step: str
    action: str
    description: str

    def as_dict(self) -> dict:
        return {"step": self.step, "action": self.action, "description": self.description}


@dataclass(frozen=True)
class RecoveryPlan:
    """从被拦现场退回到合规检查点的一条完整路径。

    ``rollback`` 必须按列表顺序执行（越序步的逆序撤回），执行完即到达合规
    检查点；随后按 ``complete_missing`` 补齐缺失步，再重试被拦的步骤。
    """

    blocked_step: str
    mode: str
    violations: tuple[str, ...]
    rollback: tuple[RecoveryStep, ...]
    complete_missing: tuple[RecoveryStep, ...]
    checkpoint: tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "blocked_step": self.blocked_step,
            "mode": self.mode,
            "violations": list(self.violations),
            "rollback": [item.as_dict() for item in self.rollback],
            "complete_missing": [item.as_dict() for item in self.complete_missing],
            "checkpoint": list(self.checkpoint),
            "retry": self.blocked_step,
        }


class SequenceBlocked(SequenceError):
    """工艺顺序不满足，动作在执行前被闸门拦截。"""

    def __init__(
        self,
        message: str,
        *,
        component: str = "sequence",
        plan: RecoveryPlan | None = None,
    ) -> None:
        super().__init__(message, component=component)
        self.plan = plan or RecoveryPlan(
            blocked_step="",
            mode="unknown",
            violations=(),
            rollback=(),
            complete_missing=(),
            checkpoint=(),
        )

    def as_record(self) -> dict:
        record = super().as_record()
        record["recovery"] = self.plan.as_dict()
        return record


def canonical_order(rules: tuple[OrderRule, ...]) -> list[str]:
    """按规则做稳定拓扑排序，返回工艺步骤的规范全序。

    同等可放的步骤按它第一次在规则里出现的先后排列，因此同一组规则永远得到
    同一条规范顺序，不依赖字典序或集合迭代顺序。
    """

    predecessors: dict[str, set[str]] = {}
    for earlier, later, _description in rules:
        predecessors.setdefault(earlier, set())
        predecessors.setdefault(later, set()).add(earlier)

    first_seen: dict[str, int] = {}
    for index, (earlier, later, _description) in enumerate(rules):
        first_seen.setdefault(earlier, index * 2)
        first_seen.setdefault(later, index * 2 + 1)

    resolved: list[str] = []
    remaining = dict(predecessors)
    while remaining:
        ready = sorted(
            (name for name, deps in remaining.items() if not deps),
            key=lambda name: first_seen[name],
        )
        if not ready:
            cycle = sorted(remaining)
            raise SequenceError(f"顺序规则存在环，无法排出规范顺序: {cycle}", component="sequence")
        current = ready[0]
        resolved.append(current)
        del remaining[current]
        for deps in remaining.values():
            deps.discard(current)
    return resolved


@dataclass
class OrderGuard:
    """单条工艺顺序的动作前闸门。

    绑定一组顺序规则和一张"步骤 → 物理撤销原语"表。执行器每做一步先调
    :meth:`authorize`，做完再调 :meth:`mark_done`；:meth:`rollback_to_compliant`
    按异常携带的方案真的把设备退回合规检查点。
    """

    name: str
    rules: tuple[OrderRule, ...]
    undo: dict[str, UndoAction] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    _executed: list[str] = field(default_factory=list)
    _undo_stack: dict[str, UndoAction] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._order = canonical_order(self.rules)
        self._predecessors = self._transitive_predecessors()

    def reset(self) -> None:
        """清空本次执行的步骤记录（不触碰设备）。"""

        self._executed.clear()
        self._undo_stack.clear()

    @property
    def order(self) -> list[str]:
        return list(self._order)

    def executed(self) -> list[str]:
        return list(self._executed)

    def _transitive_predecessors(self) -> dict[str, set[str]]:
        direct: dict[str, set[str]] = {name: set() for name in self._order}
        for earlier, later, _description in self.rules:
            direct[later].add(earlier)
        closure: dict[str, set[str]] = {}

        def predecessors_of(name: str) -> set[str]:
            if name in closure:
                return closure[name]
            result: set[str] = set()
            for direct_up in direct[name]:
                result.add(direct_up)
                result.update(predecessors_of(direct_up))
            closure[name] = result
            return result

        for name in self._order:
            predecessors_of(name)
        return closure

    def missing_before(self, step: str) -> list[str]:
        """执行 ``step`` 前仍缺失的全部（含传递）前置步，按规范顺序。"""

        if step not in self._predecessors:
            raise SequenceError(f"闸门不认识工艺步骤: {step}", component="sequence")
        done = set(self._executed)
        return [name for name in self._order if name in self._predecessors[step] and name not in done]

    def overreached(self, step: str) -> list[str]:
        """已经落地、但规范顺序里必须在 ``step`` 之后的步骤（即越序步）。"""

        done = set(self._executed)
        successors = {
            name
            for name, deps in self._predecessors.items()
            if step in deps
        }
        return [name for name in self._order if name in done and name in successors]

    def authorize(self, step: str) -> None:
        """动作前授权：不满足顺序立即拦截，物理动作不会发生。"""

        if step in self._executed:
            plan = RecoveryPlan(
                blocked_step=step,
                mode="duplicate",
                violations=(f"步骤 {step} 已经执行过，禁止重复执行",),
                rollback=(),
                complete_missing=(),
                checkpoint=tuple(self._executed),
            )
            raise SequenceBlocked(
                f"[{self.name}] 步骤 {step} 重复执行被拦截", plan=plan
            )

        missing = self.missing_before(step)
        overreached = self.overreached(step)
        if not missing and not overreached:
            return

        violations = tuple(self._violation_messages(step, missing, overreached))
        rollback = tuple(
            RecoveryStep(
                step=name,
                action=f"undo_{name}",
                description=self._undo_description(name),
            )
            for name in reversed(overreached)
        )
        checkpoint = tuple(name for name in self._executed if name not in overreached)
        complete_missing = tuple(
            RecoveryStep(
                step=name,
                action=f"perform_{name}",
                description=self._perform_description(name),
            )
            for name in missing
        )
        mode = "rollback_and_redo" if overreached else "complete_prerequisites"
        plan = RecoveryPlan(
            blocked_step=step,
            mode=mode,
            violations=violations,
            rollback=rollback,
            complete_missing=complete_missing,
            checkpoint=checkpoint,
        )
        head = violations[0] if violations else f"{step} 的前置条件不满足"
        raise SequenceBlocked(f"[{self.name}] {head}，动作已被拦截", plan=plan)

    def mark_done(self, step: str, undo: UndoAction | None = None) -> None:
        """登记一步已经成功落地，并记住它的物理撤销原语。"""

        if step not in self._predecessors:
            raise SequenceError(f"闸门不认识工艺步骤: {step}", component="sequence")
        if step not in self._executed:
            self._executed.append(step)
            if undo is not None:
                self._undo_stack[step] = undo

    def rollback_to_compliant(self, plan: RecoveryPlan) -> list[str]:
        """按退回方案逆序撤回越序步，返回实际撤回的步骤名。

        撤回动作只执行在本闸门登记过撤销原语的步；撤完后现场即处于
        ``plan.checkpoint`` 这个合规检查点，缺失的前置步由调用方补齐后重试。
        """

        rolled: list[str] = []
        for item in plan.rollback:
            if item.step in self._executed:
                undo = self._undo_stack.get(item.step) or self.undo.get(item.step)
                if undo is None:
                    raise SequenceError(
                        f"步骤 {item.step} 没有登记物理撤销原语，无法退回合规状态",
                        component="sequence",
                    )
                undo()
                self._executed.remove(item.step)
                self._undo_stack.pop(item.step, None)
                rolled.append(item.step)
        if self.executed() != list(plan.checkpoint):
            raise SequenceError(
                f"退回后现场 {self.executed()} 与合规检查点 {list(plan.checkpoint)} 不一致",
                component="sequence",
            )
        return rolled

    def _violation_messages(self, step: str, missing: list[str], overreached: list[str]) -> list[str]:
        messages: list[str] = []
        for earlier, later, description in self.rules:
            if later != step:
                continue
            if earlier in missing:
                messages.append(f"{description}：缺少前置步 {earlier}")
        for name in overreached:
            description = self._rule_description(step, name)
            messages.append(f"{description}：{name} 已越序先行，须先撤回")
        return messages

    def _rule_description(self, earlier: str, later: str) -> str:
        for up, down, description in self.rules:
            if up == earlier and down == later:
                return description
        return f"{earlier} 必须先于 {later}"

    def _undo_description(self, step: str) -> str:
        custom = self.descriptions.get(f"undo_{step}")
        return custom or f"撤回已执行的 {step}，把设备恢复到该步之前的状态"

    def _perform_description(self, step: str) -> str:
        custom = self.descriptions.get(f"perform_{step}")
        return custom or f"补做缺失的前置步 {step}"

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "canonical_order": list(self._order),
            "executed": list(self._executed),
            "compliant": not self._open_violations(),
        }

    def _open_violations(self) -> list[str]:
        done = set(self._executed)
        violations: list[str] = []
        for earlier, later, _description in self.rules:
            if later in done and earlier not in done:
                violations.append(later)
        return violations
