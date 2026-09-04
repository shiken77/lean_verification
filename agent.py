"""Two Lean-code agents: a reproducible demo agent and a DeepSeek API agent."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from contracts import FormalContract, MAXIMUM_CONTRACT


ENVIRONMENT_PROMPT = """Environment: Lean 4.33.0, core plus Init.Omega only; Mathlib is NOT installed.
Declare helper results using `theorem`, not `lemma`: the `lemma` command is unavailable in this restricted environment.
Do not assume extra syntax or tactics from Mathlib or Batteries. Use fully proved helper theorems without placeholders.
"""


GENERATION_PROMPT = """You generate small, self-contained Lean 4 programs.
Return only one ```lean fenced code block. The program must contain:
1. a function implementing the user's specification;
2. the exact required theorem shown in the locked formal contract;
3. a proof of that theorem.

Use only Lean 4 core and `import Init.Omega` when arithmetic automation is needed.
Never use sorry, admit, axiom, unsafe, partial, extern, run_cmd, #eval, or any other proof bypass.
Keep the program below 10000 characters. Auxiliary definitions and proved helper lemmas are allowed.
"""

STAGED_PROMPT = """You are implementing and proving a small Lean 4 function in checkable steps.
Return JSON only: {"status":"continue|complete|clarify", "plan":["short step"],
"current_step":"short label", "change_summary":"brief change and reason", "code":"cumulative Lean source", "question":""}.
First identify input cases and proof obligations in a short plan (not a long reasoning transcript).
Your FIRST submission must use status continue: implement the function and prove at least one useful helper lemma.
Later submissions may add other proved helpers or finish the exact locked theorem with status complete.
Every submission is a complete, standalone file, including all earlier definitions it needs.
Lean will check each submission. Do not claim the task is finished until the full locked contract passes.
Preserve verified parts where possible; if changing them is necessary, explain the change briefly.
Use only Lean 4 core and import Init.Omega. No sorry, admit, axiom, unsafe, partial, extern, run_cmd, or #eval.
No unproved placeholders even in intermediate submissions. Never define verifier_locked_contract.
Do not weaken the locked theorem, change its arguments, or silently change the user's requirement.
If the specification conflicts with the contract, return clarify with a concrete question.
Limit the Lean source to 10000 characters and the plan to 6 short steps.
"""

DIAGNOSIS_PROMPT = """Diagnose a failed Lean submission using the supplied source and exact verifier output.
Return JSON with four nonempty string fields: category, evidence, likely_cause, next_action.
Ground evidence in the actual error and code; do not invent successful checks or missing information.
Suggest a focused repair, preserving the locked theorem. If earlier repairs repeated the error, propose a different approach.
This is a repair hypothesis, not a correctness verdict. Never suggest bypassing a proof or weakening acceptance criteria.
Keep the total response below 1800 characters.
"""


@dataclass(frozen=True)
class ModelCall:
    purpose: str
    model: str
    system_prompt: str
    user_prompt: str
    response: str
    usage: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Submission:
    code: str
    status: str = "complete"
    plan: list[str] = field(default_factory=list)
    current_step: str = "Implementation and proof"
    change_summary: str = ""
    question: str = ""
    call: ModelCall | None = None
    format_error: str = ""

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

    def __init__(self, model: str | None = None, temperature: float = 0.2) -> None:
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip()
        self.temperature = temperature
        if not self.api_key:
            raise AgentError("DEEPSEEK_API_KEY was not found. Please set the environment variable in your terminal first.")

    def _request(self, system_prompt: str, task: str) -> str:
        return self._call(system_prompt, task, "formalization").response

    def _call(self, system_prompt: str, task: str, purpose: str) -> ModelCall:
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": task},
                ],
                "stream": False,
                "temperature": self.temperature,
                "max_tokens": 4096,
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
        return ModelCall(purpose, data.get("model", self.model), system_prompt, task, text, data.get("usage", {}))

    def submit(self, specification: str, contract: FormalContract, previous_code: str | None,
               feedback: str | None, strategy: str, history: list[dict], verified_fragment: str,
               plan: list[str]) -> Submission:
        task = (f"User specification:\n{specification}\n\nLocked formal contract:\n"
                f"{contract.function_signature}\n{contract.theorem_statement}\n")
        if previous_code is not None:
            task += (f"\nPrevious cumulative code:\n```lean\n{previous_code}\n```\n"
                     f"\nVerifier feedback for this same task:\n{feedback}\n"
                     "Repair this submission; preserve the exact locked declarations.\n")
        if strategy != "raw" and history:
            task += "\nRecent submission history (do not repeat unsuccessful changes):\n" + json.dumps(history[-5:], ensure_ascii=False)
        if strategy == "staged":
            task += ("\nCurrent short plan:\n" + json.dumps(plan, ensure_ascii=False)
                     + "\nLast Lean-accepted intermediate file (not final acceptance):\n" + (verified_fragment or "None yet")
                     + "\nSubmit the next checkable step. A continue result is never final PASS.")
        else:
            task += "\nGenerate the full implementation and exact theorem proof."
        system = (STAGED_PROMPT if strategy == "staged" else GENERATION_PROMPT) + "\n" + ENVIRONMENT_PROMPT
        call = self._call(system, task, "generation")
        if strategy != "staged":
            return Submission(extract_lean_code(call.response), call=call)
        try:
            data = extract_json_object(call.response)
            if data.get("status") not in {"continue", "complete", "clarify"}:
                raise ValueError("status must be continue, complete, or clarify")
            if not isinstance(data.get("plan"), list) or not all(isinstance(x, str) for x in data["plan"]):
                raise ValueError("plan must be a list of strings")
            for key in ("code", "current_step", "change_summary", "question"):
                if not isinstance(data.get(key), str):
                    raise ValueError(f"{key} must be a string")
            if data["status"] == "clarify" and not data["question"].strip():
                raise ValueError("clarify requires a concrete question")
            return Submission(data["code"], data["status"], data["plan"][:6], data["current_step"],
                              data["change_summary"], data["question"], call)
        except (AgentError, ValueError) as exc:
            return Submission("", call=call, format_error=str(exc))

    def diagnose(self, specification: str, contract: FormalContract, code: str, error: str,
                 history: list[dict]) -> tuple[str, ModelCall]:
        task = json.dumps({"requirement": specification, "locked_theorem": contract.theorem_statement,
                           "code_with_line_numbers": "\n".join(f"{i}: {line}" for i, line in enumerate(code.splitlines(), 1)),
                           "verifier_error": error, "recent_history": history[-5:]}, ensure_ascii=False)
        call = self._call(DIAGNOSIS_PROMPT + "\n" + ENVIRONMENT_PROMPT, task, "diagnosis")
        try:
            data = extract_json_object(call.response)
            fields = ("category", "evidence", "likely_cause", "next_action")
            if not all(isinstance(data.get(k), str) and data[k].strip() for k in fields):
                raise ValueError("missing diagnosis fields")
            feedback = json.dumps({k: data[k] for k in fields}, ensure_ascii=False, indent=2)
        except (AgentError, ValueError):
            feedback = "The diagnostic model returned invalid JSON; use the original Lean error below."
        return feedback, call

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
