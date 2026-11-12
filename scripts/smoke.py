"""Exercise a deployed gateway through the local SSM tunnel."""

import json
import subprocess
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

LOCAL_GATEWAY = "http://127.0.0.1:4000"
OPENER = build_opener(ProxyHandler({}))


def aws(*args):
    return subprocess.check_output(["aws", *args, "--no-cli-pager", "--output", "text"], text=True).strip()


def request(path, key=None, body=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    try:
        with OPENER.open(Request(LOCAL_GATEWAY + path, data=data, headers=headers), timeout=45) as response:
            return json.load(response)
    except HTTPError as error:
        # An upstream error body can contain credentials or request data.
        error.close()
        raise RuntimeError(f"{path} returned HTTP {error.code}") from None


def main():
    request("/health/readiness")
    try:
        request("/v1/models")
    except RuntimeError as error:
        if str(error) not in {"/v1/models returned HTTP 401", "/v1/models returned HTTP 403"}:
            raise
    else:
        raise RuntimeError("Expected unauthenticated model listing to be rejected")

    secret_arn = aws(
        "cloudformation", "describe-stacks", "--stack-name", "SpeedRunAiGateway",
        "--query", "Stacks[0].Outputs[?OutputKey=='MasterKeySecretArn'].OutputValue | [0]",
    )
    master = "sk-" + json.loads(aws(
        "secretsmanager", "get-secret-value", "--secret-id", secret_arn, "--query", "SecretString",
    ))["key"]

    budget = 0.01
    alias = f"smoke-{uuid4().hex}"
    key = request("/key/generate", master, {
        "key_alias": alias, "models": ["nova-micro"], "max_budget": budget, "duration": "10m",
    })["key"]
    try:
        info = request("/key/info", key)["info"]
        if info["max_budget"] != budget or info["models"] != ["nova-micro"]:
            raise RuntimeError("Stored virtual key does not match its requested model and budget")
        completion = request("/v1/chat/completions", key, {
            "model": "nova-micro",
            "messages": [{"role": "user", "content": "Reply with the word ready."}],
            "max_tokens": 16,
        })
        if not completion["choices"][0]["message"]["content"]:
            raise RuntimeError("Bedrock returned an empty completion")
    finally:
        request("/key/delete", master, {"keys": [key]})

    print("Readiness, authentication, budgeted key, Bedrock completion and key deletion passed.")


if __name__ == "__main__":
    main()
