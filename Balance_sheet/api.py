"""FastAPI endpoint for the balance-sheet standardization pipeline.

POST /api/balance-sheet/standardize
  - multipart upload:      -F "file=@filing.pdf"
  - or a path on disk:     -F "pdf_path=C:/path/to/filing.pdf"
                           (also accepted as ?pdf_path=... query parameter)

Run:  uvicorn Balance_sheet.api:app --port 8010
"""

import logging
import os
import tempfile
from typing import Optional

from fastapi import FastAPI, File, Form, Query, UploadFile
from fastapi.responses import JSONResponse

from .pipeline import run_pipeline

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Balance Sheet Standardizer")


@app.post("/api/balance-sheet/standardize")
async def standardize_endpoint(
    file: Optional[UploadFile] = File(None),
    pdf_path: Optional[str] = Form(None),
    pdf_path_query: Optional[str] = Query(None, alias="pdf_path"),
):
    temp_upload = None
    try:
        path = pdf_path or pdf_path_query

        if file is not None:
            fd, temp_upload = tempfile.mkstemp(prefix="bs_upload_", suffix=".pdf")
            with os.fdopen(fd, "wb") as fh:
                fh.write(await file.read())
            path = temp_upload

        if not path:
            return JSONResponse(
                status_code=400,
                content={"error": "Provide a PDF upload ('file') or a 'pdf_path'."},
            )
        if not os.path.isfile(path):
            return JSONResponse(
                status_code=400,
                content={"error": f"PDF not found: {path}"},
            )

        result = run_pipeline(path)
        return JSONResponse(content=result)
    finally:
        if temp_upload and os.path.exists(temp_upload):
            try:
                os.remove(temp_upload)
            except OSError:
                pass
