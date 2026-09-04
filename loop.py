"""生成 → Lean 检查 → 错误反馈 → 修复的核心循环。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from collections.abc import Callable

from contracts import FormalContract
from verifier import VerificationResult, diagnose_verification_failure, verify_against_contract


# "Until success" needs a practical guard: a model can remain stuck on the
# same proof obligation forever, which would otherwise spend API tokens forever.
UNTIL_SUCCESS_SAFETY_LIMIT = 20


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
    repair_feedback: str | None = None


@dataclass(frozen=True)
class LoopResult:
    passed: bool
    attempts: list[Attempt]


def run_verification_loop(
    specification: str,
    contract: FormalContract,
    agent: LeanAgent,
    max_attempts: int | None = 3,
    on_attempt: Callable[[int, bool], None] | None = None,
    use_feedback: bool = True,
) -> LoopResult:
    previous_code: str | None = None
    error: str | None = None
    attempts: list[Attempt] = []

    number = 0
    attempt_limit = UNTIL_SUCCESS_SAFETY_LIMIT if max_attempts is None else max_attempts
    while number < attempt_limit:
        number += 1
        code = agent.generate(specification, contract, previous_code, error)
        verification = verify_against_contract(code, contract.verification_wrapper)
        repair_feedback = None
        if not verification.passed:
            repair_feedback = (
                f"This is repair attempt {number + 1} for the same locked task; do not start a new task.\n"
                f"Diagnosis: {diagnose_verification_failure(verification.message)}\n"
                f"Required action: preserve the exact locked theorem and repair the previous program.\n"
                f"Lean error: {verification.message}"
            )
        attempts.append(Attempt(number, code, verification, repair_feedback))
        if on_attempt is not None:
            on_attempt(number, verification.passed)
        if verification.passed:
            return LoopResult(True, attempts)
        previous_code = code
        error = repair_feedback if use_feedback else verification.message

    return LoopResult(False, attempts)
