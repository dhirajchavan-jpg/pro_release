"""Validate the exported contract and optionally poll existing jobs.

This script never uploads files or starts paid work.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent
EXPECTED_RESULTS = {
    "understanding": ("tender_understanding", "procurement-understanding.v1"),
    "intake": ("bid_classification", "procurement-classification.v1"),
    "evaluation": ("bid_evaluation", "procurement-pipeline-evaluation.v1"),
}


def check_refs(spec: dict) -> None:
    schemas = spec["components"]["schemas"]

    def walk(value):
        if isinstance(value, dict):
            ref = value.get("$ref")
            if ref:
                name = ref.rsplit("/", 1)[-1]
                assert name in schemas, f"unresolved schema reference: {ref}"
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(spec)


def get_job(base_url: str, token: str, job_id: str) -> dict:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/pipeline/jobs/{job_id}",
        headers={"X-API-Key": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"job request failed with HTTP {exc.code}: {exc.read().decode()}") from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.getenv("PROCUREMENT_API_TOKEN", ""))
    parser.add_argument("--job", action="append", default=[], help="Existing completed job ID; may be repeated")
    args = parser.parse_args()

    spec = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    check_refs(spec)
    assert spec["security"] == [{"ApiToken": []}, {"BearerAuth": []}]
    for path in (
        "/v1/pipeline/tenders/understand",
        "/v1/pipeline/bids/classify",
        "/v1/pipeline/bids/evaluate",
        "/v1/pipeline/jobs/{job_id}",
    ):
        assert path in spec["paths"], f"missing path: {path}"

    if args.job and not args.token:
        raise SystemExit("--token or PROCUREMENT_API_TOKEN is required for live job checks")
    for job_id in args.job:
        job = get_job(args.base_url, args.token, job_id)
        assert job["status"] == "complete", f"{job_id} is {job['status']}"
        expected_type, expected_schema = EXPECTED_RESULTS[job["stage"]]
        assert job["result_type"] == expected_type
        assert job["result"]["schema_version"] == expected_schema
        print(f"live {job['stage']} contract passed: {job_id}")

    print("static OpenAPI contract passed")


if __name__ == "__main__":
    main()
