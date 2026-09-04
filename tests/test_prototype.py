from __future__ import annotations

import unittest

from agent import DemoAgent, extract_lean_code
from benchmark_cases import ALL_CASES, run_case
from contracts import MAXIMUM_CONTRACT, FormalContract, assess_specification
from loop import run_verification_loop
from verifier import find_lean


SPEC = """输入：两个整数 a 和 b
输出：两者中较大的整数
必须满足：结果不小于 a，不小于 b，并且等于 a 或 b。
"""


class InfrastructureTests(unittest.TestCase):
    def test_extracts_fenced_code(self) -> None:
        self.assertEqual(extract_lean_code("text```lean\ndef x := 1\n```"), "def x := 1")

    def test_contract_round_trip(self) -> None:
        self.assertEqual(FormalContract.from_json(MAXIMUM_CONTRACT.to_json()), MAXIMUM_CONTRACT)

    def test_clear_specification(self) -> None:
        self.assertTrue(assess_specification(SPEC)[0])

    def test_demo_fails_then_passes_against_locked_contract(self) -> None:
        lean, _ = find_lean()
        if not lean:
            self.skipTest("Lean 尚未安装")
        result = run_verification_loop(SPEC, MAXIMUM_CONTRACT, DemoAgent(), max_attempts=2)
        self.assertTrue(result.passed)
        self.assertEqual(len(result.attempts), 2)
        self.assertFalse(result.attempts[0].verification.passed)
        self.assertTrue(result.attempts[1].verification.passed)


class FifteenCaseBenchmarkTests(unittest.TestCase):
    def test_dataset_contains_exactly_15_cases(self) -> None:
        self.assertEqual(len(ALL_CASES), 15)
        self.assertEqual(len({case.id for case in ALL_CASES}), 15)


def _add_case_test(case) -> None:
    def test(self: unittest.TestCase) -> None:
        lean, _ = find_lean()
        if case.expected != "CLARIFY" and not lean:
            self.skipTest("Lean 尚未安装")
        result = run_case(case)
        self.assertTrue(
            result.passed,
            f"{case.id}: expected {case.expected}, observed {result.observed}\n{result.detail}",
        )

    test.__name__ = f"test_case_{case.id}"
    setattr(FifteenCaseBenchmarkTests, test.__name__, test)


for benchmark_case in ALL_CASES:
    _add_case_test(benchmark_case)


if __name__ == "__main__":
    unittest.main()
