"""两阶段网页：先确认形式化规范，再生成函数和证明。"""

from __future__ import annotations

import html
import base64
import binascii
import json
import os
import secrets
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from agent import AgentError, DeepSeekAgent, DemoAgent, ModelCall
from benchmark_cases import BenchmarkResult, run_all_cases
from contracts import FormalContract, assess_specification
from loop import Attempt, LoopResult, STRATEGIES, run_verification_loop, save_trace
from verifier import VerificationResult


DEFAULT_SPEC = """Function name: maximum
Input: two integers a and b
Output: an integer result

Must satisfy:
1. result >= a
2. result >= b
3. result equals a or b
"""

# The formal contract is stored server-side; the browser receives only a random token.
PENDING_CONTRACTS: dict[str, tuple[str, str, FormalContract, int | None, str]] = {}
PENDING_LOCK = threading.RLock()
BENCHMARK_JOBS: dict[str, dict] = {}
VERIFY_JOBS: dict[str, dict] = {}
FORM_TOKEN = secrets.token_urlsafe(32)
FORMALIZING = False


class BusyError(AgentError):
    pass


def check_web_capacity() -> None:
    """Call under PENDING_LOCK. This is a one-process research demo."""
    if FORMALIZING or any(job["status"] == "running" for jobs in (BENCHMARK_JOBS, VERIFY_JOBS) for job in jobs.values()):
        raise BusyError("Another task is running. Please wait for it to finish before starting a new one.")
    # Bound retained results; this is deliberately not durable job storage.
    for jobs in (BENCHMARK_JOBS, VERIFY_JOBS):
        while len(jobs) >= 10:
            jobs.pop(next(iter(jobs)))


def server_address() -> tuple[str, int]:
    host = os.environ.get("HOST", "127.0.0.1")
    password = os.environ.get("APP_ACCESS_PASSWORD", "")
    auth_mode = os.environ.get("APP_REQUIRE_AUTH")
    # Public access must be explicit. The Render image opts out by default;
    # a configured password still enables the optional login in either mode.
    required = auth_mode == "1" or (auth_mode != "0" and host not in {"127.0.0.1", "localhost", "::1"})
    if (required or password) and len(password) < 16:
        raise ValueError("Set APP_ACCESS_PASSWORD to a unique password of at least 16 characters before exposing this app.")
    return host, int(os.environ.get("PORT", "8765"))


def form_token_input() -> str:
    return f'<input type="hidden" name="form_token" value="{FORM_TOKEN}">'


def store_contract(specification: str, mode: str, contract: FormalContract, max_attempts: int | None, strategy: str = "diagnostic") -> str:
    token = secrets.token_urlsafe(24)
    with PENDING_LOCK:
        while len(PENDING_CONTRACTS) >= 100:
            PENDING_CONTRACTS.pop(next(iter(PENDING_CONTRACTS)))
        PENDING_CONTRACTS[token] = (specification, mode, contract, max_attempts, strategy)
    return token


def take_contract(token: str) -> tuple[str, str, FormalContract, int | None, str]:
    with PENDING_LOCK:
        saved = PENDING_CONTRACTS.pop(token, None)
    if saved is None:
        raise AgentError("This formal contract expired or was already used. Please formalize it again.")
    return saved


def parse_run_options(values: dict) -> tuple[int | None, str]:
    choice = values.get("attempts", ["3"])[0]
    legacy = {"until_success": "diagnostic", "until_success_raw": "raw", "until_success_feedback": "diagnostic"}
    limit = None if choice in legacy else int(choice)
    strategy = values.get("strategy", [legacy.get(choice, "diagnostic")])[0]
    if (limit is not None and not 1 <= limit <= 20) or strategy not in STRATEGIES:
        raise ValueError("Select a valid execution mode and a limit from 1 to 20.")
    return limit, strategy


