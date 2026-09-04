"""两阶段网页：先确认形式化规范，再生成函数和证明。"""

from __future__ import annotations

import html
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from agent import AgentError, DeepSeekAgent, DemoAgent
from benchmark_cases import BenchmarkResult, run_all_cases
from contracts import FormalContract, assess_specification
from loop import LoopResult, run_verification_loop


DEFAULT_SPEC = """Function name: maximum
Input: two integers a and b
Output: an integer result

Must satisfy:
1. result >= a
2. result >= b
3. result equals a or b
"""

# The formal contract is stored server-side; the browser receives only a random token.
PENDING_CONTRACTS: dict[str, tuple[str, str, FormalContract, int | None]] = {}
PENDING_LOCK = threading.Lock()
BENCHMARK_JOBS: dict[str, dict] = {}
VERIFY_JOBS: dict[str, dict] = {}


def store_contract(specification: str, mode: str, contract: FormalContract, max_attempts: int | None) -> str:
    token = secrets.token_urlsafe(24)
    with PENDING_LOCK:
        PENDING_CONTRACTS[token] = (specification, mode, contract, max_attempts)
    return token


def take_contract(token: str) -> tuple[str, str, FormalContract, int | None]:
    with PENDING_LOCK:
        saved = PENDING_CONTRACTS.pop(token, None)
    if saved is None:
        raise AgentError("This formal contract expired or was already used. Please formalize it again.")
    return saved


