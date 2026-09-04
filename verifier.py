"""调用 Lean，并把结果转换成 Agent 能使用的 PASS/FAIL。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


PROJECT_DIR = Path(__file__).resolve().parent
MAX_SOURCE_LENGTH = 10_000
ALLOWED_IMPORTS = {"Init.Omega"}
FORBIDDEN_PATTERNS = {
    r"\bsorry\b": "sorry is not allowed to skip a proof",
    r"\badmit\b": "admit is not allowed to skip a proof",
    r"\baxiom\b": "adding unproven axioms is not allowed",
    r"\bunsafe\b": "unsafe declarations are not allowed in this prototype",
    r"\bpartial\b": "partial declarations are not allowed in this prototype",
    r"\bextern\b": "external functions are not allowed in this prototype",
    r"\brun_cmd\b": "run_cmd is not allowed in this prototype",
    r"#eval\b": "#eval execution is not allowed in this prototype",
}


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    message: str
    stage: str = "candidate"


def diagnose_verification_failure(message: str) -> str:
    """把 Lean 的原始错误归类成可执行的修复建议。"""
    text = message.lower()
    if "policy check failed" in text or any(word in text for word in ("sorry", "admit", "axiom", "unsafe")):
        return "The submission used a forbidden proof bypass or unsafe construct. Remove it and provide a real proof."
    if "timeout" in text or "exceeded" in text:
        return "The proof did not finish within the verifier timeout. Simplify the implementation or use a more direct proof."
    if "unknown identifier" in text or "unknown constant" in text or "invalid" in text:
        return "The Lean program has a syntax, name, or type-structure problem. Check declarations and types before repairing the proof."
    if "unsolved goals" in text or "tactic failed" in text or "unsolved" in text:
        return "The implementation or proof leaves the locked theorem unproven. Inspect the remaining goal and add the missing reasoning."
    return "Lean rejected the candidate program. Compare it with the locked theorem and repair the implementation or proof without weakening the contract."


def check_source_policy(source: str) -> str | None:
    """在启动 Lean 前进行一个很小的安全和诚信检查。"""
    if len(source) > MAX_SOURCE_LENGTH:
        return f"Lean source is too long: {len(source)} > {MAX_SOURCE_LENGTH}"

    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("import "):
            module = stripped.removeprefix("import ").strip()
            if module not in ALLOWED_IMPORTS:
                return f"import of {module} is not allowed; only {', '.join(sorted(ALLOWED_IMPORTS))} is currently permitted"

    for pattern, message in FORBIDDEN_PATTERNS.items():
        if re.search(pattern, source, flags=re.IGNORECASE):
            return message
    return None


def find_lean() -> tuple[str | None, dict[str, str]]:
    """优先使用项目内 Lean，其次使用系统 PATH 中的 Lean。"""
    # Reduce accidental credential exposure. This is NOT process isolation:
    # Lean still shares the app's OS user and must only receive trusted inputs.
    allowed = {"PATH", "HOME", "ELAN_HOME", "LEAN_PATH", "LEAN_SYSROOT", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    configured = os.environ.get("LEAN_CMD", "").strip()
    if configured:
        return configured, env

    local_lean = PROJECT_DIR / ".elan" / "bin" / "lean"
    if local_lean.exists():
        env["ELAN_HOME"] = str(PROJECT_DIR / ".elan")
        return str(local_lean), env

    return shutil.which("lean"), env


def verify_lean(source: str, timeout_seconds: int = 20) -> VerificationResult:
    policy_error = check_source_policy(source)
    if policy_error:
        return VerificationResult(False, f"Source policy check failed: {policy_error}", "policy")

    lean, env = find_lean()
    if not lean:
        return VerificationResult(False, "Lean was not found. Please run ./install_lean.sh first.", "environment")

    temp_root = PROJECT_DIR / ".tmp"
    temp_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lean-check-", dir=temp_root) as directory:
        source_file = Path(directory) / "Generated.lean"
        source_file.write_text(source, encoding="utf-8")
        try:
            completed = subprocess.run(
                [lean, str(source_file)],
                cwd=PROJECT_DIR,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return VerificationResult(False, f"Lean check exceeded {timeout_seconds}s and was stopped.", "timeout")
        except OSError as exc:
            return VerificationResult(False, f"Could not start Lean: {exc}", "environment")

    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part.strip()).strip()
    if completed.returncode == 0:
        return VerificationResult(True, output or "Lean accepted the function, theorem, and proof.")
    return VerificationResult(False, output or f"Lean exit code: {completed.returncode}")


def check_locked_declarations(source: str, signature: str, theorem: str) -> str | None:
    """A conservative prototype check; not a full Lean parser or security boundary."""
    normalized = re.sub(r"\s+", "", source)
    for expected in (signature, theorem):
        if re.sub(r"\s+", "", expected) + ":=" not in normalized:
            return "Preserve this exact locked declaration (including every assumption): " + expected
    return None


def verify_against_contract(source: str, verification_wrapper: str, timeout_seconds: int = 20,
                            *, signature: str = "", theorem: str = "") -> VerificationResult:
    """追加由系统锁定的定理，防止 Agent 仅证明一个更弱的命题。"""
    if "verifier_locked_contract" in source:
        return VerificationResult(False, "Agent source may not define the reserved verifier_locked_contract theorem.", "policy")
    if signature and theorem:
        error = check_locked_declarations(source, signature, theorem)
        if error:
            return VerificationResult(False, error, "declaration")
    candidate = verify_lean(source, timeout_seconds)
    if not candidate.passed:
        return candidate
    combined = source.rstrip() + "\n\n-- This theorem is appended by the verifier.\n" + verification_wrapper.strip() + "\n"
    result = verify_lean(combined, timeout_seconds=timeout_seconds)
    if not result.passed and result.stage not in {"environment", "timeout"}:
        return VerificationResult(False, "Candidate alone passed; the appended contract check failed. "
                                  "Review contract integration before retrying.\n" + result.message, "contract")
    return result
