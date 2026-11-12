"""Exercise the ECS-pinned LiteLLM Proxy against a loopback-only mock."""

import json
import re
import signal
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from secrets import token_hex
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
PROXY = "http://127.0.0.1:4000"
ROUTER_RETRIES = 2
# Each plan returns its final status on any extra attempts, which the ledger detects.
CASES = {
    "baseline": ((200,), (), 200, "primary"),
    "retry": ((429, 429, 200), (), 200, "primary"),
    "fallback": ((503, 503, 503), (200,), 200, "backup"),
    "exhaustion": ((503, 503, 503), (503, 503, 503), 503, None),
}


class MockProvider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return

        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        model = payload.get("model")
        plan = self.server.plans.get(model)
        if plan is None:
            model, status = "unknown", 400
        else:
            index = self.server.counts.get(model, 0)
            self.server.counts[model] = index + 1
            status = plan[min(index, len(plan) - 1)]
        self.server.attempts.append({"model": model, "status": status})

        if status == 200:
            body = {
                "id": "chatcmpl-" + model,
                "object": "chat.completion",
                "created": 1,
                "model": model,
                "choices": [{
                    "index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "mock success " + model},
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        else:
            body = {"error": {
                "message": f"injected {status} for {model}",
                "type": {429: "rate_limit_error", 503: "server_error"}.get(status, "invalid_request_error"),
                "code": str(status),
            }}

        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def request(path, key, payload=None, timeout=40):
    data = json.dumps(payload).encode() if payload is not None else None
    query = Request(PROXY + path, data, {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    })
    try:
        response = build_opener(ProxyHandler({})).open(query, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read()
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        return response.status, body


def wait_for_proxy(proxy, key):
    deadline = monotonic() + 90
    while monotonic() < deadline:
        if proxy.poll() is not None:
            raise RuntimeError(f"Proxy exited during startup (code {proxy.returncode})")
        try:
            if request("/health/readiness", key, timeout=1)[0] == 200:
                return
        except OSError:
            pass
        sleep(0.5)
    raise RuntimeError("Proxy readiness timed out")


def run_case(name, scenario, attempts, key):
    primary, backup, expected_status, success_route = scenario
    model = f"{name}-primary"
    expected = [
        {"model": f"{name}-{route}", "status": status}
        for route, sequence in (("primary", primary), ("backup", backup))
        for status in sequence
    ]
    start = len(attempts)
    status, marker, request_error = None, None, None
    try:
        status, body = request("/v1/chat/completions", key, {
            "model": model,
            "messages": [{"role": "user", "content": "Reliability test"}],
            "max_tokens": 8,
        })
        if success_route:
            expected_marker = f"mock success {name}-{success_route}"
            try:
                if body["choices"][0]["message"]["content"] == expected_marker:
                    marker = expected_marker
            except (KeyError, IndexError, TypeError):
                pass
        else:
            error = body.get("error") if isinstance(body, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            if isinstance(message, str):
                for route in ("primary", "backup"):
                    injected = f"injected 503 for {name}-{route}"
                    if injected in message:
                        marker = injected
                        break
    except OSError as error:
        request_error = type(error).__name__

    observed = attempts[start:]
    checks = {
        "http_status": status == expected_status,
        "response_body": marker is not None,
        "upstream_sequence": observed == expected,
    }
    return {
        "name": name,
        "passed": all(checks.values()),
        "checks": checks,
        "expected_http_status": expected_status,
        "http_status": status,
        "response_marker": marker,
        "request_error": request_error,
        "expected_upstream": expected,
        "upstream": observed,
    }


def run_suite():
    with TemporaryDirectory() as temp, HTTPServer(("127.0.0.1", 0), MockProvider) as mock:
        mock.plans = {
            f"{name}-{route}": statuses or (503,)
            for name, (primary, backup, _, _) in CASES.items()
            for route, statuses in (("primary", primary), ("backup", backup))
        }
        mock.counts = {}
        mock.attempts = []
        key = "sk-" + token_hex(32)
        config = Path(temp) / "config.yaml"
        config.write_text(json.dumps({
            "general_settings": {"master_key": key},
            "model_list": [
                {"model_name": model, "litellm_params": {
                    "model": "openai/" + model,
                    "api_base": f"http://127.0.0.1:{mock.server_port}/v1",
                    "api_key": "local-mock-placeholder",
                    "max_retries": 0,
                }}
                for model in mock.plans
            ],
            "litellm_settings": {"request_timeout": 5, "turn_off_message_logging": True},
            "router_settings": {
                "num_retries": ROUTER_RETRIES,
                "max_fallbacks": 1,
                "disable_cooldowns": True,
                "fallbacks": [{f"{name}-primary": [f"{name}-backup"]} for name in CASES],
            },
        }))
        worker = Thread(target=mock.serve_forever, daemon=True)
        worker.start()
        try:
            with subprocess.Popen(
                ["/app/docker/prod_entrypoint.sh", "--config", str(config),
                 "--host", "127.0.0.1", "--port", "4000"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ) as proxy:
                try:
                    wait_for_proxy(proxy, key)
                    return [run_case(name, scenario, mock.attempts, key) for name, scenario in CASES.items()]
                finally:
                    proxy.terminate()
                    try:
                        proxy.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proxy.kill()
        finally:
            mock.shutdown()
            worker.join()


def run_in_container():
    try:
        report = {"cases": run_suite()}
    except RuntimeError as error:
        report = {"cases": [], "error": str(error)}
    except OSError as error:
        report = {"cases": [], "error": type(error).__name__}
    print(json.dumps(report))


def write_reports(output, image, report):
    cases = report["cases"]
    passed = "error" not in report and len(cases) == len(CASES) and all(case["passed"] for case in cases)
    results = {"image": image, "passed": passed, **report}
    evidence = {
        case["name"]: {key: case[key] for key in ("http_status", "response_marker", "upstream")}
        for case in cases
    }
    for name, payload in (("results.json", results), ("evidence.json", evidence)):
        (output / name).write_text(json.dumps(payload, indent=2) + "\n")

    lines = ["# LiteLLM reliability test", "", f"Overall **{'PASS' if passed else 'FAIL'}**", ""]
    if "error" in report:
        lines.append(report["error"])
    for case in cases:
        observed = ", ".join(f"{event['model']} {event['status']}" for event in case["upstream"])
        lines.append(f"- **{'PASS' if case['passed'] else 'FAIL'}** {case['name']} — "
                     f"HTTP {case['http_status']}, upstream [{observed}]")
        failed = [check for check, matched in case["checks"].items() if not matched]
        if failed:
            lines.append(f"  Failed checks: {', '.join(failed)}")
    (output / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[2:]))
    print(f"Reports: {output}")
    return 0 if passed else 1


def main():
    run_id = "litellm-reliability-" + uuid4().hex[:12]
    output = ROOT / ".local" / "reliability" / run_id
    output.mkdir(parents=True)
    image = None

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        dockerfile = (ROOT / "gateway" / "Dockerfile").read_text()
        pinned = re.search(r"^FROM (ghcr\.io/berriai/litellm-non_root:[^\s@]+@sha256:[a-f0-9]{64})$",
                           dockerfile, re.MULTILINE)
        if pinned is None:
            raise RuntimeError("Expected a digest-pinned LiteLLM base image in gateway/Dockerfile")
        image = pinned[1]
        try:
            run = subprocess.run([
                "docker", "run", "--rm", "--init", "--name", run_id,
                "--platform", "linux/amd64", "--network", "none", "--log-driver", "none",
                "--env", "LITELLM_LOCAL_MODEL_COST_MAP=True",
                "--mount", f"type=bind,source={Path(__file__).resolve()},target=/tmp/reliability.py,readonly",
                "--entrypoint", "python", image, "/tmp/reliability.py", "--inside-container",
            ], stdout=subprocess.PIPE, text=True, timeout=300)
            if run.returncode:
                raise RuntimeError(f"Docker run failed (code {run.returncode}). See terminal output.")
            report = json.loads(run.stdout)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            subprocess.run(["docker", "rm", "-f", run_id], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, check=False)
            raise
    except RuntimeError as error:
        report = {"cases": [], "error": str(error)}
    except (OSError, ValueError, KeyboardInterrupt, subprocess.TimeoutExpired) as error:
        report = {"cases": [], "error": f"Reliability run failed ({type(error).__name__})"}

    return write_reports(output, image, report)


if __name__ == "__main__":
    if sys.argv[1:] == ["--inside-container"]:
        run_in_container()
    else:
        sys.exit(main())
