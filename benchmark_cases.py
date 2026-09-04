"""Fifteen fixed cases: valid tasks, faulty implementations, proof bypasses, and ambiguous requirements."""

from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
import re

from contracts import FormalContract, MAXIMUM_CONTRACT, assess_specification, make_contract
from loop import LoopResult, run_verification_loop, save_trace
from verifier import verify_against_contract, verify_lean


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    group: str
    description: str
    expected: str
    source: str = ""
    wrapper: str = ""
    specification: str = ""
    contract: FormalContract | None = None
    clarification_reason: str = ""


IMPORT = "import Init.Omega\n\n"

POSITIVE_CASES = [
    BenchmarkCase(
        "positive_add_one", "Valid task", "Adding one to a natural number makes it strictly larger", "PASS",
        IMPORT + """def addOne (n : Nat) : Nat := n + 1

theorem addOne_correct (n : Nat) : addOne n > n := by
  unfold addOne
  omega
""",
        """theorem verifier_locked_contract (n : Nat) : addOne n > n := by
  exact addOne_correct n""",
        "Input: a natural number n. Output: n plus one. The result must be greater than n.",
        make_contract(
            "def addOne (n : Nat) : Nat",
            "theorem solution_correct (n : Nat) : addOne n > n",
            "For every natural number n, addOne returns a value strictly greater than n.",
            """theorem verifier_locked_contract (n : Nat) : addOne n > n := by
  exact solution_correct n""",
        ),
    ),
    BenchmarkCase(
        "positive_maximum", "Valid task", "Maximum of two integers", "PASS",
        IMPORT + """def maximum (a b : Int) : Int := if a ≥ b then a else b

theorem maximum_correct (a b : Int) :
    maximum a b ≥ a ∧ maximum a b ≥ b ∧
    (maximum a b = a ∨ maximum a b = b) := by
  unfold maximum
  split <;> omega
""",
        """theorem verifier_locked_contract (a b : Int) :
    maximum a b ≥ a ∧ maximum a b ≥ b ∧
    (maximum a b = a ∨ maximum a b = b) := by
  exact maximum_correct a b""",
        "Input: two integers a and b. Output: the larger integer. The result must be at least both inputs and equal one of them.",
        MAXIMUM_CONTRACT,
    ),
    BenchmarkCase(
        "positive_minimum", "Valid task", "Minimum of two integers", "PASS",
        IMPORT + """def minimum (a b : Int) : Int := if a ≤ b then a else b

theorem minimum_correct (a b : Int) :
    minimum a b ≤ a ∧ minimum a b ≤ b ∧
    (minimum a b = a ∨ minimum a b = b) := by
  unfold minimum
  split <;> omega
""",
        """theorem verifier_locked_contract (a b : Int) :
    minimum a b ≤ a ∧ minimum a b ≤ b ∧
    (minimum a b = a ∨ minimum a b = b) := by
  exact minimum_correct a b""",
        "Input: two integers a and b. Output: the smaller integer. The result must be at most both inputs and equal one of them.",
        make_contract(
            "def minimum (a b : Int) : Int",
            "theorem solution_correct (a b : Int) : minimum a b ≤ a ∧ minimum a b ≤ b ∧ (minimum a b = a ∨ minimum a b = b)",
            "For any two integers, minimum returns the smaller input.",
            """theorem verifier_locked_contract (a b : Int) :
    minimum a b ≤ a ∧ minimum a b ≤ b ∧
    (minimum a b = a ∨ minimum a b = b) := by
  exact solution_correct a b""",
        ),
    ),
    BenchmarkCase(
        "positive_absolute", "Valid task", "An integer absolute value is non-negative", "PASS",
        IMPORT + """def absolute (x : Int) : Int := if x ≥ 0 then x else -x

theorem absolute_correct (x : Int) :
    absolute x ≥ 0 ∧ (absolute x = x ∨ absolute x = -x) := by
  unfold absolute
  split <;> omega
""",
        """theorem verifier_locked_contract (x : Int) :
    absolute x ≥ 0 ∧ (absolute x = x ∨ absolute x = -x) := by
  exact absolute_correct x""",
        "Input: an integer x. Output: its absolute value. The result must be non-negative and equal x or -x.",
        make_contract(
            "def absolute (x : Int) : Int",
            "theorem solution_correct (x : Int) : absolute x ≥ 0 ∧ (absolute x = x ∨ absolute x = -x)",
            "For every integer, absolute returns a non-negative value equal to the input or its negation.",
            """theorem verifier_locked_contract (x : Int) :
    absolute x ≥ 0 ∧ (absolute x = x ∨ absolute x = -x) := by
  exact solution_correct x""",
        ),
    ),
    BenchmarkCase(
        "positive_clamp", "Valid task", "A result is bounded by lower and upper limits", "PASS",
        IMPORT + """def clamp (lo hi x : Int) : Int :=
  if x < lo then lo else if x > hi then hi else x

theorem clamp_correct (lo hi x : Int) (h : lo ≤ hi) :
    lo ≤ clamp lo hi x ∧ clamp lo hi x ≤ hi := by
  unfold clamp
  split
  · omega
  · split <;> omega
""",
        """theorem verifier_locked_contract (lo hi x : Int) (h : lo ≤ hi) :
    lo ≤ clamp lo hi x ∧ clamp lo hi x ≤ hi := by
  exact clamp_correct lo hi x h""",
        "Input: a lower bound lo, upper bound hi, and integer x. Output: x clamped to the interval. Assume lo ≤ hi; the result must lie between the bounds.",
        make_contract(
            "def clamp (lo hi x : Int) : Int",
            "theorem solution_correct (lo hi x : Int) (h : lo ≤ hi) : lo ≤ clamp lo hi x ∧ clamp lo hi x ≤ hi",
            "When lo is no greater than hi, clamp returns a value inside the closed interval [lo, hi].",
            """theorem verifier_locked_contract (lo hi x : Int) (h : lo ≤ hi) :
    lo ≤ clamp lo hi x ∧ clamp lo hi x ≤ hi := by
  exact solution_correct lo hi x h""",
        ),
    ),
]

