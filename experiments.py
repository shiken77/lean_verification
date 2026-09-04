"""Explicitly invoked, bounded A/B/C comparison. This command calls the paid API."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from agent import DeepSeekAgent
from benchmark_cases import ALL_CASES, POSITIVE_CASES, preflight_benchmark, run_case


def compare(max_attempts: int = 20, repeats: int = 1, workers: int = 3) -> Path:
    if not 1 <= max_attempts <= 20 or not 1 <= repeats <= 10 or not 1 <= workers <= 5:
        raise ValueError("Use 1–20 submissions, 1–10 repetitions, and 1–5 workers.")
    preflight_benchmark()
    agent = DeepSeekAgent(temperature=0.2)
    root = Path(__file__).parent
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = root / ".runs" / f"comparison-{stamp}.json"
    output.parent.mkdir(exist_ok=True)
    fixed = [run_case(case) for case in ALL_CASES if case.expected != "PASS"]
    report = {
        "started_at": stamp, "status": "running", "model_requested": agent.model,
        "temperature": agent.temperature, "max_submissions_per_task": max_attempts,
        "repetitions": repeats, "workers": workers,
        "note": "Exploratory independent trajectories, not paired identical first answers. Mode order rotates by case. "
                "Fixed controls run once; only normal tasks measure model behavior. Diagnosis calls cost extra. "
                "Staged mode requires an intermediate check, so submissions are not directly equivalent to repairs.",
        "source_sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest()
                          for p in ("agent.py", "loop.py", "verifier.py", "benchmark_cases.py", "lean-toolchain")},
        "fixed_controls": [{"id": r.case.id, "passed": r.passed, "observed": r.observed, "detail": r.detail} for r in fixed],
        "model_runs": [], "errors": [],
    }

    def persist() -> None:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    persist()
    print(f"Report: {output}", flush=True)
    print(f"Preflight passed; fixed controls: {sum(r.passed for r in fixed)}/10", flush=True)
    modes = ["raw", "diagnostic", "staged"]
    jobs = []
    for repeat in range(repeats):
        for i, case in enumerate(POSITIVE_CASES):
            offset = (i + repeat) % 3
            for strategy in modes[offset:] + modes[:offset]:
                jobs.append((repeat + 1, case, strategy))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run_case, case, agent, f"DeepSeek {agent.model} / {strategy}",
                                   max_attempts=max_attempts, strategy=strategy): (rep, case.id, strategy)
                   for rep, case, strategy in jobs}
        for future in as_completed(futures):
            rep, case_id, strategy = futures[future]
            try:
                result = future.result()
                run = result.loop_result
                report["model_runs"].append({"repetition": rep, "case": case_id, "strategy": strategy,
                                             "trace_path": result.trace_path, "contract": asdict(result.case.contract),
                                             "specification": result.case.specification, **run.to_dict()})
                print(f"{case_id} / {strategy}: {result.observed}, {len(run.attempts)} submissions, "
                      f"{run.api_calls} calls, {run.total_tokens} tokens, stop={run.stop_reason}", flush=True)
            except Exception as exc:
                report["errors"].append({"repetition": rep, "case": case_id, "strategy": strategy, "error": str(exc)})
                print(f"{case_id} / {strategy}: infrastructure error: {exc}", flush=True)
            persist()
    report["status"] = "done" if not report["errors"] else "completed_with_errors"
    report["summary"] = {}
    for strategy in modes:
        runs = [r for r in report["model_runs"] if r["strategy"] == strategy]
        report["summary"][strategy] = {
            "passed": sum(r["passed"] for r in runs), "tasks": len(runs),
            "submissions": sum(len(r["attempts"]) for r in runs),
            "api_calls": sum(r["api_calls"] for r in runs),
            "total_tokens": sum(r["total_tokens"] for r in runs) if all(r["total_tokens"] is not None for r in runs) else None,
        }
    persist()
    print(json.dumps(report["summary"], indent=2), flush=True)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-attempts", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    compare(args.max_attempts, args.repeats, args.workers)
