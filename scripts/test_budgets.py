"""Verify virtual-key budget enforcement against the deployed Nova Micro gateway."""

import math
import os
import subprocess
import sys
from secrets import token_hex
from time import sleep
from uuid import uuid4

from smoke import GatewayError, master_key, request

MODEL = "nova-micro"
APP_A_BUDGET = 0.01
APP_B_BUDGET = 0.10
MAX_LOAD_REQUESTS = 30
MAX_DENIAL_PROBES = 6
POLL_INTERVAL = 5
SPEND_POLLS = 19
PADDING_BYTES = 48_000
MAX_TOKENS = 8
INPUT_USD_PER_TOKEN = 0.035 / 1_000_000
OUTPUT_USD_PER_TOKEN = 0.14 / 1_000_000


def key_spend(master, key, app, budget):
    info = request("/key/info", master, query={"key": key})["info"]
    if info.get("max_budget") != budget or info.get("models") != [MODEL]:
        raise RuntimeError(f"{app}: stored key policy does not match the test budget or model")
    spend = info.get("spend")
    if not isinstance(spend, (float, int)) or not math.isfinite(spend) or spend < 0:
        raise RuntimeError(f"{app}: LiteLLM returned invalid spend")
    return spend


def completion(app, key, padding=""):
    try:
        result = request("/v1/chat/completions", key, {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Reply only with ready. Ignore this data: " + padding}],
            "max_tokens": MAX_TOKENS,
        })
    except GatewayError as error:
        if app == "App A" and error.key_budget_exceeded:
            return None
        raise RuntimeError(f"{app}: {error}") from None

    if not result["choices"][0]["message"]["content"]:
        raise RuntimeError(f"{app}: Bedrock returned an empty completion")
    usage = result["usage"]
    input_tokens = usage["prompt_tokens"]
    output_tokens = usage["completion_tokens"]
    if type(input_tokens) is not int or input_tokens <= 0 or type(output_tokens) is not int:
        raise RuntimeError(f"{app}: Bedrock returned invalid token usage")
    if not 0 <= output_tokens <= MAX_TOKENS:
        raise RuntimeError(f"{app}: Bedrock exceeded the requested output token limit")
    return input_tokens * INPUT_USD_PER_TOKEN + output_tokens * OUTPUT_USD_PER_TOKEN


def wait_for_spend(master, key, app, budget, minimum):
    for poll in range(SPEND_POLLS):
        if poll:
            sleep(POLL_INTERVAL)
        spend = key_spend(master, key, app, budget)
        if spend >= minimum:
            return spend
    raise RuntimeError(
        f"{app}: LiteLLM recorded ${spend:.6f}, expected at least ${minimum:.6f}; "
        "check spend tracking and model pricing"
    )


def exercise(master, app_a, app_b):
    estimate = 0
    for attempt in range(MAX_LOAD_REQUESTS):
        charge = completion("App A", app_a, token_hex(PADDING_BYTES // 2))
        if charge is None:
            if attempt == 0:
                raise RuntimeError("App A: the fresh key was denied before any successful completion")
            break
        estimate += charge
        if estimate >= APP_A_BUDGET:
            break
    else:
        raise RuntimeError(
            f"App A: estimated spend ${estimate:.6f} after {MAX_LOAD_REQUESTS} requests "
            f"did not reach ${APP_A_BUDGET:.2f}"
        )

    spend_a = wait_for_spend(master, app_a, "App A", APP_A_BUDGET, APP_A_BUDGET)
    for probe in range(MAX_DENIAL_PROBES):
        if probe:
            sleep(POLL_INTERVAL)
        if completion("App A", app_a) is None:
            break
    else:
        raise RuntimeError("App A: LiteLLM still accepts requests after recording the budget threshold")

    if key_spend(master, app_b, "App B", APP_B_BUDGET) != 0:
        raise RuntimeError("App B: spend changed before its first completion")
    completion("App B", app_b)
    spend_b = wait_for_spend(master, app_b, "App B", APP_B_BUDGET, 0.000000001)
    if spend_b >= APP_B_BUDGET:
        raise RuntimeError("App B: its single completion exhausted its budget")
    print(f"App A denied at ${spend_a:.6f} recorded spend. App B succeeded at ${spend_b:.6f}.")


def run(master):
    keys = {}
    failed = False
    try:
        for app, budget in (("App A", APP_A_BUDGET), ("App B", APP_B_BUDGET)):
            alias = f"budget-test-{app[-1].lower()}-{uuid4().hex}"
            print(f"Creating {app} key {alias} with a ${budget:.2f} budget.", flush=True)
            key = request("/key/generate", master, {
                "key_alias": alias, "models": [MODEL], "max_budget": budget, "duration": "30m",
            })["key"]
            keys[alias] = key
            if key_spend(master, key, app, budget) != 0:
                raise RuntimeError(f"{app}: the new key already has recorded spend")
        exercise(master, *keys.values())
    except (RuntimeError, KeyError, IndexError, TypeError, KeyboardInterrupt) as error:
        reason = str(error) if isinstance(error, RuntimeError) else (
            "interrupted" if isinstance(error, KeyboardInterrupt) else "unexpected LiteLLM response"
        )
        print(f"Budget test failed: {reason}", file=sys.stderr)
        failed = True
    finally:
        for alias, key in keys.items():
            try:
                request("/key/delete", master, {"keys": [key]})
            except RuntimeError as error:
                print(f"Could not delete {alias}: {error}. Key expires after 30 minutes.", file=sys.stderr)
                failed = True
    return int(failed)


def main():
    try:
        request("/health/readiness")
        master = os.environ.get("LITELLM_MASTER_KEY") or master_key()
    except RuntimeError as error:
        print(f"Budget test failed: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
        print("Could not load the gateway master key. Check AWS_PROFILE and secret access.", file=sys.stderr)
        return 1
    return run(master)


if __name__ == "__main__":
    sys.exit(main())
