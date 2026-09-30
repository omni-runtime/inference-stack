#!/usr/bin/env python3
"""Initialize this stack's private credential files and Kubernetes Secret.

Values are read from an explicit dotenv file or reused from private files;
they are never printed or included in process arguments. This is an offline
operator command, not part of the inference request path.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess

import yaml
from boundary import enforce_execution

ROOT = Path(__file__).resolve().parents[1]
KEYS = ("GATEWAY_TOKEN", "MODEL_API_KEY", "CLOUD_API_KEY")


def read_dotenv(path):
    values = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def initialize(args):
    env = yaml.safe_load((ROOT / "environments" / args.environment / "environment.yaml").read_text())
    if env.get("name") != args.environment:
        raise ValueError("environment name must match its selected directory")
    if env["runtime"] != args.runtime:
        raise ValueError("environment/runtime mismatch")
    enforce_execution(env)
    private = ROOT / "secrets" / args.environment
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    values = {key: (private / key).read_text().strip() for key in KEYS if (private / key).exists()}
    original = dict(values)
    if args.source_env:
        source = read_dotenv(args.source_env)
        # Explicit legacy import is migration only; source files are untouched.
        aliases = {"GATEWAY_TOKEN": "ROUTER_API_KEY", "MODEL_API_KEY": "MODEL_API_KEY", "CLOUD_API_KEY": "LLM_API_KEY"}
        for key in KEYS:
            value = source.get(key) or source.get(aliases[key])
            if value:
                values[key] = value
    for key in ["GATEWAY_TOKEN", "MODEL_API_KEY"]:
        values.setdefault(key, secrets.token_urlsafe(32))
    # An empty cloud key is allowed only for explicitly unauthenticated mocks.
    values.setdefault("CLOUD_API_KEY", "")
    for key, value in values.items():
        path = private / key
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(value)
    if values != original:
        revision = ROOT / ".state" / args.environment / "credentials.version"
        revision.parent.mkdir(parents=True, exist_ok=True)
        revision.write_text(secrets.token_hex(16) + "\n")
    if args.runtime == "kubernetes":
        k8s = env["kubernetes"]
        command = ["kubectl", "--kubeconfig", k8s["kubeconfig"], "--context", k8s["context"],
                   "--namespace", k8s["namespace"], "apply", "-f", "-"]
        namespace = {"apiVersion":"v1", "kind":"Namespace", "metadata":{
            "name":k8s["namespace"], "labels":{"app.kubernetes.io/part-of":"inference-stack"}}}
        secret = {"apiVersion":"v1", "kind":"Secret", "metadata":{"name":"inference-credentials",
            "namespace":k8s["namespace"], "labels":{"app.kubernetes.io/part-of":"inference-stack"}},
            "type":"Opaque", "stringData":values}
        for document in [namespace, secret]:
            subprocess.run(command, input=json.dumps(document), text=True, check=True, timeout=60)
    print(f"Credential references initialized for {args.environment}/{args.runtime}; values omitted")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--runtime", choices=["kubernetes", "docker"], required=True)
    parser.add_argument("--source-env", type=Path)
    initialize(parser.parse_args())
