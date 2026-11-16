"""Development-only compatibility API for filesystem evaluation jobs."""

import logging
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile

from ai_provider import AIConfigurationError, settings as ai_settings, validate as validate_ai_settings
from procurement.domain.text import clean_text
from procurement.evaluation.reporting import simple_report
from procurement.platform.ingestion import SUPPORTED_UPLOAD_SUFFIXES
from procurement.platform.job_context import clear_job_deadline, set_job_deadline
from procurement.platform.legacy_jobs import (
    JOBS,
    JOBS_LOCK,
    JOB_SEMAPHORE,
    read_job_status,
    set_job,
    write_job_artifacts,
    write_job_error,
)


router = APIRouter()
logger = logging.getLogger("procurement_evaluator")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))


@router.post("/evaluate")
async def evaluate(
    background_tasks: BackgroundTasks,
    tender: UploadFile = File(...),
    vendor_bid: UploadFile = File(...),
    guidelines: UploadFile | None = File(None),
    guidelines_text: str = Form(""),
) -> dict[str, str]:
    if os.getenv("APP_ENV", "development").strip().lower() == "production":
        raise HTTPException(status_code=410, detail="legacy filesystem evaluation is disabled; use the PostgreSQL-backed /v1 API")
    try:
        validate_ai_settings(ai_settings())
    except AIConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    job_id = uuid.uuid4().hex
    work_dir = Path(tempfile.mkdtemp(prefix=f"procurement-{job_id}-"))
    try:
        tender_path = await save_upload(tender, work_dir / "upload_tender")
        guidelines_file_path = await save_upload(guidelines, work_dir / "upload_guidelines") if guidelines else None
        guidelines_text_path = write_guidelines_text(guidelines_text, work_dir / "upload_guidelines_text")
        guidelines_paths = [path for path in (guidelines_file_path, guidelines_text_path) if path]
        vendor_path = await save_upload(vendor_bid, work_dir / "upload_vendor")
        if not zipfile.is_zipfile(vendor_path):
            raise HTTPException(status_code=400, detail="vendor_bid must be a zip file")
        if not JOB_SEMAPHORE.acquire(blocking=False):
            raise HTTPException(status_code=503, detail="server is busy; retry later")
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise

    set_job(job_id, status="queued", error=None, report=None)
    background_tasks.add_task(run_job, job_id, tender_path, vendor_path, work_dir, guidelines_paths, True)
    return {"bid_id": job_id, "status": "queued", "status_url": f"/jobs/{job_id}"}


@router.get("/jobs/{bid_id}")
def get_job(bid_id: str) -> dict[str, Any]:
    if os.getenv("APP_ENV", "development").strip().lower() == "production":
        raise HTTPException(status_code=410, detail="legacy filesystem jobs are disabled; use /v1/operations")
    validate_bid_id(bid_id)
    with JOBS_LOCK:
        job = JOBS.get(bid_id)
    if not job:
        job = read_job_status(bid_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return public_job_status(job)


async def save_upload(upload: UploadFile, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    filename = Path(upload.filename or "upload.bin").name
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_UPLOAD_SUFFIXES:
        raise HTTPException(status_code=415, detail=f"unsupported upload file type: {suffix or 'none'}")
    path = directory / filename
    total = 0
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    with path.open("wb") as destination:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                destination.close()
                path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail=f"upload exceeds {MAX_UPLOAD_MB} MB")
            destination.write(chunk)
    if total == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"{upload.filename or 'upload'} is empty")
    return path


def write_guidelines_text(guidelines_text: str | None, directory: Path) -> Path | None:
    text = (guidelines_text or "").strip()
    if not text:
        return None
    content = clean_text(text)
    if not content:
        return None
    if len(content.encode("utf-8")) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"guidelines text exceeds {MAX_UPLOAD_MB} MB")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "guidelines-text.txt"
    path.write_text(content, encoding="utf-8")
    return path


def validate_bid_id(bid_id: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{32}", bid_id):
        raise HTTPException(status_code=404, detail="job not found")


def public_job_status(job: dict[str, Any]) -> dict[str, Any]:
    public = dict(job)
    public.pop("artifact_dir", None)
    if job.get("status") == "succeeded":
        public["internal_audit_available"] = True
    return public


def run_job(
    job_id: str,
    tender_path: Path,
    vendor_path: Path,
    work_dir: Path,
    guidelines_paths: list[Path] | None = None,
    release_job_slot: bool = False,
) -> None:
    from procurement.legacy_evaluation import evaluate_submission

    set_job(job_id, status="running")
    set_job_deadline()
    try:
        result, docs, handoffs = evaluate_submission(job_id, tender_path, vendor_path, work_dir, guidelines_paths)
        artifact_dir = write_job_artifacts(job_id, result, docs, handoffs)
        public_report = simple_report(result)
        set_job(
            job_id,
            status="succeeded",
            report=public_report,
            internal_audit_available=True,
        )
    except Exception as exc:
        error_id = uuid.uuid4().hex[:12]
        logger.exception("Evaluation job %s failed with error_id=%s", job_id, error_id)
        write_job_error(job_id, error_id, exc)
        set_job(job_id, status="failed", error=f"evaluation failed; error_id={error_id}", error_id=error_id)
    finally:
        clear_job_deadline()
        if release_job_slot:
            JOB_SEMAPHORE.release()
        shutil.rmtree(work_dir, ignore_errors=True)
