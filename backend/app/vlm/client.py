from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.schemas import ApplicationEvidence, ComplianceFinding, LabelEvidence, ReviewStatus
from app.vlm.validation import evaluate_vlm_text

logger = logging.getLogger("uvicorn.error")


def _deepseek_ocr_text_prompt() -> str:
    return "Free OCR."


def _vlm_label_compliance_prompt(side: str) -> str:
    _ = side
    return _deepseek_ocr_text_prompt()


def _prepare_image_for_vlm(label_bytes: bytes) -> str:
    """Shared image preprocessing for all VLMs. Uses VLM_IMAGE_* env (shared)."""
    max_side = int(os.getenv("VLM_IMAGE_MAX_SIDE", os.getenv("VLM_MAX_IMAGE_SIDE", "960")))
    jpeg_quality = int(os.getenv("VLM_IMAGE_JPEG_QUALITY", os.getenv("VLM_JPEG_QUALITY", "62")))
    if max_side <= 0:
        return base64.b64encode(label_bytes).decode("ascii")

    try:
        import cv2
        import numpy as np
    except Exception:
        # If decode fails, pass bytes through unmodified.
        return base64.b64encode(label_bytes).decode("ascii")

    try:
        data = np.frombuffer(label_bytes, dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None:
            return base64.b64encode(label_bytes).decode("ascii")

        h, w = image.shape[:2]
        longest = max(h, w)
        if longest > max_side:
            scale = max_side / float(longest)
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

        ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
        if not ok:
            return base64.b64encode(label_bytes).decode("ascii")
        return base64.b64encode(encoded.tobytes()).decode("ascii")
    except Exception:
        return base64.b64encode(label_bytes).decode("ascii")


def _extract_message_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content", "")
    else:
        # Ollama native /api/chat shape: {"message": {"content": "..."}}
        message = payload.get("message", {})
        if not isinstance(message, dict):
            raise RuntimeError("VLM response missing message content.")
        content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
        return "\n".join(text_parts)
    return str(content)


def extract_label_text_with_local_vlm(label_bytes: bytes, *, side: str = "label") -> str:
    """DeepSeek OCR: uses DEEPSEEK_OCR_* env vars (unique to this model)."""
    base_url = os.getenv("DEEPSEEK_OCR_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
    logger.info("DEEPSEEK_OCR_BASE_URL: %s", base_url)
    model = os.getenv("DEEPSEEK_OCR_MODEL", "deepseek-ai/DeepSeek-OCR")
    api_key = os.getenv("DEEPSEEK_OCR_API_KEY", "ollama")
    timeout_seconds = float(os.getenv("DEEPSEEK_OCR_TIMEOUT_SECONDS", "45"))
    max_tokens = int(os.getenv("DEEPSEEK_OCR_MAX_TOKENS", "420"))
    openai_compat = os.getenv("DEEPSEEK_OCR_OPENAI_COMPATIBLE", "").strip().lower() in ("1", "true", "yes")

    logger.info("DeepSeek OCR start side=%s model=%s openai_compat=%s", side, model, openai_compat)
    image_b64 = _prepare_image_for_vlm(label_bytes)
    label_compliance_prompt = _vlm_label_compliance_prompt(side)
    logger.info("VLM label compliance prompt:\n%s", label_compliance_prompt)

    if openai_compat:
        # Hugging Face / OpenAI: POST to .../v1/chat/completions
        url = base_url if base_url.endswith("/v1") else (base_url.rstrip("/") + "/v1")
        url = url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "stream": False,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": label_compliance_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                        },
                    ],
                },
            ],
        }
    else:
        # Ollama: POST to .../api/chat
        url = base_url.removesuffix("/v1") + "/api/chat"
        payload = {
            "model": model,
            "stream": False,
            "options": {"temperature": 0, "num_predict": max_tokens},
            "messages": [
                {
                    "role": "user",
                    "content": label_compliance_prompt,
                    "images": [image_b64],
                }
            ],
        }

    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"VLM HTTP error ({exc.code}): {detail[:240]}") from exc
    except URLError as exc:
        raise RuntimeError(f"VLM network error: {exc.reason}") from exc

    response_json = json.loads(body)
    vlm_text = _extract_message_content(response_json).strip()
    if not vlm_text:
        raise RuntimeError("VLM returned empty compliance content.")
    logger.info("DeepSeek OCR raw output:\n%s", vlm_text)
    return vlm_text


def verify_label_with_local_vlm(
    label_bytes: bytes, application: ApplicationEvidence, side: str = "label"
) -> tuple[LabelEvidence, list[ComplianceFinding], ReviewStatus]:
    vlm_text = extract_label_text_with_local_vlm(label_bytes, side=side)

    label, findings, status = evaluate_vlm_text(application, vlm_text, side=side)
    return label, findings, status
