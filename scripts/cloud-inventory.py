#!/usr/bin/env python3
"""Read provider model metadata. All inference probes must use the SR gateway."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request

import yaml
from boundary import enforce_execution

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--runtime", choices=["kubernetes", "docker"], required=True)
    args = parser.parse_args()
    env = yaml.safe_load((ROOT / "environments" / args.environment / "environment.yaml").read_text())
    if env["runtime"] != args.runtime:
        raise ValueError("environment/runtime mismatch")
    enforce_execution(env)
    models = yaml.safe_load((ROOT / "config/models/catalog.yaml").read_text())["models"]
    urls = sorted({m["base_url"].rstrip("/") for m in models if m["pool"] == "cloud"})
    key = (ROOT / "secrets" / args.environment / "CLOUD_API_KEY").read_text().strip()
    if not key:
        raise ValueError("CLOUD_API_KEY is not configured")
    reports = []
    for url in urls:
        request = urllib.request.Request(url+"/models", headers={"Authorization":"Bearer "+key})
        try:
            response = urllib.request.urlopen(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read(1048577)
            record = {"endpoint":url+"/models", "status":response.status,
                      "body_sha256":hashlib.sha256(body).hexdigest(), "models":[]}
        if response.status == 200 and len(body) <= 1048576:
            document = json.loads(body)
            record["models"] = [{k:entry[k] for k in ["id", "object", "owned_by"] if k in entry}
                                for entry in document.get("data", [])]
        reports.append(record)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "reports" / args.environment / args.runtime / (stamp+"-cloud-inventory")
    output.mkdir(parents=True)
    (output / "report.json").write_text(json.dumps({"inventory_only":True, "inference_tested":False,
        "environment":args.environment,"runtime":args.runtime,"providers":reports}, indent=2)+"\n")
    print(json.dumps({"report":str(output),"statuses":[r["status"] for r in reports],
                      "model_ids":[m.get("id") for r in reports for m in r["models"]]}))


if __name__ == "__main__":
    main()
