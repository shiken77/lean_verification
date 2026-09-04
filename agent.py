"""Two Lean-code agents: a reproducible demo agent and a DeepSeek API agent."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from contracts import FormalContract, MAXIMUM_CONTRACT


GENERATION_PROMPT = """You generate small, self-contained Lean 4 programs.
Return only one ```lean fenced code block. The program must contain:
1. a function implementing the user's specification;
2. the exact required theorem shown in the locked formal contract;
3. a proof of that theorem.

Use only Lean 4 core and `import Init.Omega` when arithmetic automation is needed.
Never use sorry, admit, axiom, unsafe, partial, extern, run_cmd, #eval, or any other proof bypass.
Keep the program below 3000 characters.
"""

FORMALIZATION_PROMPT = """Translate a concrete natural-language function requirement into a small Lean 4 contract.
Return JSON only, with exactly these string fields:
function_signature: a Lean def signature without :=
theorem_statement: a theorem declaration without := by
explanation: a plain-language explanation of the theorem
verification_wrapper: a complete theorem named verifier_locked_contract that proves the same proposition by applying the generated correctness theorem

Use Int or Nat and only concepts available from Lean core plus Init.Omega.
The correctness theorem name must be solution_correct, and verification_wrapper must apply solution_correct.
Do not include implementation code or a proof of solution_correct.
"""


class AgentError(RuntimeError):
    """Agent 无法生成结果。"""


def extract_lean_code(text: str) -> str:
    """从模型回复中取出 Lean 代码块。"""
    match = re.search(r"```(?:lean)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    code = match.group(1) if match else text
    return code.strip()


def extract_json_object(text: str) -> dict[str, str]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AgentError(f"The agent did not return valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentError("The agent's formal contract was not a JSON object.")
    return data


class DemoAgent:
    """Fails deliberately on the first round and fixes the code on the second round."""

    def formalize(self, specification: str) -> FormalContract:
        text = specification.lower()
        if not any(word in text for word in ("maximum", "最大", "较大")):
            raise AgentError("The built-in demo currently supports only the maximum function. Choose DeepSeek API for other requirements.")
        return MAXIMUM_CONTRACT

    def generate(
        self,
        specification: str,
        contract: FormalContract,
        previous_code: str | None,
        error: str | None,
    ) -> str:
        del specification, contract
        if previous_code is None:
            return """import Init.Omega

def maximum (a b : Int) : Int := a

theorem maximum_correct (a b : Int) :
    maximum a b ≥ a ∧
    maximum a b ≥ b ∧
    (maximum a b = a ∨ maximum a b = b) := by
  unfold maximum
  omega
"""

        del error
        return """import Init.Omega

def maximum (a b : Int) : Int :=
  if a ≥ b then a else b

theorem maximum_correct (a b : Int) :
    maximum a b ≥ a ∧
    maximum a b ≥ b ∧
    (maximum a b = a ∨ maximum a b = b) := by
  unfold maximum
  split
  · rename_i h
    constructor
    · omega
    constructor
    · exact h
    · exact Or.inl rfl
  · rename_i h
    have hba : b ≥ a := by omega
    constructor
    · exact hba
    constructor
    · omega
    · exact Or.inr rfl
"""


class DeepSeekAgent:
    """通过 DeepSeek 的 Chat Completions API（OpenAI 兼容）生成或修复 Lean 代码。"""

    def __init__(self, model: str | None = None) -> None:
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip()
        if not self.api_key:
            raise AgentError("DEEPSEEK_API_KEY was not found. Please set the environment variable in your terminal first.")

    def _request(self, system_prompt: str, task: str) -> str:
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": task},
                ],
                "stream": False,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise AgentError(f"DeepSeek API returned HTTP {exc.code}: {body[:1000]}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AgentError(f"Could not connect to the DeepSeek API: {exc}") from exc

        choices = data.get("choices", [])
        text = choices[0].get("message", {}).get("content", "") if choices else ""
        if not text.strip():
            raise AgentError("The model did not return any readable text.")
        return text

    def formalize(self, specification: str) -> FormalContract:
        data = extract_json_object(self._request(FORMALIZATION_PROMPT, f"User requirement:\n{specification}"))
        try:
            return FormalContract(
                function_signature=data["function_signature"],
                theorem_statement=data["theorem_statement"],
                explanation=data["explanation"],
                verification_wrapper=data["verification_wrapper"],
            )
        except (KeyError, TypeError) as exc:
            raise AgentError("The agent's formal contract is missing required fields.") from exc

    def generate(
        self,
        specification: str,
        contract: FormalContract,
        previous_code: str | None,
        error: str | None,
    ) -> str:
        locked_contract = (
            f"Function signature:\n{contract.function_signature}\n\n"
            f"Required theorem (use this exact statement):\n{contract.theorem_statement}"
        )
        if previous_code is None:
            task = (
                f"User specification:\n{specification}\n\n"
                f"Locked formal contract:\n{locked_contract}\n\n"
                "Generate the implementation and proof."
            )
        else:
            task = (
                f"User specification:\n{specification}\n\n"
                f"Locked formal contract:\n{locked_contract}\n\n"
                f"The previous Lean program failed:\n```lean\n{previous_code}\n```\n\n"
                f"Repair context and Lean or policy error:\n{error}\n\n"
                "This is the same task, not a new task. Repair the previous program while preserving the exact locked theorem and all verifier policies."
            )

        return extract_lean_code(self._request(GENERATION_PROMPT, task))
