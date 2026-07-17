#!/usr/bin/env python3
"""Deliver one signed issue event twice with the same GitHub delivery ID."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import subprocess
import urllib.request
import uuid
from pathlib import Path


def gh_json(path: str) -> dict:
    process = subprocess.run(["gh", "api", path], check=True, text=True, capture_output=True)
    return json.loads(process.stdout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("item", help="owner/repository#issue")
    parser.add_argument("--url", default="http://127.0.0.1:8787/webhooks/github")
    parser.add_argument("--secret-file", default="/etc/openhands-symphony/webhook-secret")
    parser.add_argument("--delivery", default="")
    args = parser.parse_args()
    repository, number_text = args.item.rsplit("#", 1)
    number = int(number_text)
    if number < 1 or repository.count("/") != 1:
        raise SystemExit("item must use owner/repository#positive-number")
    payload = {
        "action": "labeled",
        "repository": gh_json(f"repos/{repository}"),
        "issue": gh_json(f"repos/{repository}/issues/{number}"),
        "label": {"name": "agent:ready"},
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    secret = Path(args.secret_file).read_bytes().strip()
    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    delivery = args.delivery or f"symphony-smoke-{uuid.uuid4()}"
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": signature,
    }
    results = []
    for attempt in (1, 2):
        request = urllib.request.Request(args.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
        results.append(result)
        print(json.dumps({"attempt": attempt, "delivery": delivery, "response": result}, sort_keys=True))
    if results[0].get("duplicate") is True or results[1].get("duplicate") is not True:
        raise SystemExit("duplicate delivery was not coalesced as expected")
    if results[0].get("job_id") != results[1].get("job_id") and results[1].get("job_id") is not None:
        raise SystemExit("duplicate delivery did not resolve to the same durable job")


if __name__ == "__main__":
    main()