def start_benchmark_job(max_attempts: int | None) -> str:
    job_id = secrets.token_urlsafe(16)
    with PENDING_LOCK:
        BENCHMARK_JOBS[job_id] = {
            "status": "running", "completed": 0, "total": 15, "attempts": 0,
            "current_case": "Starting", "current_attempt": 0, "max_attempts": max_attempts, "results": None, "error": "",
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
                    if passed or (max_attempts is not None and attempt >= max_attempts):
                        job["completed"] += 1

            results = run_all_cases(agent, "DeepSeek agent", progress, max_attempts)
            with PENDING_LOCK:
                BENCHMARK_JOBS[job_id].update({"status": "done", "results": results, "current_case": "Finished"})
        except Exception as exc:
            with PENDING_LOCK:
                BENCHMARK_JOBS[job_id].update({"status": "error", "error": str(exc)})

    threading.Thread(target=run, daemon=True).start()
    return job_id


def start_verify_job(specification: str, mode: str, contract: FormalContract, max_attempts: int | None) -> str:
    job_id = secrets.token_urlsafe(16)
    with PENDING_LOCK:
        VERIFY_JOBS[job_id] = {
            "status": "running", "attempts": 0, "current_attempt": 0,
            "current_step": "Starting", "result": None, "specification": specification, "contract": contract, "max_attempts": max_attempts, "error": "",
        }

    def run() -> None:
        try:
            agent = DeepSeekAgent() if mode == "deepseek" else DemoAgent()

            def progress(attempt: int, passed: bool) -> None:
                with PENDING_LOCK:
                    VERIFY_JOBS[job_id].update({
                        "attempts": attempt,
                        "current_attempt": attempt,
                        "current_step": "Lean accepted the proof" if passed else "Lean failed; sending feedback for repair",
                    })

            result = run_verification_loop(specification, contract, agent, max_attempts=max_attempts, on_attempt=progress)
            with PENDING_LOCK:
                VERIFY_JOBS[job_id].update({"status": "done", "result": result, "current_step": "Finished"})
        except Exception as exc:
            with PENDING_LOCK:
                VERIFY_JOBS[job_id].update({"status": "error", "error": str(exc), "current_step": "Stopped"})

    threading.Thread(target=run, daemon=True).start()
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
@media (max-width:720px) { .flow,.contract-grid { grid-template-columns:1fr; }.contract-grid .wide { grid-column:auto; } header h1 { font-size:28px; } }
"""


def page_shell(content: str, active_step: int = 1) -> bytes:
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
<section class="flow">{flow}</section>{content}</main></body></html>""".encode("utf-8")


def render_start(specification: str = DEFAULT_SPEC, mode: str = "demo", error: str = "") -> bytes:
    api_ready = bool(os.environ.get("DEEPSEEK_API_KEY"))
    error_html = f'<section class="result fail"><h2>More information needed</h2><p>{html.escape(error)}</p></section>' if error else ""
    content = f"""{error_html}<form method="post" action="/formalize">
<h2>Step 1 · Describe the function</h2>
<label for="specification">Natural-language requirement</label>
<textarea id="specification" name="specification" required>{html.escape(specification)}</textarea>
<div class="controls"><div><label for="mode">Agent</label><select id="mode" name="mode" onchange="document.getElementById('benchmark-link').href='/benchmark?mode='+encodeURIComponent(this.value)">
<option value="demo" {'selected' if mode == 'demo' else ''}>Built-in maximum demo</option>
<option value="deepseek" {'selected' if mode == 'deepseek' else ''}>DeepSeek API</option></select></div>
<div><label for="attempts">Normal-case retry policy</label><select id="attempts" name="attempts" onchange="updateBenchmarkLink()">
<option value="3">Try up to 3 times</option><option value="5">Try up to 5 times</option><option value="until_success">Until success</option></select></div>
<button type="submit">Create formal contract</button><a id="benchmark-link" class="button secondary" href="/benchmark?mode={html.escape(mode, quote=True)}&attempts=3" onclick="this.textContent='Running 15 cases...'; this.style.pointerEvents='none'; this.style.opacity='.65';">Run 15-case test set with this Agent</a>
<span class="hint">DeepSeek API: {'configured' if api_ready else 'DEEPSEEK_API_KEY not set'}</span></div></form>
<script>function updateBenchmarkLink() {{ const mode = document.getElementById('mode').value; const attempts = document.getElementById('attempts').value; document.getElementById('benchmark-link').href = '/benchmark?mode=' + encodeURIComponent(mode) + '&attempts=' + encodeURIComponent(attempts); }} updateBenchmarkLink();</script>"""
    return page_shell(content, 1)


def render_confirmation(specification: str, contract: FormalContract, token: str) -> bytes:
    content = f"""<section class="card"><h2>Step 2 · Confirm the locked formal contract</h2>
<p>The agent translated your requirement into the following Lean contract. Confirm that its plain-language meaning matches your intent.</p>
<div class="contract-grid"><div><h3>Function signature</h3><pre>{html.escape(contract.function_signature)}</pre></div>
<div><h3>Plain-language meaning</h3><p>{html.escape(contract.explanation)}</p></div>
<div class="wide"><h3>Correctness theorem</h3><pre>{html.escape(contract.theorem_statement)}</pre></div></div></section>
<form method="post" action="/verify"><input type="hidden" name="contract_token" value="{html.escape(token, quote=True)}">
<label><input type="checkbox" name="confirmed" value="yes" required> I confirm that this theorem expresses my requirement.</label>
<div class="controls"><button type="submit">Confirm, generate and verify</button><a class="button secondary" href="/">Go back and revise</a></div></form>"""
    return page_shell(content, 2)


def render_attempts(specification: str, contract: FormalContract, result: LoopResult) -> bytes:
    final_class = "pass" if result.passed else "fail"
    final_text = "PASS: Lean accepted the locked contract" if result.passed else "FAIL: retry limit reached"
    cards = [f'<section class="result {final_class}"><h2>{final_text}</h2><p>Attempts: {len(result.attempts)}</p></section>']
    cards.append(f'<section class="card"><h3>Locked theorem</h3><pre>{html.escape(contract.theorem_statement)}</pre></section>')
    for attempt in result.attempts:
        status = "PASS" if attempt.verification.passed else "FAIL"
        cards.append(f"""<details open class="attempt"><summary>Attempt {attempt.number} · <span class="{status.lower()}">{status}</span></summary>
<h3>Agent-generated Lean</h3><pre><code>{html.escape(attempt.code)}</code></pre><h3>Lean feedback</h3>
<pre class="feedback">{html.escape(attempt.verification.message)}</pre></details>""")
    cards.append('<p><a class="button secondary" href="/">Start another requirement</a> <a class="button secondary" href="/benchmark">View test set</a></p>')
    return page_shell("\n".join(cards), 3 if not result.passed else 4)


def render_benchmark(results: list[BenchmarkResult]) -> bytes:
    passed = sum(result.passed for result in results)
    total_attempts = sum(result.attempts for result in results)
    model_cases = sum(result.execution_source.endswith("+ Lean") and "Fixed case" not in result.execution_source for result in results)
    rows = []
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        rows.append(f"<tr><td>{html.escape(result.case.id)}</td><td>{html.escape(result.case.group)}</td><td>{html.escape(result.case.description)}</td><td>{result.case.expected}</td><td>{result.observed}</td><td class='{status.lower()}'>{status}</td><td>{result.attempts}</td><td>{html.escape(result.execution_source)}</td></tr>")
    content = f"""<section class="result {'pass' if passed == len(results) else 'fail'}"><h2>15-case verifier benchmark: {passed}/{len(results)} passed</h2>
<p><strong>Summary:</strong> {model_cases} model-generated cases; {len(results) - model_cases} fixed verifier cases; {total_attempts} total attempts.</p>
<p><strong>Execution source:</strong> normal cases use the selected Agent + Lean. Faulty, bypass, and ambiguous cases remain fixed local cases. If the selected Agent is DeepSeek, only the 5 normal cases call the DeepSeek API.</p>
<p>These cases test valid proofs, faulty implementations, proof bypass attempts, and ambiguous requirements.</p></section>
<table class="benchmark"><thead><tr><th>ID</th><th>Group</th><th>Purpose</th><th>Expected</th><th>Observed</th><th>Result</th><th>Attempts</th><th>Execution source</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table><p><a class="button secondary" href="/">Back to prototype</a></p>"""
    return page_shell(content, 4)


def render_benchmark_loading(job_id: str) -> bytes:
    content = f"""<section class="card"><h2>Running 15-case test set...</h2>
<p id="progress-text">Starting the selected Agent and Lean verifier...</p>
<div style="height:18px;background:#e7eaf2;border-radius:99px;overflow:hidden;margin:22px 0"><div id="progress-bar" style="height:100%;width:0%;background:var(--blue);transition:width .3s"></div></div>
<p class="hint">The five normal cases run through the selected Agent. A failed attempt is sent back for repair according to your selected retry policy.</p></section>
<script>
const jobId = {json.dumps(job_id)};
async function poll() {{
  const response = await fetch('/benchmark/status?job=' + encodeURIComponent(jobId));
  const job = await response.json();
  const percent = Math.round(job.completed / job.total * 100);
  document.getElementById('progress-bar').style.width = percent + '%';
  document.getElementById('progress-text').textContent = `${{percent}}% · ${{job.completed}}/${{job.total}} cases · ${{job.attempts}} total attempts · current: ${{job.current_case}} (attempt ${{job.current_attempt}})`;
  if (job.status === 'done') {{ window.location.href = '/benchmark?job=' + encodeURIComponent(jobId); return; }}
  if (job.status === 'error') {{ document.getElementById('progress-text').textContent = 'Benchmark stopped: ' + job.error; return; }}
  setTimeout(poll, 800);
}}
poll();
</script>"""
    return page_shell(content, 4)


def render_verify_loading(job_id: str) -> bytes:
    content = f"""<section class="card"><h2>Verifying the confirmed theorem...</h2>
<p id="verify-step">Theorem confirmed ✓ · preparing the Agent...</p>
<div style="height:18px;background:#e7eaf2;border-radius:99px;overflow:hidden;margin:22px 0"><div id="verify-bar" style="height:100%;width:15%;background:var(--blue);transition:width .3s"></div></div>
<ol><li>Theorem confirmed and locked ✓</li><li>Agent generates the Lean implementation and proof</li><li>Lean checks the code and proof</li><li>If FAIL: return feedback and retry</li></ol></section>
<script>
const jobId = {json.dumps(job_id)};
async function pollVerify() {{
  const response = await fetch('/verify/status?job=' + encodeURIComponent(jobId));
  const job = await response.json();
  const percent = job.status === 'done' ? 100 : Math.min(95, 15 + job.attempts * 28);
  document.getElementById('verify-bar').style.width = percent + '%';
  document.getElementById('verify-step').textContent = `${{job.current_step}} · attempt ${{job.current_attempt}}/${{job.max_attempts === null ? 'until success' : job.max_attempts}}`;
  if (job.status === 'done') {{ window.location.href = '/verify?job=' + encodeURIComponent(jobId); return; }}
  if (job.status === 'error') {{ document.getElementById('verify-step').textContent = 'Verification stopped: ' + job.error; return; }}
  setTimeout(pollVerify, 600);
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
    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self._send(render_start())
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
            mode = query.get("mode", ["demo"])[0]
            if mode == "deepseek":
                attempt_choice = query.get("attempts", ["3"])[0]
                max_attempts = None if attempt_choice == "until_success" else int(attempt_choice)
                self._send(render_benchmark_loading(start_benchmark_job(max_attempts)))
                return
            else:
                results = run_all_cases()
            self._send(render_benchmark(results))
        elif parsed.path == "/verify/status":
            job_id = parse_qs(parsed.query).get("job", [""])[0]
            with PENDING_LOCK:
                job = VERIFY_JOBS.get(job_id)
                if job is None:
                    self.send_error(404)
                    return
                payload = {key: job[key] for key in ("status", "attempts", "current_attempt", "current_step", "max_attempts", "error")}
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
                payload = {key: job[key] for key in ("status", "completed", "total", "attempts", "current_case", "current_attempt", "error")}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path not in {"/formalize", "/verify"}:
            self.send_error(404)
            return
        form = self._read_form()
        if form is None:
            return
        specification = form.get("specification", [""])[0].strip()
        mode = form.get("mode", ["demo"])[0]
        attempt_choice = form.get("attempts", ["3"])[0]
        max_attempts = None if attempt_choice == "until_success" else int(attempt_choice)
        try:
            if self.path == "/formalize":
                clear, reason = assess_specification(specification)
                if not clear:
                    self._send(render_start(specification, mode, reason), 400)
                    return
                agent = DeepSeekAgent() if mode == "deepseek" else DemoAgent()
                contract = agent.formalize(specification)
                token = store_contract(specification, mode, contract, max_attempts)
                self._send(render_confirmation(specification, contract, token))
                return

            if form.get("confirmed", [""])[0] != "yes":
                raise AgentError("You must confirm the formal contract before verification.")
            specification, mode, contract, max_attempts = take_contract(form.get("contract_token", [""])[0])
            self._send(render_verify_loading(start_verify_job(specification, mode, contract, max_attempts)))
        except (AgentError, ValueError) as exc:
            self._send(render_start(specification, mode, str(exc)), 400)
        except Exception as exc:
            self._send(render_start(specification, mode, f"Unexpected error: {exc}"), 500)

    def _read_form(self) -> dict[str, list[str]] | None:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 50_000:
            self.send_error(413)
            return None
        return parse_qs(self.rfile.read(length).decode("utf-8"))

    def _send(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[web] {format % args}")


if __name__ == "__main__":
    address = ("127.0.0.1", int(os.environ.get("PORT", "8765")))
    print(f"Lean verification prototype: http://{address[0]}:{address[1]}")
    ThreadingHTTPServer(address, Handler).serve_forever()