FAULTY_CASES = [
    BenchmarkCase(
        "faulty_add_one", "Faulty implementation", "addOne incorrectly returns the input", "FAIL",
        IMPORT + """def addOne (n : Nat) : Nat := n
theorem addOne_correct (n : Nat) : addOne n > n := by unfold addOne; omega
""",
        POSITIVE_CASES[0].wrapper,
    ),
    BenchmarkCase(
        "faulty_maximum", "Faulty implementation", "maximum always returns a", "FAIL",
        IMPORT + """def maximum (a b : Int) : Int := a
theorem maximum_correct (a b : Int) :
    maximum a b ≥ a ∧ maximum a b ≥ b ∧
    (maximum a b = a ∨ maximum a b = b) := by unfold maximum; omega
""",
        POSITIVE_CASES[1].wrapper,
    ),
    BenchmarkCase(
        "faulty_minimum", "Faulty implementation", "minimum always returns a", "FAIL",
        IMPORT + """def minimum (a b : Int) : Int := a
theorem minimum_correct (a b : Int) :
    minimum a b ≤ a ∧ minimum a b ≤ b ∧
    (minimum a b = a ∨ minimum a b = b) := by unfold minimum; omega
""",
        POSITIVE_CASES[2].wrapper,
    ),
    BenchmarkCase(
        "faulty_absolute", "Faulty implementation", "absolute returns negative inputs unchanged", "FAIL",
        IMPORT + """def absolute (x : Int) : Int := x
theorem absolute_correct (x : Int) :
    absolute x ≥ 0 ∧ (absolute x = x ∨ absolute x = -x) := by unfold absolute; omega
""",
        POSITIVE_CASES[3].wrapper,
    ),
    BenchmarkCase(
        "faulty_clamp", "Faulty implementation", "clamp does not handle out-of-range values", "FAIL",
        IMPORT + """def clamp (lo hi x : Int) : Int := x
theorem clamp_correct (lo hi x : Int) (h : lo ≤ hi) :
    lo ≤ clamp lo hi x ∧ clamp lo hi x ≤ hi := by unfold clamp; omega
""",
        POSITIVE_CASES[4].wrapper,
    ),
]

CHEATING_CASES = [
    BenchmarkCase("cheat_sorry", "Proof bypass", "Uses sorry to skip the proof", "REJECT", "theorem fake : False := by sorry"),
    BenchmarkCase("cheat_admit", "Proof bypass", "Uses admit to skip the proof", "REJECT", "theorem fake : False := by admit"),
    BenchmarkCase("cheat_axiom", "Proof bypass", "Declares an unproven axiom", "REJECT", "axiom fake : False\ntheorem result : False := fake"),
]

AMBIGUOUS_CASES = [
    BenchmarkCase(
        "ambiguous_best_sort", "Ambiguous requirement", "No input, output, or checkable acceptance criteria", "CLARIFY",
        specification="Build the best sorting function",
    ),
    BenchmarkCase(
        "ambiguous_clamp_behavior", "Ambiguous requirement", "The input and output are clear, but the acceptance condition does not specify all clamp branches", "CLARIFY",
        specification="Input: lower bound lo, upper bound hi, and integer x. Output: an integer result. The result must stay between lo and hi.",
        clarification_reason="The requirement is not complete enough to characterize clamp behavior. Please confirm what must happen when x < lo, lo ≤ x ≤ hi, and x > hi.",
    ),
]