def start_benchmark_job(max_attempts: int | None, strategy: str) -> str:
    job_id = secrets.token_urlsafe(16)
    with PENDING_LOCK:
        check_web_capacity()
        BENCHMARK_JOBS[job_id] = {
            "status": "running", "completed": 0, "total": 15, "attempts": 0,
            "current_case": "Checking all reference contracts before API calls", "current_attempt": 0,
            "current_step": "Preflight", "max_attempts": max_attempts, "strategy": strategy,
            "results": None, "error": "", "attempt_html": "", "case_states": {},
        }

    def run() -> None:
        try:
            agent = DeepSeekAgent()

            def progress(case_id: str, attempt: int, passed: bool) -> None:
                with PENDING_LOCK:
                    job = BENCHMARK_JOBS[job_id]
                    job["attempts"] += 1
                    job["current_case"] = case_id
                    job["current_attempt"] = attempt
            def event(case_id: str, data: dict) -> None:
                with PENDING_LOCK:
                    job = BENCHMARK_JOBS[job_id]
                    job.update(current_case=case_id, current_attempt=data["attempt"], current_step=data["kind"])
                    job["case_states"][case_id] = f'{data["kind"]} · submission {data["attempt"]}'
                    if data["kind"] == "attempt_done":
                        job["attempt_html"] += render_attempt_card(data["record"], case_id)

            def completed(result: BenchmarkResult) -> None:
                with PENDING_LOCK:
                    job = BENCHMARK_JOBS[job_id]
                    job["completed"] += 1
                    job["case_states"][result.case.id] = result.observed

            label = f"DeepSeek {agent.model} / {STRATEGIES[strategy]}"
            results = run_all_cases(agent, label, progress, max_attempts, strategy=strategy,
                                    on_event=event, on_case_done=completed,
                                    workers=int(os.environ.get("BENCHMARK_WORKERS", "5")))
            with PENDING_LOCK:
                BENCHMARK_JOBS[job_id].update({"status": "done", "results": results, "current_case": "Finished"})
        except Exception as exc:
            with PENDING_LOCK:
                BENCHMARK_JOBS[job_id].update({"status": "error", "error": str(exc)})

    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception:
        with PENDING_LOCK:
            BENCHMARK_JOBS.pop(job_id, None)
        raise
    return job_id


def start_verify_job(specification: str, mode: str, contract: FormalContract, max_attempts: int | None, strategy: str = "diagnostic") -> str:
    job_id = secrets.token_urlsafe(16)
    with PENDING_LOCK:
        check_web_capacity()
        VERIFY_JOBS[job_id] = {
            "status": "running", "attempts": 0, "current_attempt": 0,
            "current_step": "Starting", "result": None, "specification": specification, "contract": contract,
            "max_attempts": max_attempts, "error": "", "strategy": strategy, "attempt_html": "",
        }

    def run() -> None:
        try:
            agent = DeepSeekAgent() if mode == "deepseek" else DemoAgent()

            def event(data: dict) -> None:
                with PENDING_LOCK:
                    VERIFY_JOBS[job_id].update({
                        "current_attempt": data["attempt"],
                        "current_step": data["kind"] + " · " + data.get("message", "")[:300],
                    })
                    if data["kind"] == "attempt_done":
                        VERIFY_JOBS[job_id]["attempts"] += 1
                        VERIFY_JOBS[job_id]["attempt_html"] += render_attempt_card(data["record"])

            result = run_verification_loop(specification, contract, agent, max_attempts=max_attempts, strategy=strategy, on_event=event)
            trace = save_trace(result, specification, contract, "web-" + strategy)
            with PENDING_LOCK:
                VERIFY_JOBS[job_id].update({"status": "done", "result": result, "current_step": "Finished", "trace_path": str(trace)})
        except Exception as exc:
            with PENDING_LOCK:
                VERIFY_JOBS[job_id].update({"status": "error", "error": str(exc), "current_step": "Stopped"})

    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception:
        with PENDING_LOCK:
            VERIFY_JOBS.pop(job_id, None)
        raise
    return job_id

