"""Isolated, metered checklist validation; never updates project data."""
import argparse
import json
import os
from decimal import Decimal
from pathlib import Path
import sys
import threading
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--source-file", required=True)
    parser.add_argument("--pages", help="Optional comma-separated page numbers for a targeted diagnostic")
    parser.add_argument("--compliance-fixture", type=Path, help="Contrasting backend-only policy cases")
    parser.add_argument("--review-existing-findings", action="store_true", help="Verify saved findings against the full package without changing them")
    parser.add_argument("--compliance-package", action="store_true", help="Run the real compliance stage on the saved full tender, isolated from the database")
    parser.add_argument("--all-sources", action="store_true", help="Extract the checklist from every saved tender source")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test-model", choices=["gemini-2.5-flash"], help="Process-only model override; never edits the live environment")
    parser.add_argument("--budget-cap-inr", type=Decimal, default=Decimal("1000"), help="Explicitly authorized cumulative validation cap")
    args = parser.parse_args()
    load_dotenv()
    if args.test_model:
        os.environ["AI_CONTEXT_TOKENS"] = "1048576"
        for role in ("FAST", "DEEP", "OCR", "RERANKER", "VERIFIER", "PREMIUM"):
            os.environ["AI_MODEL_" + role] = args.test_model
        for key in list(os.environ):
            if key.startswith("AI_") and key.endswith("CONTEXT_TOKENS"):
                os.environ[key] = "1048576"
            elif key.endswith("INPUT_COST_PER_MILLION_TOKENS"):
                os.environ[key] = "0.30"
            elif key.endswith("OUTPUT_COST_PER_MILLION_TOKENS"):
                os.environ[key] = "2.50"
    if os.getenv("AI_PROVIDER") != "gemini":
        raise RuntimeError("This validation budget guard supports native Gemini only")
    # Isolated process guard below replaces the live tenant guard, not its configuration.
    os.environ["AI_BUDGET_LIMIT_INR"] = ""
    os.environ["AI_RETRY_ATTEMPTS"] = "1"
    from procurement.platform import llm
    from procurement.platform.files import write_json_atomic
    from procurement.platform.provider_usage import start_provider_usage, stop_provider_usage
    from procurement.understanding.source_obligations import extract_source_obligations
    import psycopg
    from procurement.understanding.rendering import render_pdf_page_png_b64

    args.output.mkdir(parents=True, exist_ok=True)
    artifact_prefix = (args.test_model + "-") if args.test_model else ""
    ledger_path = args.output / "budget.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {"reserved_inr": 0, "calls": [], "usage": []}
    lock = threading.Lock()
    input_rate = max(Decimal(os.environ[k]) for k in os.environ if k.endswith("INPUT_COST_PER_MILLION_TOKENS"))
    output_rate = max(Decimal(os.environ[k]) for k in os.environ if k.endswith("OUTPUT_COST_PER_MILLION_TOKENS"))
    fx = Decimal(os.environ["USD_TO_INR_RATE"])
    original = llm.post_gemini_content

    def guarded(api_key, model, body, attempt=1, task="gemini_content"):
        if args.test_model and model != args.test_model:
            raise RuntimeError("Validation attempted a model outside the selected test model")
        if args.test_model and int(body.get("generationConfig", {}).get("maxOutputTokens") or 0) > 65536:
            from procurement.providers.errors import AIContextLimitError
            raise AIContextLimitError("Selected test model supports at most 65536 output tokens; split output targets")
        llm.ensure_context_budget(body, task)
        parts = [p for c in body.get("contents", []) for p in c.get("parts", [])]
        # Deliberately over-reserve: one token per UTF-8 byte, generous image allowance,
        # double configured rates. Never refund failed/uncertain calls.
        inputs = sum(len(p.get("text", "").encode("utf-8")) for p in parts)
        inputs += 16384 * sum("inline_data" in p for p in parts)
        outputs = int(body.get("generationConfig", {}).get("maxOutputTokens") or 65536)
        reserved = (inputs * input_rate + outputs * output_rate) * fx * 2 / 1000000
        with lock:
            total = Decimal(str(ledger["reserved_inr"])) + reserved
            if total > args.budget_cap_inr:
                raise RuntimeError(f"Validation budget reservation would exceed INR {args.budget_cap_inr}")
            ledger["authorized_limit_inr"] = str(args.budget_cap_inr)
            ledger["reserved_inr"] = str(total)
            ledger["calls"].append({"task": task, "reservation_inr": str(reserved), "attempt": attempt})
            write_json_atomic(ledger_path, ledger)
        try:
            return original(api_key, model, body, attempt=attempt, task=task)
        except llm.GeminiOutputTruncated as exc:
            write_json_atomic(args.output / f"truncated-response-{len(ledger['calls'])}.json",
                              {"task": task, "model": model, "partial_text": exc.partial_text})
            raise

    def record(event):
        with lock:
            ledger["usage"].append(json.loads(json.dumps(event, default=str)))
            # Release unused reservation only for this completed, metered call.
            # Uncertain earlier calls retain their full reservation across restarts.
            if (event.get("status") == "succeeded" and event.get("total_tokens")
                    and ledger["calls"] and not ledger["calls"][-1].get("settled_inr")
                    and ledger["calls"][-1]["task"] == event.get("task", "").removesuffix("_vision")):
                inputs = int(event.get("input_tokens") or 0)
                outputs = max(int(event.get("output_tokens") or 0), int(event["total_tokens"]) - inputs)
                charge = (inputs * input_rate + outputs * output_rate) * fx * 2 / 1000000
                reservation = Decimal(ledger["calls"][-1]["reservation_inr"])
                ledger["reserved_inr"] = str(Decimal(ledger["reserved_inr"]) - reservation + charge)
                ledger["calls"][-1]["settled_inr"] = str(charge)
            write_json_atomic(ledger_path, ledger)

    url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        row = conn.execute("select t.understanding_json from tender_versions t join workbench_projects p on p.tender_version_id=t.id where p.id=%s", (args.project,)).fetchone()
        if not row:
            raise ValueError("Unknown project")
        data = row[0]
    # Recover metadata only from the identical saved extraction, never from ID order.
    for page in data.get("extracted_units", []):
        for block in page.get("blocks", []):
            unit = data["source_units"].get(block.get("block_id"))
            if (unit and unit.get("document") == page.get("document_id")
                    and unit.get("page") == page.get("page_number")
                    and unit.get("text") == block.get("text")
                    and type(block.get("reading_order")) is int):
                unit["reading_order"] = block["reading_order"]
    units = {k: v for k, v in data["source_units"].items() if args.all_sources or v.get("source_file") == args.source_file}
    if args.pages:
        selected_pages = {int(page) for page in args.pages.split(",")}
        units = {k: v for k, v in units.items() if int(v.get("page") or 0) in selected_pages}
    if not units:
        raise ValueError("No matching source units")
    pages = {v["page_id"]: v for v in units.values()}

    def image_loader(page_id):
        unit = pages[page_id]
        source = (args.source_root / unit["source_file"]).resolve()
        if not source.is_relative_to(args.source_root.resolve()):
            raise ValueError("Source escaped validation directory")
        return {"mime_type": "image/png", "data": render_pdf_page_png_b64(source, int(unit["page"]) - 1)}

    operation = "source-validation-" + uuid.uuid4().hex
    start_provider_usage(operation, {"project_id": args.project}, record)
    llm.post_gemini_content = guarded
    try:
        if args.compliance_package:
            from procurement.compliance.tender_audit import run_compliance_audit
            result = run_compliance_audit(data, progress=lambda stage, detail: print(
                json.dumps({"stage": stage, **detail}, default=str), flush=True))
            write_json_atomic(args.output / (artifact_prefix + "full-package-compliance.json"), result)
            print(json.dumps({"status": result["status"], "findings": len(result["findings"]),
                              "reserved_inr": ledger["reserved_inr"]}))
            return
        if args.review_existing_findings:
            from procurement.compliance.corpus import load_corpus
            from procurement.compliance.finding_review import review_findings
            saved = data["compliance"]
            findings, decisions = review_findings(saved["findings"], load_corpus(saved["policy_id"]),
                                                  data["source_units"], call=llm.llm_json)
            verification_name = artifact_prefix + "existing-compliance-verification.json"
            write_json_atomic(args.output / verification_name,
                              {"findings": findings, "verification": decisions})
            print(json.dumps({"candidates": len(saved["findings"]), "retained": len(findings),
                              "reserved_inr": ledger["reserved_inr"]}))
            return
        if args.compliance_fixture:
            from procurement.compliance.corpus import load_corpus
            from procurement.compliance.package_review import audit_package
            from procurement.compliance.finding_review import review_findings
            results = []
            for case in json.loads(args.compliance_fixture.read_text(encoding="utf-8")):
                corpus = load_corpus(case["policy_id"])
                case_units = {"case-p1": {"document": "validation-case", "page": 1, "text": case["text"]}}
                reviews = []
                try:
                    candidates, reviews = audit_package(corpus, case_units, call=llm.llm_json,
                        progress=lambda *args: None, on_clause_reviewed=reviews.extend)
                    target_candidates = [item for item in candidates
                                         if item.get("clause_id") in case["clause_ids"]]
                    findings, decisions = review_findings(target_candidates, corpus, case_units, call=llm.llm_json)
                except Exception as exc:
                    results.append({"case": case["name"], "expected_issue": case["expected_issue"],
                                    "status": "incomplete", "passed": False,
                                    "error": str(exc), "reviews": reviews})
                    write_json_atomic(args.output / (artifact_prefix + "package-compliance-contrast.json"), results)
                    raise
                # These are partial policy scenarios, not complete compliant tenders.
                # Check the named obligation while retaining every other finding for inspection.
                target_findings = [item for item in findings if item["clause_id"] in case["clause_ids"]]
                results.append({"case": case["name"], "expected_issue": case["expected_issue"],
                                "status": "completed", "passed": bool(target_findings) == case["expected_issue"], "reviews": reviews,
                                "target_clause_ids": case["clause_ids"],
                                "findings": findings, "verification": decisions})
                write_json_atomic(args.output / (artifact_prefix + "package-compliance-contrast.json"), results)
                print(json.dumps({"case": case["name"], "passed": results[-1]["passed"]}), flush=True)
            return
        result = extract_source_obligations(units, call=llm.llm_json, image_loader=image_loader, amendment_state={})
        suffix = "-pages-" + args.pages.replace(",", "-") if args.pages else ""
        stem = "full-package" if args.all_sources else Path(args.source_file).stem
        write_json_atomic(args.output / (artifact_prefix + stem + suffix + "-checklist.json"), result)
        print(json.dumps({"status": result["status"], "criteria": len(result["items"]), "reserved_inr": ledger["reserved_inr"]}))
    finally:
        llm.post_gemini_content = original
        stop_provider_usage(operation)


if __name__ == "__main__":
    main()
