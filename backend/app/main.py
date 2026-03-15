from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.matching.reconcile import reconcile_documents
from app.parsing.pdf_fields import (
    ApplicationExtractionRejectedError,
    debug_application_pdf_table,
    extract_pdf_region_as_png,
    parse_application_pdf_async,
    render_pdf_page_for_picker,
)
from app.schemas import (
    ApplicationEvidence,
    BatchResultItem,
    ComplianceFinding,
    LabelEvidence,
    ReviewStatus,
    VerificationResult,
)
from app.vlm.client import extract_label_text_with_local_vlm
from app.vlm.validation import evaluate_vlm_text

# Load backend/.env.local automatically so model endpoints
# are applied without requiring shell exports before starting uvicorn.
load_dotenv(Path(__file__).resolve().parents[1] / ".env.local")

app = FastAPI(title="Local AI Label Verifier", version="0.1.0")
logger = logging.getLogger("uvicorn.error")

VERIFY_LABEL_BBOX = {
    "page": 0,
    "x0": 26.0,
    "y0": 682.0,
    "x1": 586.0,
    "y1": 976.0,
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=600,
)


@app.middleware("http")
async def ensure_localhost_cors(request, call_next):
    response = await call_next(request)
    origin = request.headers.get("origin", "")
    if origin in {
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    }:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers.setdefault(
            "Access-Control-Allow-Methods",
            "GET,POST,PUT,PATCH,DELETE,OPTIONS",
        )
        response.headers.setdefault("Access-Control-Allow-Headers", "*")
        response.headers.setdefault("Vary", "Origin")
    return response


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


async def _extract_label_text(label_bytes: bytes) -> str:
    return await asyncio.to_thread(
        extract_label_text_with_local_vlm,
        label_bytes,
        side="combined",
    )


async def _parse_application(pdf_bytes: bytes) -> ApplicationEvidence:
    return await parse_application_pdf_async(pdf_bytes, use_vlm=False)


def _extract_label_evidence_from_text(
    *,
    application: ApplicationEvidence,
    label_text: str,
) -> tuple[LabelEvidence, list[ComplianceFinding], ReviewStatus]:
    return evaluate_vlm_text(application, label_text, side="combined")


def _merge_review_statuses(*statuses: ReviewStatus) -> ReviewStatus:
    if any(status == ReviewStatus.FAIL for status in statuses):
        return ReviewStatus.FAIL
    if any(status == ReviewStatus.NEEDS_REVIEW for status in statuses):
        return ReviewStatus.NEEDS_REVIEW
    return ReviewStatus.PASS


def _status_from_findings(findings: list[ComplianceFinding], fallback: ReviewStatus) -> ReviewStatus:
    if not findings:
        return fallback
    return _merge_review_statuses(*(item.status for item in findings), fallback)


def _rejected_response(message: str, status_code: int = 422) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"result": "rejected", "message": message})


