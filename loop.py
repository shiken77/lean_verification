"""Bounded, traceable generation/diagnosis/checkpoint loops. No model-global task memory."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
import difflib
import json
from pathlib import Path
import re
import time

from agent import AgentError, ModelCall, Submission
from contracts import FormalContract
from verifier import VerificationResult, verify_against_contract, verify_lean

UNTIL_SUCCESS_SAFETY_LIMIT = 20
STRATEGIES = {"raw": "A · Raw Lean errors", "diagnostic": "B · Diagnosis + repair history",
              "staged": "C · Checkable steps + diagnosis"}


@dataclass(frozen=True)
class Attempt:
    number: int
    code: str
    verification: VerificationResult
    repair_feedback: str | None = None
    feedback_received: str | None = None
    submission_status: str = "complete"
    current_step: str = "Implementation and proof"
    plan: list[str] = field(default_factory=list)
    change_summary: str = ""
    calls: list[ModelCall] = field(default_factory=list)
    api_calls: int = 0
    elapsed_seconds: float = 0


@dataclass(frozen=True)
class LoopResult:
    passed: bool
    attempts: list[Attempt]
    stop_reason: str = "retry_limit"
    strategy: str = "diagnostic"
    elapsed_seconds: float = 0

    @property
    def api_calls(self) -> int:
        return sum(a.api_calls for a in self.attempts)

    @property
    def total_tokens(self) -> int | None:
        calls = [c for a in self.attempts for c in a.calls]
        if len(calls) != self.api_calls or any("total_tokens" not in c.usage for c in calls):
            return None
        return sum(c.usage["total_tokens"] for c in calls)

    def to_dict(self) -> dict:
        return {**asdict(self), "api_calls": self.api_calls, "total_tokens": self.total_tokens,
                "attempts_to_success": len(self.attempts) if self.passed else None}


def save_trace(result: LoopResult, specification: str, contract: FormalContract, label: str) -> Path:
    """Local experiment artifact; requests never include credentials. Ignored by git."""
    root = Path(__file__).parent / ".runs"
    root.mkdir(exist_ok=True)
    path = root / f"{time.time_ns()}-{re.sub(r'[^a-zA-Z0-9_-]', '_', label)}.json"
    path.write_text(json.dumps({"specification": specification, "contract": asdict(contract),
                               **result.to_dict()}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def describe_change(previous: str | None, current: str) -> str:
    if previous is None:
        return "Initial submission."
    if previous.strip() == current.strip():
        return "No code change from the previous submission."
    changes = list(difflib.ndiff(previous.splitlines(), current.splitlines()))
    return f"Code diff: {sum(x.startswith('+ ') for x in changes)} lines added; {sum(x.startswith('- ') for x in changes)} lines removed."


def run_verification_loop(specification: str, contract: FormalContract, agent,
                          max_attempts: int | None = 3,
                          on_attempt: Callable[[int, bool], None] | None = None,
                          use_feedback: bool = True, *, strategy: str | None = None,
                          on_event: Callable[[dict], None] | None = None) -> LoopResult:
    strategy = strategy or ("diagnostic" if use_feedback else "raw")
    if strategy not in STRATEGIES:
        raise ValueError("Unknown execution strategy")
    limit = UNTIL_SUCCESS_SAFETY_LIMIT if max_attempts is None else max_attempts
    if not isinstance(limit, int) or not 1 <= limit <= UNTIL_SUCCESS_SAFETY_LIMIT:
        raise ValueError("Attempt limit must be between 1 and 20")
    if strategy == "staged" and not hasattr(agent, "submit"):
        raise ValueError("Checkable-step mode requires a step-capable agent, not the built-in demo")
    started = time.monotonic()
    attempts: list[Attempt] = []
    previous_code = None
    feedback = None
    history: list[dict] = []
    verified_fragment = ""
    plan: list[str] = []
    repeats: dict[tuple, int] = {}
    stop = "retry_limit"

    def emit(kind: str, number: int, **fields) -> None:
        if on_event:
            on_event({"kind": kind, "attempt": number, **fields})

    for number in range(1, limit + 1):
        attempt_started = time.monotonic()
        emit("generating", number, message="Agent is preparing the next submission")
        calls: list[ModelCall] = []
        api_calls = int(hasattr(agent, "submit"))
        try:
            submission = (agent.submit(specification, contract, previous_code, feedback, strategy,
                                       history, verified_fragment, plan) if hasattr(agent, "submit")
                          else Submission(agent.generate(specification, contract, previous_code, feedback)))
            if submission.call:
                calls.append(submission.call)
            if submission.plan:
                plan = submission.plan
            emit("checking", number, message=submission.current_step, plan=plan)
            if submission.format_error:
                verification = VerificationResult(False, "Submission format error: " + submission.format_error, "format")
            elif submission.status == "clarify":
                verification = VerificationResult(False, submission.question, "clarify")
            elif strategy == "staged" and number == 1 and submission.status != "continue":
                verification = VerificationResult(False, "The first staged submission must be a checkable intermediate step, not complete.", "format")
            elif submission.status == "continue":
                if not submission.code.strip() or not re.search(r"\btheorem\s+", submission.code):
                    verification = VerificationResult(False, "An intermediate step needs cumulative code and at least one proved helper theorem.", "format")
                elif "verifier_locked_contract" in submission.code:
                    verification = VerificationResult(False, "Do not define the reserved verifier_locked_contract.", "policy")
                else:
                    verification = verify_lean(submission.code)
                    if verification.passed:
                        verified_fragment = submission.code
                        verification = VerificationResult(True, "Intermediate file accepted by Lean. The full locked contract has NOT passed yet.", "intermediate")
            else:
                verification = verify_against_contract(submission.code, contract.verification_wrapper,
                                                       signature=contract.function_signature, theorem=contract.theorem_statement)
        except AgentError as exc:
            submission = Submission("")
            verification = VerificationResult(False, str(exc), "api")

        final_pass = verification.passed and submission.status == "complete"
        normalized_error = re.sub(r"[^\s]*lean-check-[^\s:]+", "Generated.lean", verification.message)
        fingerprint = (submission.code.strip(), normalized_error, submission.status)
        repeats[fingerprint] = repeats.get(fingerprint, 0) + 1
        terminal = verification.stage in {"environment", "contract", "api", "clarify"}
        stagnant = repeats[fingerprint] >= 3 and not final_pass
        change_summary = submission.change_summary or describe_change(previous_code, submission.code)
        summary = {"attempt": number, "step": submission.current_step,
                   "change": change_summary, "stage": verification.stage,
                   "accepted": verification.passed, "error": normalized_error[:2400]}
        history.append(summary)
        repair_feedback = None
        if not final_pass and not terminal and not stagnant and number < limit:
            if verification.passed:
                repair_feedback = verification.message + " Continue the plan; submit the full exact theorem when ready."
            elif strategy == "raw":
                repair_feedback = verification.message
            elif hasattr(agent, "diagnose"):
                emit("diagnosing", number, message="Diagnosing this failure using the code, error and history")
                api_calls += 1
                try:
                    diagnosis, call = agent.diagnose(specification, contract, submission.code, verification.message, history)
                    calls.append(call)
                    repair_feedback = "Diagnostic hypothesis (not a verified fact):\n" + diagnosis
                    summary["diagnostic_hypothesis"] = diagnosis
                except AgentError as exc:
                    repair_feedback = "Diagnosis unavailable; falling back to the original error: " + str(exc)
                repair_feedback += "\nOriginal verifier output:\n" + verification.message
            else:
                repair_feedback = verification.message

        record = Attempt(number, submission.code, verification, repair_feedback, feedback,
                         submission.status, submission.current_step, list(plan), change_summary,
                         calls, api_calls, round(time.monotonic() - attempt_started, 3))
        attempts.append(record)
        emit("attempt_done", number, message=verification.message, record=record, final_pass=final_pass)
        if on_attempt:
            on_attempt(number, final_pass)
        if final_pass or terminal or stagnant:
            stop = "passed" if final_pass else (verification.stage if terminal else "stagnation")
            break
        previous_code = submission.code
        feedback = repair_feedback

    return LoopResult(stop == "passed", attempts, stop, strategy, round(time.monotonic() - started, 3))
