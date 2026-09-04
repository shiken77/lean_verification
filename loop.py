"""生成 → Lean 检查 → 错误反馈 → 修复的核心循环。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from collections.abc import Callable

from contracts import FormalContract
from verifier import VerificationResult, verify_against_contract


class LeanAgent(Protocol):
    def generate(
        self,
        specification: str,
        contract: FormalContract,
        previous_code: str | None,
        error: str | None,
    ) -> str: ...


@dataclass(frozen=True)
class Attempt:
    number: int
    code: str
    verification: VerificationResult


@dataclass(frozen=True)
class LoopResult:
    passed: bool
    attempts: list[Attempt]


def run_verification_loop(
    specification: str,
    contract: FormalContract,
    agent: LeanAgent,
    max_attempts: int = 3,
    on_attempt: Callable[[int, bool], None] | None = None,
) -> LoopResult:
    previous_code: str | None = None
    error: str | None = None
    attempts: list[Attempt] = []

    for number in range(1, max_attempts + 1):
        code = agent.generate(specification, contract, previous_code, error)
        verification = verify_against_contract(code, contract.verification_wrapper)
        attempts.append(Attempt(number, code, verification))
        if on_attempt is not None:
            on_attempt(number, verification.passed)
        if verification.passed:
            return LoopResult(True, attempts)
        previous_code = code
        error = verification.message

    return LoopResult(False, attempts)
