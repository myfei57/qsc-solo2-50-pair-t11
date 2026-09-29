"""硫化机相位状态机。

一台硫化机在一个模次里依次经历装模、合模、升温、硫化、冷却、泄压、开模、
取件几个相位。相位迁移把工艺顺序约束钉死在设备上：没有合模不允许升温，
没有升温不允许计时，没有泄压不允许开模。非法迁移直接拒绝。
"""

from __future__ import annotations

import enum

from ..errors import SequenceError


class PressPhase(str, enum.Enum):
    """硫化机相位。"""

    IDLE = "idle"
    MOLD_LOADED = "mold_loaded"
    CLAMPING = "clamping"
    HEATING = "heating"
    CURING = "curing"
    COOLING = "cooling"
    VENTING = "venting"
    OPENING = "opening"
    DONE = "done"


class PressStateMachine:
    """记录相位迁移，非法迁移直接拒绝。"""

    _ALLOWED = {
        PressPhase.IDLE: (PressPhase.MOLD_LOADED,),
        PressPhase.MOLD_LOADED: (PressPhase.CLAMPING, PressPhase.IDLE),
        PressPhase.CLAMPING: (PressPhase.HEATING, PressPhase.MOLD_LOADED),
        PressPhase.HEATING: (PressPhase.CURING, PressPhase.VENTING),
        PressPhase.CURING: (PressPhase.COOLING, PressPhase.VENTING),
        PressPhase.COOLING: (PressPhase.VENTING,),
        PressPhase.VENTING: (PressPhase.OPENING,),
        PressPhase.OPENING: (PressPhase.DONE,),
        PressPhase.DONE: (PressPhase.IDLE,),
    }

    # 顺序闸门退回合规检查点时使用的回退迁移：只沿正常迁移边反向退一格，
    # 表示"这一步的物理动作撤回"，不允许跨越多个相位。泄压（VENTING）的
    # 前一相位是硫化保温（CURING），开模则由 VENTING 正向进入 OPENING。
    _RECOVERY = {
        PressPhase.CURING: PressPhase.HEATING,
        PressPhase.HEATING: PressPhase.CLAMPING,
        PressPhase.CLAMPING: PressPhase.MOLD_LOADED,
        PressPhase.MOLD_LOADED: PressPhase.IDLE,
        PressPhase.OPENING: PressPhase.VENTING,
        PressPhase.VENTING: PressPhase.CURING,
        PressPhase.COOLING: PressPhase.CURING,
    }

    def __init__(self) -> None:
        self._phase = PressPhase.IDLE
        self._history: list[PressPhase] = [PressPhase.IDLE]

    @property
    def phase(self) -> PressPhase:
        return self._phase

    def transition(self, target: PressPhase, component: str = "press") -> PressPhase:
        if target == self._phase:
            return self._phase
        if target not in self._ALLOWED[self._phase]:
            raise SequenceError(
                f"硫化机不允许从 {self._phase.value} 迁移到 {target.value}",
                component=component,
            )
        self._phase = target
        self._history.append(target)
        return self._phase

    def rollback(self, component: str = "press") -> PressPhase:
        """沿正常迁移边反向退一格，供顺序闸门撤回越序步。"""

        target = self._RECOVERY.get(self._phase)
        if target is None:
            raise SequenceError(
                f"硫化机相位 {self._phase.value} 没有可退回的上一相位",
                component=component,
            )
        self._phase = target
        self._history.append(target)
        return self._phase

    def history(self) -> list[str]:
        return [phase.value for phase in self._history]

    def snapshot(self) -> dict:
        return {"phase": self._phase.value, "history": self.history()}

    def restore(self, snapshot: dict) -> None:
        """从落盘快照恢复相位。"""

        phase = snapshot.get("phase")
        if not isinstance(phase, str):
            raise SequenceError("硫化机快照缺少相位", component="press")
        try:
            restored = PressPhase(phase)
        except ValueError as exc:
            raise SequenceError(f"未知硫化机相位: {phase}", component="press") from exc
        self._phase = restored
        self._history = [PressPhase.IDLE, restored]