CSS = """
:root { color-scheme:light; --ink:#172033; --blue:#2f5bea; --green:#177245; --red:#b42318; --muted:#616a7c; }
* { box-sizing:border-box; }
body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#f0efe9; color:var(--ink); }
main { max-width:1100px; margin:40px auto; padding:0 20px 60px; }
header { background:var(--ink); color:white; padding:34px; border-radius:18px; box-shadow:0 14px 35px #17203322; }
header h1 { margin:0 0 10px; font-size:34px; } header p { margin:0; color:#dbe3ff; line-height:1.6; }
.flow { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin:22px 0; }
.flow a { background:white; border:1px solid #d9dce6; border-radius:12px; padding:14px; text-align:center; font-weight:650; color:var(--ink); text-decoration:none; transition:transform .15s, border-color .15s, box-shadow .15s; }
.flow a:hover { transform:translateY(-2px); border-color:var(--blue); box-shadow:0 5px 14px #17203318; }
.flow .active { border:2px solid var(--blue); color:var(--blue); }
form,.card,.attempt,.result { background:white; border:1px solid #ddd; border-radius:16px; padding:24px; margin-top:18px; }
label { display:block; font-weight:700; margin-bottom:8px; }
textarea { width:100%; min-height:210px; resize:vertical; border:1px solid #bdc4d2; border-radius:10px; padding:14px; font:15px/1.6 ui-monospace,monospace; }
.controls { display:flex; gap:14px; align-items:end; margin-top:15px; flex-wrap:wrap; }
select { padding:10px 12px; border:1px solid #bdc4d2; border-radius:9px; background:white; }
button,.button { display:inline-block; padding:11px 18px; border:0; border-radius:9px; background:var(--blue); color:white; font-weight:750; cursor:pointer; text-decoration:none; }
.secondary { background:#e7eaf2; color:var(--ink); }.hint { color:var(--muted); font-size:14px; }
.pass { color:var(--green); }.fail { color:var(--red); }.result.pass { border-left:6px solid var(--green); }.result.fail { border-left:6px solid var(--red); }
summary { cursor:pointer; font-size:20px; font-weight:750; }
pre { overflow:auto; background:#111827; color:#e5e7eb; padding:16px; border-radius:10px; line-height:1.5; white-space:pre-wrap; }
pre.feedback { background:#f7f5ef; color:#303746; border:1px solid #e0ddd2; }
.contract-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }.contract-grid .wide { grid-column:1/-1; }
.benchmark { width:100%; border-collapse:collapse; background:white; margin-top:18px; }.benchmark th,.benchmark td { border-bottom:1px solid #ddd; padding:12px; text-align:left; }
.table-scroll { overflow-x:auto; }.progress-track { height:18px;background:#e7eaf2;border-radius:99px;overflow:hidden;margin:22px 0; }
.busy { height:100%;width:30%;background:var(--blue);animation:busy 1.4s ease-in-out infinite alternate; }
@keyframes busy { from {transform:translateX(0)} to {transform:translateX(230%)} }
@media (prefers-reduced-motion:reduce) { .busy { animation:none; } }
@media (max-width:720px) { .flow,.contract-grid { grid-template-columns:1fr; }.contract-grid .wide { grid-column:auto; } header h1 { font-size:28px; } }
"""


