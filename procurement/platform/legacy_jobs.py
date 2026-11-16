"""Filesystem job state for the development-only compatibility API."""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from procurement.domain.schemas import EvaluationReport, ExtractedDocument
from procurement.domain.serialization import model_dump
from procurement.domain.text import clean_text
from procurement.evaluation.reporting import simple_report
from procurement.platform.files import write_json_atomic


logger = logging.getLogger("procurement_evaluator")
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
JOBS_ROOT = Path(os.getenv("JOBS_ROOT", "samples/runs/jobs"))
MAX_RUNNING_JOBS = max(1, int(os.getenv("MAX_RUNNING_JOBS", "1")))
JOB_SEMAPHORE = threading.BoundedSemaphore(MAX_RUNNING_JOBS)


def jobs_root_path() -> Path:
    return JOBS_ROOT if JOBS_ROOT.is_absolute() else Path.cwd() / JOBS_ROOT


def jobs_root_writable() -> bool:
    try:
        root = jobs_root_path()
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def artifact_dir_for(job_id: str) -> Path:
    root = jobs_root_path().resolve()
    target = (root / job_id).resolve()
    if target != root and root not in target.parents:
        raise ValueError("unsafe job id")
    return target


def write_json(path: Path, data: Any) -> None:
    write_json_atomic(path, data)


def set_job(job_id: str, **updates: Any) -> None:
    now = time.time()
    with JOBS_LOCK:
        current = JOBS.setdefault(job_id, {"bid_id": job_id})
        current.setdefault("created_at", now)
        current.update(updates)
        if updates.get("status") == "running":
            current.setdefault("started_at", now)
        if updates.get("status") in {"succeeded", "failed"}:
            current["finished_at"] = now
        current["updated_at"] = now
        snapshot = dict(current)
    write_job_status(job_id, snapshot)


def read_job_status(job_id: str) -> dict[str, Any] | None:
    status_path = artifact_dir_for(job_id) / "status.json"
    if not status_path.exists():
        return None
    try:
        job = json.loads(status_path.read_text(encoding="utf-8"))
        if job.get("status") == "succeeded" and not job.get("report"):
            report_path = artifact_dir_for(job_id) / "report.json"
            if report_path.exists():
                job["report"] = json.loads(report_path.read_text(encoding="utf-8"))
        with JOBS_LOCK:
            JOBS[job_id] = job
        return job
    except Exception:
        logger.exception("Failed to read job status for %s", job_id)
        return None


def write_job_status(job_id: str, job: dict[str, Any]) -> None:
    artifact_dir = artifact_dir_for(job_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    write_json(artifact_dir / "status.json", job)
    event = {
        "time": job.get("updated_at", time.time()),
        "status": job.get("status"),
        "error": job.get("error"),
        "artifact_dir": job.get("artifact_dir"),
    }
    with (artifact_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True) + "\n")


def write_job_error(job_id: str, error_id: str, exc: Exception) -> None:
    artifact_dir = artifact_dir_for(job_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    detail = {
        "error_id": error_id,
        "error_type": type(exc).__name__,
        "message": clean_text(str(exc))[:300],
    }
    write_json(artifact_dir / f"error-{error_id}.json", detail)


def write_job_artifacts(
    job_id: str,
    report: EvaluationReport,
    docs: list[ExtractedDocument],
    handoffs: dict[str, Any] | None = None,
) -> Path:
    artifact_dir = artifact_dir_for(job_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    pages = [page for document in docs for page in document.pages]
    write_json(artifact_dir / "report.json", simple_report(report))
    write_json(artifact_dir / "evaluation-detail.json", model_dump(report))
    write_json(artifact_dir / "documents-detail.json", [model_dump(document.record) for document in docs])
    write_json(artifact_dir / "logical-documents-detail.json", [model_dump(item) for item in report.logical_documents])
    write_json(artifact_dir / "page-units.json", [model_dump(page) for page in pages])
    handoff_files = []
    for name, payload in (handoffs or {}).items():
        filename = f"{name}.json"
        write_json(artifact_dir / filename, payload)
        handoff_files.append(filename)
    write_json(
        artifact_dir / "manifest.json",
        {
            "job_id": job_id,
            "files": [
                "report.json",
                "evaluation-detail.json",
                "documents-detail.json",
                "logical-documents-detail.json",
                "page-units.json",
            ]
            + handoff_files,
        },
    )
    return artifact_dir


def mark_stale_jobs() -> None:
    root = jobs_root_path()
    if not root.exists():
        return
    for status_path in root.glob("*/status.json"):
        try:
            job = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("status") not in {"queued", "running"}:
            continue
        job["status"] = "failed"
        job["error"] = "job was interrupted by server restart; please resubmit"
        job["finished_at"] = time.time()
        job["updated_at"] = job["finished_at"]
        job.setdefault("bid_id", status_path.parent.name)
        write_job_status(status_path.parent.name, job)
