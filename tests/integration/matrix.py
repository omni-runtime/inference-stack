#!/usr/bin/env python3
"""Run the explicit mock deployment matrix through the offline operator CLI."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from boundary import enforce_execution


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--runtime", choices=["kubernetes", "docker"], required=True)
    parser.add_argument("--examples", nargs="*")
    parser.add_argument("--catalog", default="config/models/catalog.yaml")
    parser.add_argument("--release")
    parser.add_argument("--candidate-test", action="store_true")
    args = parser.parse_args()
    environment = yaml.safe_load((ROOT / "environments" / args.environment / "environment.yaml").read_text())
    if environment["runtime"] != args.runtime:
        raise ValueError("environment/runtime mismatch")
    enforce_execution(environment)
    examples = args.examples or sorted(p.name for p in (ROOT / "examples").iterdir())
    output = ROOT / "reports" / args.environment / args.runtime / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-matrix")
    output.mkdir(parents=True)
    report = {"environment":args.environment, "runtime":args.runtime, "overlay":"mock", "examples":[]}
    for example in examples:
        if not (ROOT / "examples" / example / "example.yaml").is_file():
            raise ValueError("unknown example")
        record = {"example":example, "steps":[], "status":"running"}
        report["examples"].append(record)
        for action in ["check", "deploy", "test"]:
            command = [sys.executable, str(ROOT / "scripts/deploy.py"), action, "--runtime", args.runtime,
                       "--environment", args.environment, "--example", example, "--overlay", "mock", "--catalog", args.catalog]
            if args.release:
                command += ["--release", args.release]
            if args.candidate_test:
                command += ["--candidate-test"]
            print(f"{example}: {action}", flush=True)
            log = output / (example + "-" + action + ".log")
            with log.open("w") as stream:
                try:
                    result = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, timeout=1200)
                    code = result.returncode
                except subprocess.TimeoutExpired:
                    code = 124
            record["steps"].append({"action":action, "command":command, "exit_code":code, "log":log.name})
            if code:
                record["status"] = "failed"
                print(f"{example}: failed at {action}; {log}", flush=True)
                break
        else:
            record["status"] = "passed"
            print(f"{example}: passed", flush=True)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(output, flush=True)
    return int(any(r["status"] != "passed" for r in report["examples"]))


if __name__ == "__main__":
    raise SystemExit(main())
