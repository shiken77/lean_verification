"""Natural-language requirements, formal contracts, and clarity checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json


@dataclass(frozen=True)
class FormalContract:
    """A Lean acceptance contract locked after user confirmation."""

    function_signature: str
    theorem_statement: str
    explanation: str
    verification_wrapper: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, value: str) -> "FormalContract":
        data = json.loads(value)
        required = {"function_signature", "theorem_statement", "explanation", "verification_wrapper"}
        if not isinstance(data, dict) or set(data) != required:
            raise ValueError("The formal contract has an incomplete data structure.")
        if not all(isinstance(data[key], str) and data[key].strip() for key in required):
            raise ValueError("The formal contract cannot contain empty fields.")
        return cls(**data)


MAXIMUM_CONTRACT = FormalContract(
    function_signature="def maximum (a b : Int) : Int",
    theorem_statement="""theorem maximum_correct (a b : Int) :
  maximum a b ≥ a ∧
  maximum a b ≥ b ∧
  (maximum a b = a ∨ maximum a b = b)""",
    explanation="For any two integers a and b, the result is at least both inputs and equals either a or b.",
    verification_wrapper="""theorem verifier_locked_contract (a b : Int) :
    maximum a b ≥ a ∧
    maximum a b ≥ b ∧
    (maximum a b = a ∨ maximum a b = b) := by
  exact maximum_correct a b""",
)


def make_contract(function_signature: str, theorem_statement: str, explanation: str, wrapper: str) -> FormalContract:
    """Create a benchmark contract while keeping the examples readable."""
    return FormalContract(function_signature, theorem_statement, explanation, wrapper)


def assess_specification(specification: str) -> tuple[bool, str]:
    """Check that a natural-language request contains input, output, and a checkable condition."""
    text = specification.strip().lower()
    if len(text) < 20:
        return False, "The requirement is too short. Please describe the input, output, and required conditions."

    has_input = any(word in text for word in ("输入", "input", "给定", "接收"))
    has_output = any(word in text for word in ("输出", "output", "返回", "return"))
    has_condition = any(
        word in text
        for word in ("必须", "满足", "要求", "保证", "should", "must", "greater", "less", "等于", "不小于", "不大于")
    )

    missing: list[str] = []
    if not has_input:
        missing.append("what the input is")
    if not has_output:
        missing.append("what the output is")
    if not has_condition:
        missing.append("what conditions the output must satisfy")
    if missing:
        return False, "This requirement cannot yet be formalized. Please specify " + ", ".join(missing) + "."
    return True, "The requirement contains an input, an output, and a checkable condition."