@app.post("/api/verify", response_model=VerificationResult)
async def verify_single(
    application_pdf: UploadFile = File(...),
) -> VerificationResult:
    if not application_pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="application_pdf must be a PDF.")

    pdf_bytes = await application_pdf.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Empty file upload.")

    try:
        label_image_bytes = await asyncio.to_thread(
            extract_pdf_region_as_png,
            pdf_bytes,
            page_index=VERIFY_LABEL_BBOX["page"],
            x0=VERIFY_LABEL_BBOX["x0"],
            y0=VERIFY_LABEL_BBOX["y0"],
            x1=VERIFY_LABEL_BBOX["x1"],
            y1=VERIFY_LABEL_BBOX["y1"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        parse_task = asyncio.create_task(_parse_application(pdf_bytes))
        ocr_task = asyncio.create_task(_extract_label_text(label_image_bytes))
        parse_result, ocr_result = await asyncio.gather(parse_task, ocr_task, return_exceptions=True)
    except Exception:
        # Defensive guard: gather(return_exceptions=True) should avoid bubbling task errors.
        logger.exception("Unexpected task orchestration error for /api/verify")
        raise HTTPException(status_code=500, detail="Internal orchestration error.")

    if isinstance(parse_result, ApplicationExtractionRejectedError):
        return _rejected_response(str(parse_result))
    if isinstance(parse_result, ValueError):
        raise HTTPException(status_code=400, detail=str(parse_result)) from parse_result
    if isinstance(parse_result, Exception):
        raise HTTPException(status_code=400, detail=str(parse_result)) from parse_result
    application = parse_result

    if isinstance(ocr_result, Exception):
        logger.exception("VLM verification failed for /api/verify")
        raise HTTPException(status_code=502, detail=f"VLM verification failed: {ocr_result}") from ocr_result
    label_text = ocr_result
    label, findings, label_status = _extract_label_evidence_from_text(
        application=application,
        label_text=label_text,
    )
    reconciliation = reconcile_documents(application, label)
    result = VerificationResult(
        status=_status_from_findings(findings, fallback=label_status),
        beverage_type=application.beverage_type,
        findings=findings,
        reconciliation=reconciliation,
        label_evidence=label,
        application_evidence=application,
    )
    return result


@app.post("/api/verify/batch", response_model=List[BatchResultItem])
async def verify_batch(
    application_pdfs: List[UploadFile] = File(...),
    label_images: List[UploadFile] = File(...),
) -> List[BatchResultItem]:
    if len(application_pdfs) != len(label_images):
        raise HTTPException(
            status_code=400,
            detail="application_pdfs and label_images length mismatch.",
        )

    results: List[BatchResultItem] = []
    for app_file, label_file in zip(application_pdfs, label_images):
        app_bytes = await app_file.read()
        label_bytes = await label_file.read()
        if not app_bytes or not label_bytes:
            raise HTTPException(status_code=400, detail=f"Empty file upload in '{app_file.filename}'.")
        try:
            parse_task = asyncio.create_task(_parse_application(app_bytes))
            ocr_task = asyncio.create_task(_extract_label_text(label_bytes))
            parse_result, ocr_result = await asyncio.gather(parse_task, ocr_task, return_exceptions=True)
        except Exception:
            logger.exception("Unexpected task orchestration error for /api/verify/batch item filename=%s", app_file.filename)
            raise HTTPException(status_code=500, detail=f"Internal orchestration error for '{app_file.filename}'.")

        if isinstance(parse_result, ApplicationExtractionRejectedError):
            return _rejected_response(f"{app_file.filename}: {parse_result}")
        if isinstance(parse_result, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid application PDF '{app_file.filename}': {parse_result}",
            ) from parse_result
        if isinstance(parse_result, Exception):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid application PDF '{app_file.filename}': {parse_result}",
            ) from parse_result
        application = parse_result

        if isinstance(ocr_result, Exception):
            logger.exception("VLM verification failed for /api/verify/batch item filename=%s", app_file.filename)
            raise HTTPException(
                status_code=502,
                detail=f"VLM verification failed for '{app_file.filename}': {ocr_result}",
            ) from ocr_result
        label_text = ocr_result
        label, findings, label_status = _extract_label_evidence_from_text(
            application=application,
            label_text=label_text,
        )
        reconciliation = reconcile_documents(application, label)
        result = VerificationResult(
            status=_status_from_findings(findings, fallback=label_status),
            beverage_type=application.beverage_type,
            findings=findings,
            reconciliation=reconciliation,
            label_evidence=label,
            application_evidence=application,
        )
        results.append(BatchResultItem(filename=app_file.filename, result=result))
    return results


@app.post("/api/debug/pdf-table")
async def debug_pdf_table(application_pdf: UploadFile = File(...)) -> dict:
    if not application_pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="application_pdf must be a PDF.")
    pdf_bytes = await application_pdf.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Empty file upload.")
    try:
        return await asyncio.to_thread(debug_application_pdf_table, pdf_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/debug/pdf-picker")
async def debug_pdf_picker(application_pdf: UploadFile = File(...), page: int = Form(0)) -> dict:
    """Render PDF page to image for interactive coordinate picking."""
    if not application_pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="application_pdf must be a PDF.")
    pdf_bytes = await application_pdf.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Empty file upload.")
    return await asyncio.to_thread(render_pdf_page_for_picker, pdf_bytes, page)

