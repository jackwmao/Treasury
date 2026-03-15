# AI-Powered Alcohol Label Verification App

Local-inference web application for matching front/back label images to application PDFs and running CFR-based compliance checks.

## Stack

- Frontend: React + TypeScript + Vite
- Backend: FastAPI (Python)
- Label extraction: local VLM client (`backend/app/vlm/client.py`)
  - VLM: DeepSeek-OCR

## Features

- Single verification: upload application PDF with labels affixed.
- Batch verification: process multiple PDF/front/back label sets.
- Cross-document matching: label and application values have to agree.
- Regulatory checks: label must contain information required under regulations in the manner as prescribed.
  - During the current prototype, this is a limited feature.
- `pass | needs_review` outputs with machine-readable finding codes.

## Test files
You can find sample application files in the [`docs/`](docs/) directory.

- [Sample Application 1 (PDF)](docs/sample%20application.pdf)
- [Sample Application 2 (PDF)](docs/sample%20application%202.pdf) - should reject with no labels
- [Sample Application 3 (PDF)](docs/sample%20application%203.pdf)

## Live demo

You may visit [treasury.jackmao.software](https://treasury.jackmao.software) for a live demo. Please note that there may be a 1-2 minute cold start after 3/20. Currently using AWS EC2 to host the server and HuggingFace to host the VLM, but the VLM can be hosted locally (see below for local setup instructions).

## Approach

See [docs/approach.md](docs/approach.md) for full details on architecture and validation logic.

## Project layout

- `frontend/` React UI
- `backend/` FastAPI API and inference/rules
- `docs/approach.md` assumptions, architecture, and trade-offs

## Local run

### Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

## Tests

```bash
cd backend
pytest
```

## Notes

- No cloud API calls are used in this implementation.
- The local VLM module is scaffolded for pluggable on-host model integration.
  - To run on localhost, you need to replace the VLM calls with a service like Ollama.
- Compliance checks are decision support for human reviewers, not legal determinations.