ALL_CASES = POSITIVE_CASES + FAULTY_CASES + CHEATING_CASES + AMBIGUOUS_CASES


@dataclass(frozen=True)
class BenchmarkResult:
    case: BenchmarkCase
    passed: bool
    observed: str
    detail: str
    attempts: int = 1
    execution_source: str = "Fixed case + Lean"
    loop_result: LoopResult | None = None
    trace_path: str = ""


def reference_for_contract(case: BenchmarkCase) -> str:
    """Adapt only the reference theorem name, not its mathematical proposition."""
    if case.contract is None:
        raise ValueError("This case has no model contract")
    old = re.search(r"theorem\s+(\w+)", case.source).group(1)
    new = re.search(r"theorem\s+(\w+)", case.contract.theorem_statement).group(1)
    return re.sub(r"\b" + old + r"\b", new, case.source)


def preflight_case(case: BenchmarkCase) -> None:
    contract = case.contract
    result = verify_against_contract(reference_for_contract(case), contract.verification_wrapper,
                                     signature=contract.function_signature, theorem=contract.theorem_statement)
    if not result.passed:
        raise ValueError(f"Benchmark preflight failed for {case.id}; no model should repair this fixture.\n{result.message}")


def preflight_benchmark() -> None:
    for case in POSITIVE_CASES:
        preflight_case(case)


def run_case(case: BenchmarkCase, agent=None, agent_label: str = "Selected agent", on_attempt: Callable[[str, int, bool], None] | None = None, max_attempts: int | None = 3, use_feedback: bool = True, *, strategy: str | None = None, on_event=None) -> BenchmarkResult:
    if case.expected == "PASS" and agent is not None and case.contract is not None:
        preflight_case(case)
        loop_result = run_verification_loop(
            case.specification,
            case.contract,
            agent,
            max_attempts=max_attempts,
            on_attempt=lambda number, passed: on_attempt(case.id, number, passed) if on_attempt else None,
            use_feedback=use_feedback,
            strategy=strategy,
            on_event=(lambda event: on_event(case.id, event)) if on_event else None,
        )
        observed = "PASS" if loop_result.passed else ("BLOCKED" if loop_result.stop_reason in {"contract", "environment", "api"} else "FAIL")
        detail = loop_result.attempts[-1].verification.message if loop_result.attempts else "The agent produced no attempt."
        trace = save_trace(loop_result, case.specification, case.contract, case.id + "-" + loop_result.strategy)
        return BenchmarkResult(
            case,
            loop_result.passed,
            observed,
            detail,
            len(loop_result.attempts),
            f"{agent_label} + Lean",
            loop_result,
            str(trace),
        )

    if case.expected == "CLARIFY":
        if case.clarification_reason:
            return BenchmarkResult(case, True, "CLARIFY", case.clarification_reason, 1, "Fixed clarification fixture (not model-generated)")
        clear, detail = assess_specification(case.specification)
        observed = "FORMALIZE" if clear else "CLARIFY"
        return BenchmarkResult(case, observed == case.expected, observed, detail, 1, "Fixed case + clarity checker")

    if case.expected == "REJECT":
        result = verify_lean(case.source)
        observed = "PASS" if result.passed else "REJECT"
        return BenchmarkResult(case, observed == case.expected, observed, result.message, 1, "Fixed case + Lean")

    result = verify_against_contract(case.source, case.wrapper)
    observed = "PASS" if result.passed else "FAIL"
    return BenchmarkResult(case, observed == case.expected, observed, result.message, 1, "Fixed case + Lean")


def run_all_cases(agent=None, agent_label: str = "Selected agent", on_attempt: Callable[[str, int, bool], None] | None = None, max_attempts: int | None = 3, use_feedback: bool = True, *, strategy: str | None = None, on_event=None, on_case_done=None) -> list[BenchmarkResult]:
    if agent is None:
        return [run_case(case) for case in ALL_CASES]
    # All model-path wrappers must accept references BEFORE any paid request.
    preflight_benchmark()

    # The five model-generated cases are independent. Run them concurrently so
    # the page does not wait for five full API conversations one after another.
    model_cases = [case for case in ALL_CASES if case.expected == "PASS"]
    fixed_cases = [case for case in ALL_CASES if case.expected != "PASS"]
    def execute(case):
        result = run_case(case, agent, agent_label, on_attempt, max_attempts, use_feedback,
                          strategy=strategy, on_event=on_event)
        if on_case_done:
            on_case_done(result)
        return result
    with ThreadPoolExecutor(max_workers=len(model_cases)) as executor:
        model_results = list(executor.map(execute, model_cases))
    fixed_results = []
    for case in fixed_cases:
        result = run_case(case)
        if on_attempt:
            on_attempt(case.id, 1, result.passed)
        if on_case_done:
            on_case_done(result)
        fixed_results.append(result)
    return model_results + fixed_results