def page_shell(content: str, active_step: int = 1) -> bytes:
    public_notice = ""
    if os.environ.get("APP_REQUIRE_AUTH") == "0" and not os.environ.get("APP_ACCESS_PASSWORD"):
        public_notice = '<p class="hint" role="note">Public research demo · No login. Model requests use the owner\'s DeepSeek credit. Do not enter private information; results are not private.</p>'
    steps = [
        ("1. Function spec", "/"),
        ("2. Confirm theorem", "/formalize"),
        ("3. Lean checks", "/verify"),
        ("4. PASS / fix", "/benchmark"),
    ]
    flow = "".join(
        f'<a class="{"active" if i == active_step else ""}" href="{href}">{label}</a>'
        for i, (label, href) in enumerate(steps, 1)
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lean Verification Loop</title><style>{CSS}</style></head><body><main>
<header><h1>Lean Verification Loop</h1><p>Requirement → formal contract → human confirmation → implementation and proof → Lean verification</p></header>
<section class="flow">{flow}</section>{public_notice}{content}</main></body></html>""".encode("utf-8")


def render_start(specification: str = DEFAULT_SPEC, mode: str = "demo", error: str = "") -> bytes:
    api_ready = bool(os.environ.get("DEEPSEEK_API_KEY"))
    error_html = f'<section class="result fail"><h2>More information needed</h2><p>{html.escape(error)}</p></section>' if error else ""
    content = f"""{error_html}<form method="post" action="/formalize">{form_token_input()}
<h2>Step 1 · Describe the function</h2>
<label for="specification">Natural-language requirement</label>
<textarea id="specification" name="specification" required>{html.escape(specification)}</textarea>
<div class="controls"><div><label for="mode">Agent</label><select id="mode" name="mode" onchange="updateBenchmarkLink()">
<option value="demo" {'selected' if mode == 'demo' else ''}>Built-in maximum demo</option>
<option value="deepseek" {'selected' if mode == 'deepseek' else ''}>DeepSeek API</option></select></div>
<div><label for="attempts">Normal-case retry policy</label><select id="attempts" name="attempts" onchange="updateBenchmarkLink()">
<option value="3">Try up to 3 times</option><option value="5">Try up to 5 times</option><option value="until_success">Until success (max 20 submissions)</option></select></div>
<div><label for="strategy">Execution mode</label><select id="strategy" name="strategy" onchange="updateBenchmarkLink()">
<option value="raw">A · Raw Lean errors</option><option value="diagnostic" selected>B · Diagnosis + history</option><option value="staged">C · Checkable steps + diagnosis</option></select></div>
<button type="submit">Create formal contract</button><button type="submit" id="benchmark-link" class="secondary" formaction="/benchmark" formnovalidate>Run 15-case test set with this Agent</button>
<span class="hint">DeepSeek API: {'configured' if api_ready else 'DEEPSEEK_API_KEY not set'}</span></div>
<p class="hint">B/C may use an extra model call to diagnose a failed submission. C verifies intermediate steps; those are not final PASS. The built-in demo is scripted and does not measure feedback quality.</p></form><p><a class="button secondary" href="/comparison">View saved A/B/C comparisons (no API call)</a></p>
<script>function updateBenchmarkLink() {{ const mode = document.getElementById('mode').value; const strategy = document.getElementById('strategy'); strategy.querySelector('[value="staged"]').disabled = mode !== 'deepseek'; if (mode !== 'deepseek' &amp;&amp; strategy.value === 'staged') strategy.value = 'diagnostic'; }} updateBenchmarkLink();</script>""".replace('&amp;&amp;', '&&')
    return page_shell(content, 1)


def render_confirmation(specification: str, contract: FormalContract, token: str) -> bytes:
    content = f"""<section class="card"><h2>Step 2 · Confirm the locked formal contract</h2>
<p>The agent translated your requirement into the following Lean contract. Confirm that its plain-language meaning matches your intent.</p>
<div class="contract-grid"><div><h3>Function signature</h3><pre>{html.escape(contract.function_signature)}</pre></div>
<div><h3>Plain-language meaning</h3><p>{html.escape(contract.explanation)}</p></div>
<div class="wide"><h3>Correctness theorem</h3><pre>{html.escape(contract.theorem_statement)}</pre></div></div></section>
<form method="post" action="/verify">{form_token_input()}<input type="hidden" name="contract_token" value="{html.escape(token, quote=True)}">
<label><input type="checkbox" name="confirmed" value="yes" required> I confirm that this theorem expresses my requirement.</label>
<div class="controls"><button type="submit">Confirm, generate and verify</button><a class="button secondary" href="/">Go back and revise</a></div></form>"""
    return page_shell(content, 2)


def render_attempt_card(attempt: Attempt, case_id: str = "") -> str:
    status = "STEP ACCEPTED (not final PASS)" if attempt.verification.stage == "intermediate" else ("PASS" if attempt.verification.passed else "FAIL")
    esc = html.escape
    plan = "\n".join(f"{i}. {step}" for i, step in enumerate(attempt.plan, 1)) or "No explicit plan in this mode."
    requests = "".join(f'<details><summary>{esc(call.purpose)} · {esc(call.model)} · exact prompt</summary><pre>{esc(call.system_prompt)}</pre><pre>{esc(call.user_prompt)}</pre><h4>Model response</h4><pre>{esc(call.response)}</pre></details>' for call in attempt.calls)
    return f'''<details class="attempt"><summary>{esc(case_id)} Attempt {attempt.number} · {status}</summary>
<p>Step: {esc(attempt.current_step)} · check stage: {esc(attempt.verification.stage)} · {attempt.api_calls} API calls · {attempt.elapsed_seconds:.1f}s</p>
<h3>Plan</h3><pre class="feedback">{esc(plan)}</pre><h3>Change summary</h3><p>{esc(attempt.change_summary or 'Not separately reported in this mode.')}</p>
<h3>Feedback actually supplied to this attempt</h3><pre class="feedback">{esc(attempt.feedback_received or 'First submission; no previous feedback.')}</pre>
<h3>Agent-generated Lean</h3><pre>{esc(attempt.code)}</pre><h3>Verifier evidence</h3><pre class="feedback">{esc(attempt.verification.message)}</pre>
<h3>Feedback prepared for the next attempt</h3><pre class="feedback">{esc(attempt.repair_feedback or 'No next repair scheduled: completed, stopped, or limit reached.')}</pre>{requests}</details>'''


def render_attempts(specification: str, contract: FormalContract, result: LoopResult) -> bytes:
    final_class = "pass" if result.passed else "fail"
    final_text = "PASS: Lean accepted the locked contract" if result.passed else "NOT PASSED: " + result.stop_reason
    tokens = str(result.total_tokens) if result.total_tokens is not None else "unavailable"
    models = ', '.join(sorted({c.model for a in result.attempts for c in a.calls})) or 'Scripted local demo (no API)'
    cards = [f'<section class="result {final_class}"><h2>{html.escape(final_text)}</h2><p>{html.escape(STRATEGIES[result.strategy])} · Submissions: {len(result.attempts)} · API calls: {result.api_calls} · Tokens: {tokens}</p><p>Reported model: {html.escape(models)}</p><p>Attempts to success: {len(result.attempts) if result.passed else "— (not successful)"}</p><p class="hint">Usage covers this verification loop, excluding earlier formal-contract generation.</p></section>']
    cards.append(f'<section class="card"><h3>Locked theorem</h3><pre>{html.escape(contract.theorem_statement)}</pre></section>')
    cards.extend(render_attempt_card(attempt) for attempt in result.attempts)
    cards.append('<p><a class="button secondary" href="/">Start another requirement</a> <a class="button secondary" href="/benchmark">View test set</a></p>')
    return page_shell("\n".join(cards), 4)


def render_benchmark(results: list[BenchmarkResult]) -> bytes:
    passed = sum(result.passed for result in results)
    total_attempts = sum(result.attempts for result in results)
    model_cases = sum(result.loop_result is not None for result in results)
    api_calls = sum(result.loop_result.api_calls for result in results if result.loop_result)
    rows = []
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        run = result.loop_result
        success_attempt = str(result.attempts) if run and run.passed else "—"
        stop = run.stop_reason if run else "Fixed check"
        cost = f'{run.api_calls} / {run.total_tokens if run.total_tokens is not None else "unavailable"}' if run else '0 / 0'
        actual_models = ', '.join(sorted({c.model for a in run.attempts for c in a.calls})) if run else ''
        source = result.execution_source + (f' (API reports: {actual_models})' if actual_models else '')
        rows.append(f"<tr><td>{html.escape(result.case.id)}</td><td>{result.case.expected}</td><td>{result.observed}</td><td class='{status.lower()}'>{status}</td><td>{result.attempts}</td><td>{success_attempt}</td><td>{cost}</td><td>{html.escape(stop)}</td><td>{html.escape(source)}</td></tr>")
    content = f"""<section class="result {'pass' if passed == len(results) else 'fail'}"><h2>15-case verifier benchmark: {passed}/{len(results)} passed</h2>
<p><strong>Summary:</strong> {model_cases} model-generated cases; {len(results) - model_cases} fixed verifier cases; {total_attempts} total submissions/checks; {api_calls} API calls.</p>
<p><strong>Execution source:</strong> normal cases use the selected Agent + Lean. Faulty, bypass, and ambiguous cases remain fixed local cases. If the selected Agent is DeepSeek, only the 5 normal cases call the DeepSeek API.</p>
<p>Intermediate acceptance is not final PASS. The clarification fixtures are not evidence of model reasoning. PASS proves only the locked formal property.</p></section>
<div class="table-scroll"><table class="benchmark"><thead><tr><th>ID</th><th>Expected</th><th>Observed</th><th>Result</th><th>Submissions / checks</th><th>Attempts to success</th><th>API calls / tokens</th><th>Stop reason</th><th>Execution source</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
{''.join(render_attempt_card(a, r.case.id) for r in results if r.loop_result for a in r.loop_result.attempts)}
<p><a class="button secondary" href="/">Back to prototype</a></p>"""
    return page_shell(content, 4)


def render_benchmark_loading(job_id: str) -> bytes:
    content = f"""<section class="card"><h2>Running 15-case test set...</h2>
<p id="progress-text">Starting the selected Agent and Lean verifier...</p>
<div style="height:18px;background:#e7eaf2;border-radius:99px;overflow:hidden;margin:22px 0"><div id="progress-bar" style="height:100%;width:0%;background:var(--blue);transition:width .3s"></div></div>
<p class="hint">Progress counts finished cases, including stopped cases. Every task has at most 20 submissions. Three identical outcomes stop early; diagnostics are extra API calls.</p><pre class="feedback" id="case-states"></pre></section><section id="live-attempts"></section>
<script>
const jobId = {json.dumps(job_id)};
let previousHtml = '';
async function poll() {{
 try {{
  const response = await fetch('/benchmark/status?job=' + encodeURIComponent(jobId));
  if (!response.ok) throw new Error('Status unavailable (server may have restarted).');
  const job = await response.json();
  const percent = Math.round(job.completed / job.total * 100);
  document.getElementById('progress-bar').style.width = percent + '%';
  document.getElementById('progress-text').textContent = `${{percent}}% · ${{job.completed}}/${{job.total}} cases · ${{job.attempts}} submissions/checks · ${{job.current_case}} · ${{job.current_step}} (attempt ${{job.current_attempt}})`;
  document.getElementById('case-states').textContent = Object.entries(job.case_states).map(([id, state]) => id + ': ' + state).join(String.fromCharCode(10));
  if (previousHtml !== job.attempt_html) {{ document.getElementById('live-attempts').innerHTML = job.attempt_html; previousHtml = job.attempt_html; }}
  if (job.status === 'done') {{ window.location.href = '/benchmark?job=' + encodeURIComponent(jobId); return; }}
  if (job.status === 'error') {{ document.getElementById('progress-text').textContent = 'Benchmark stopped: ' + job.error; return; }}
 }} catch (error) {{ document.getElementById('progress-text').textContent = error.message + ' Retrying status...'; }}
  setTimeout(poll, 1500);
}}
poll();
</script>"""
    return page_shell(content, 4)


def render_comparison(filename: str = "") -> bytes:
    paths = {p.name: p for p in sorted((Path(__file__).parent / '.runs').glob('comparison-*.json'), reverse=True)}
    if not paths:
        return page_shell('<section class="card"><h2>No saved comparisons yet</h2><p>Run experiments.py to create a bounded API comparison. Viewing this page never starts a model run.</p></section>', 4)
    selected = filename or next(iter(paths))
    if selected not in paths:
        return page_shell('<section class="result fail"><h2>Unknown comparison</h2></section>', 4)
    try:
        data = json.loads(paths[selected].read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return page_shell('<section class="card"><h2>Report is being saved. Please refresh.</h2></section>', 4)
    esc = html.escape
    fixed_passed = sum(r['passed'] for r in data['fixed_controls'])
    actual_models = ', '.join(sorted({c['model'] for r in data['model_runs'] for a in r['attempts'] for c in a['calls']})) or 'Not available yet'
    summary_rows = []
    for strategy, label in STRATEGIES.items():
        runs = [r for r in data['model_runs'] if r['strategy'] == strategy]
        tokens = sum(r['total_tokens'] for r in runs) if all(r['total_tokens'] is not None for r in runs) else 'unavailable'
        summary_rows.append(f"<tr><td>{esc(label)}</td><td>{sum(r['passed'] for r in runs)}/{len(runs)}</td><td>{sum(len(r['attempts']) for r in runs)}</td><td>{sum(r['api_calls'] for r in runs)}</td><td>{tokens}</td></tr>")
    rows, cards = [], []
    for run in sorted(data['model_runs'], key=lambda r: (r['case'], r['strategy'], r['repetition'])):
        attempts_to_success = run['attempts_to_success'] if run['passed'] else '—'
        rows.append(f"<tr><td>{esc(run['case'])}</td><td>{esc(run['strategy'])}</td><td>{run['repetition']}</td><td>{'PASS' if run['passed'] else 'NOT PASSED'}</td><td>{len(run['attempts'])}</td><td>{attempts_to_success}</td><td>{esc(run['stop_reason'])}</td></tr>")
        for record in run['attempts']:
            attempt = Attempt(**{**record, 'verification': VerificationResult(**record['verification']),
                                 'calls': [ModelCall(**c) for c in record['calls']]})
            cards.append(render_attempt_card(attempt, run['case'] + ' / ' + run['strategy']))
    links = ' · '.join(f'<a href="/comparison?run={esc(name, quote=True)}">{esc(name)}</a>' for name in paths)
    content = f'''<section class="card"><h2>Saved A/B/C comparison</h2>
<p>Report: {esc(selected)} · status: {esc(data['status'])}</p>
<p>Requested model: {esc(data['model_requested'])} · API-reported model: {esc(actual_models)} · temperature: {data['temperature']} · cap: {data['max_submissions_per_task']} submissions/task</p>
<p>Fixed controls: {fixed_passed}/10 (run once, shared across modes). Only the five normal cases measure model behavior.</p>
<p class="hint">{esc(data['note'])} A single comparison does not establish a general improvement.</p>
<div class="table-scroll"><table class="benchmark"><thead><tr><th>Mode</th><th>Normal tasks passed</th><th>Submissions</th><th>API calls</th><th>Total tokens</th></tr></thead><tbody>{''.join(summary_rows)}</tbody></table></div></section>
<div class="table-scroll"><table class="benchmark"><thead><tr><th>Case</th><th>Mode</th><th>Repeat</th><th>Outcome</th><th>Submissions</th><th>Attempts to success</th><th>Stop reason</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<details class="card"><summary>Saved reports</summary>{links}</details>
<details class="card"><summary>Experiment errors and exact source fingerprints</summary><pre>{esc(json.dumps({'errors': data['errors'], 'source_sha256': data['source_sha256']}, indent=2))}</pre></details>
{''.join(cards)}<p><a class="button" href="/">Back to prototype</a></p>'''
    return page_shell(content, 4)


def render_verify_loading(job_id: str) -> bytes:
    content = f"""<section class="card"><h2>Verifying the confirmed theorem...</h2>
<p id="verify-step">Theorem confirmed ✓ · preparing the Agent...</p>
<div class="progress-track" role="progressbar" aria-label="Verification in progress"><div id="verify-bar" class="busy"></div></div>
<p class="hint">The animation indicates activity, not a predicted completion percentage. Intermediate steps are checked but never counted as final PASS.</p>
<ol><li>Theorem confirmed and locked ✓</li><li>Agent prepares a submission</li><li>Lean checks the submitted step or full proof</li><li>Repair, continue, finish, or stop with an explicit reason</li></ol></section><section id="live-attempts"></section>
<script>
const jobId = {json.dumps(job_id)};
let previousHtml = '';
async function pollVerify() {{
 try {{
  const response = await fetch('/verify/status?job=' + encodeURIComponent(jobId));
  if (!response.ok) throw new Error('Status unavailable (server may have restarted).');
  const job = await response.json();
  document.getElementById('verify-step').textContent = `${{job.current_step}} · submission ${{job.current_attempt}}/${{job.max_attempts === null ? 20 : job.max_attempts}} · mode: ${{job.strategy}}`;
  if (previousHtml !== job.attempt_html) {{ document.getElementById('live-attempts').innerHTML = job.attempt_html; previousHtml = job.attempt_html; }}
  if (job.status === 'done') {{ window.location.href = '/verify?job=' + encodeURIComponent(jobId); return; }}
  if (job.status === 'error') {{ document.getElementById('verify-step').textContent = 'Verification stopped: ' + job.error; return; }}
 }} catch (error) {{ document.getElementById('verify-step').textContent = error.message + ' Retrying status...'; }}
  setTimeout(pollVerify, 1500);
}}
pollVerify();
</script>"""
    return page_shell(content, 3)


def render_stage_info(title: str, description: str, next_href: str, next_label: str, active_step: int) -> bytes:
    content = f"""<section class="card"><h2>{html.escape(title)}</h2>
<p>{html.escape(description)}</p><p><a class="button" href="{html.escape(next_href, quote=True)}">{html.escape(next_label)}</a>
<a class="button secondary" href="/">Start from the beginning</a></p></section>"""
    return page_shell(content, active_step)


class Handler(BaseHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        super().end_headers()

    def _authorized(self) -> bool:
        password = os.environ.get("APP_ACCESS_PASSWORD", "")
        if not password:
            if os.environ.get("APP_REQUIRE_AUTH") == "1":
                self.send_error(503, "Access protection is not configured")
                return False
            return True
        expected = ("guest:" + password).encode("utf-8")
        try:
            scheme, value = self.headers.get("Authorization", "").split(" ", 1)
            received = base64.b64decode(value, validate=True)
            if scheme.lower() == "basic" and secrets.compare_digest(received, expected):
                return True
        except (ValueError, binascii.Error):
            pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Lean friends demo", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            self._send(b"ok")
            return
        if not self._authorized():
            return
        if parsed.path == "/":
            self._send(render_start())
        elif parsed.path == "/comparison":
            self._send(render_comparison(parse_qs(parsed.query).get("run", [""])[0]))
        elif parsed.path == "/formalize":
            self._send(render_stage_info(
                "Step 2 · Confirm theorem",
                "The agent translates your natural-language requirement into a formal Lean contract. After you confirm it, the contract is locked before code generation begins.",
                "/",
                "Enter a function requirement",
                2,
            ))
        elif parsed.path == "/verify":
            query = parse_qs(parsed.query)
            job_id = query.get("job", [""])[0]
            if job_id:
                with PENDING_LOCK:
                    job = VERIFY_JOBS.get(job_id)
                    if job is None:
                        self.send_error(404)
                        return
                    if job["status"] == "running":
                        self._send(render_verify_loading(job_id))
                        return
                    if job["status"] == "error":
                        self._send(render_start(error=job["error"]), 500)
                        return
                    result = job["result"]
                    specification = job["specification"]
                    contract = job["contract"]
                self._send(render_attempts(specification, contract, result))
                return
            self._send(render_stage_info(
                "Step 3 · Lean checks",
                "After the formal contract is confirmed, the agent generates a Lean implementation and proof. Lean checks the proof and returns PASS or FAIL; failed attempts are sent back for repair.",
                "/",
                "Start a verification run",
                3,
            ))
        elif parsed.path == "/benchmark":
            query = parse_qs(parsed.query)
            job_id = query.get("job", [""])[0]
            if job_id:
                with PENDING_LOCK:
                    job = BENCHMARK_JOBS.get(job_id)
                    if job is None:
                        self.send_error(404)
                        return
                    if job["status"] == "running":
                        self._send(render_benchmark_loading(job_id))
                        return
                    if job["status"] == "error":
                        self._send(render_start(error=job["error"]), 500)
                        return
                    results = job["results"]
                self._send(render_benchmark(results))
                return
            # Opening, prefetching, or refreshing a link must never start paid work.
            self._send(render_stage_info("15-case test set", "Select an Agent and click Run 15-case test set on the home page. Opening this page does not start a new run.", "/", "Choose Agent and run options", 4))
        elif parsed.path == "/verify/status":
            job_id = parse_qs(parsed.query).get("job", [""])[0]
            with PENDING_LOCK:
                job = VERIFY_JOBS.get(job_id)
                if job is None:
                    self.send_error(404)
                    return
                payload = {key: job[key] for key in ("status", "attempts", "current_attempt", "current_step", "max_attempts", "error", "strategy", "attempt_html")}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/benchmark/status":
            job_id = parse_qs(parsed.query).get("job", [""])[0]
            with PENDING_LOCK:
                job = BENCHMARK_JOBS.get(job_id)
                if job is None:
                    self.send_error(404)
                    return
                payload = {key: job[key] for key in ("status", "completed", "total", "attempts", "current_case", "current_attempt", "error", "current_step", "case_states", "attempt_html", "strategy")}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        global FORMALIZING
        if not self._authorized():
            return
        if self.path not in {"/formalize", "/verify", "/benchmark"}:
            self.send_error(404)
            return
        form = self._read_form()
        if form is None:
            return
        if not secrets.compare_digest(form.get("form_token", [""])[0].encode(), FORM_TOKEN.encode()):
            self.send_error(403, "Refresh the page before submitting this form")
            return
        specification = form.get("specification", [""])[0].strip()
        mode = form.get("mode", ["demo"])[0]
        try:
            if mode not in {"demo", "deepseek"}:
                raise ValueError("Select a supported Agent.")
            if self.path == "/benchmark":
                max_attempts, strategy = parse_run_options(form)
                if mode == "deepseek":
                    self._redirect("/benchmark?job=" + start_benchmark_job(max_attempts, strategy))
                else:
                    with PENDING_LOCK:
                        check_web_capacity()
                        FORMALIZING = True
                    try:
                        self._send(render_benchmark(run_all_cases()))
                    finally:
                        with PENDING_LOCK:
                            FORMALIZING = False
                return
            if self.path == "/formalize":
                max_attempts, strategy = parse_run_options(form)
                if mode != "deepseek" and strategy == "staged":
                    raise ValueError("Checkable steps require DeepSeek API; the built-in demo is scripted.")
                clear, reason = assess_specification(specification)
                if not clear:
                    self._send(render_start(specification, mode, reason), 400)
                    return
                with PENDING_LOCK:
                    check_web_capacity()
                    FORMALIZING = True
                try:
                    agent = DeepSeekAgent() if mode == "deepseek" else DemoAgent()
                    contract = agent.formalize(specification)
                finally:
                    with PENDING_LOCK:
                        FORMALIZING = False
                token = store_contract(specification, mode, contract, max_attempts, strategy)
                self._send(render_confirmation(specification, contract, token))
                return

            if form.get("confirmed", [""])[0] != "yes":
                raise AgentError("You must confirm the formal contract before verification.")
            with PENDING_LOCK:
                check_web_capacity()  # Do not consume a confirmation while another task is busy.
                token = form.get("contract_token", [""])[0]
                saved = take_contract(token)
                try:
                    job_id = start_verify_job(*saved)
                except Exception:
                    PENDING_CONTRACTS[token] = saved
                    raise
            self._redirect("/verify?job=" + job_id)
        except BusyError as exc:
            self._send(render_start(specification, mode, str(exc)), 429)
        except (AgentError, ValueError) as exc:
            self._send(render_start(specification, mode, str(exc)), 400)
        except Exception as exc:
            self._send(render_start(specification, mode, f"Unexpected error: {exc}"), 500)

    def _read_form(self) -> dict[str, list[str]] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or self.headers.get("Transfer-Encoding"):
                raise ValueError("Invalid request length")
        except ValueError:
            self.send_error(400)
            return None
        if length > 50_000:
            self.send_error(413)
            return None
        try:
            return parse_qs(self.rfile.read(length).decode("utf-8"), max_num_fields=20)
        except (UnicodeDecodeError, ValueError):
            self.send_error(400)
            return None

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[web] {format % args}")


if __name__ == "__main__":
    address = server_address()
    # Fail the deployment before reporting healthy if Lean is missing or broken.
    from verifier import verify_lean
    readiness = verify_lean("theorem deployment_ready : True := by trivial")
    if not readiness.passed:
        raise SystemExit("Lean startup check failed: " + readiness.message)
    print(f"Lean verification prototype: http://{address[0]}:{address[1]}")
    ThreadingHTTPServer(address, Handler).serve_forever()
