from fastapi import FastAPI, HTTPException, Request, Body
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn
import uuid
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

import main as image


# ---------------------------------------------------------------------------
# Middleware: sanitize raw JSON body BEFORE FastAPI parses it
# ---------------------------------------------------------------------------
class SanitizeJsonBodyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            raw = await request.body()
            try:
                sanitized = self._sanitize(raw.decode("utf-8")).encode("utf-8")
            except Exception:
                sanitized = raw

            async def receive():
                return {"type": "http.request", "body": sanitized, "more_body": False}

            request = Request(request.scope, receive)

        return await call_next(request)

    @staticmethod
    def _sanitize(text: str) -> str:
        result = []
        in_string = False
        escape_next = False
        for ch in text:
            if escape_next:
                result.append(ch)
                escape_next = False
                continue
            if ch == "\\" and in_string:
                result.append(ch)
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                result.append(ch)
                continue
            if in_string:
                if ch == "\n":
                    result.append("\\n")
                elif ch == "\r":
                    result.append("\\r")
                elif ch == "\t":
                    result.append("\\t")
                else:
                    result.append(ch)
            else:
                result.append(ch)
        return "".join(result)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Slide Forge API",
    description="Generate professional PowerPoint presentations using AWS Bedrock (Claude) and Nova Canvas.",
    version="1.1.0"
)

app.add_middleware(SanitizeJsonBodyMiddleware)

# Basic CORS so a React dev server can talk to this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Shared core logic
# ---------------------------------------------------------------------------
def _run_generation(prompt: str):
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    print(f"Generating for: {prompt[:80]}...")
    pptx_data, slides = image.run(prompt.strip())
    filename = f"presentation_{uuid.uuid4().hex[:8]}.pptx"
    return Response(
        content=pptx_data,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Access-Control-Expose-Headers": "Content-Disposition"
        }
    )


# ---------------------------------------------------------------------------
# Pydantic models for React JSON API
# ---------------------------------------------------------------------------
class GenerateRequest(BaseModel):
    text: str
    theme_id: Optional[str] = None


class ThemePayload(BaseModel):
    id: str
    name: Optional[str] = None


class SlidePayload(BaseModel):
    slide_number: int
    content_type: str
    content: Dict[str, Any]
    layout_override: Optional[str] = None


class ExportRequest(BaseModel):
    slides: List[SlidePayload]
    theme: ThemePayload


def _slides_to_payload(slides: List["image.SlideData"]) -> List[Dict[str, Any]]:
    """Convert internal SlideData objects into plain JSON-serializable dicts."""
    out: List[Dict[str, Any]] = []
    for s in slides:
        out.append(
            {
                "slide_number": s.slide_number,
                "content_type": s.content_type.value,
                "content": s.content,
                "layout_override": s.layout_override,
            }
        )
    return out


def _payload_to_slides(items: List[SlidePayload]) -> List["image.SlideData"]:
    slides: List["image.SlideData"] = []
    for item in items:
        try:
            ct = image.ContentType(item.content_type)
        except ValueError:
            ct = image.ContentType.BULLETS
        slides.append(
            image.SlideData(
                slide_number=item.slide_number,
                content_type=ct,
                content=dict(item.content or {}),
                layout_override=item.layout_override,
            )
        )
    return slides
 
 
# ---------------------------------------------------------------------------
# POST /generate-text  - plain text body (Swagger UI + curl + Postman + Python)
# ---------------------------------------------------------------------------
@app.post(
    "/generate-text",
    summary="Generate PPTX - paste plain text directly",
    tags=["Generate"],
    response_class=Response,
    responses={
        200: {
            "description": "Downloadable .pptx file",
            "content": {
                "application/vnd.openxmlformats-officedocument.presentationml.presentation": {}
            }
        }
    }
)
async def generate_pptx_text(
    prompt: str = Body(
        ...,
        media_type="text/plain",
        description="Paste your content as plain text - multiline, bullets all supported. No JSON escaping needed.",
    )
):
    try:
        return _run_generation(prompt)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")


# ---------------------------------------------------------------------------
# /api/generate  - JSON API for React frontend (returns slides + theme, no PPTX)
# ---------------------------------------------------------------------------
@app.post(
    "/api/generate",
    summary="Generate slides JSON for React UI",
    tags=["React"],
)
async def api_generate(req: GenerateRequest):
    try:
        # Pass theme_id to main pipeline
        slides, theme = image.prepare_slides(req.text, provided_theme_id=req.theme_id)
        variants = image.generate_layout_variants(slides, num_variants=4)
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")

    return {
        "slides": _slides_to_payload(slides),
        "variants": [
            _slides_to_payload(v) for v in (variants or [slides])
        ],
        "theme": {
            "id": theme.value,
            "name": theme.value.replace("_", " ").title(),
        },
    }


# ---------------------------------------------------------------------------
# /api/export  - JSON API for React UI (takes edited slides + theme, returns PPTX)
# ---------------------------------------------------------------------------
@app.post(
    "/api/export",
    summary="Compile slides JSON into PPTX",
    tags=["React"],
    response_class=Response,
    responses={
        200: {
            "description": "Downloadable .pptx file",
            "content": {
                "application/vnd.openxmlformats-officedocument.presentationml.presentation": {}
            },
        }
    },
)
async def api_export(req: ExportRequest):
    try:
        try:
            theme_enum = image.Theme(req.theme.id)
        except ValueError:
            theme_enum = image.Theme.MIDNIGHT_EXECUTIVE

        slide_objs = _payload_to_slides(req.slides)
        filename = f"presentation_{uuid.uuid4().hex[:8]}.pptx"
        pptx_bytes = image.export_pptx(slide_objs, theme_enum, filename=filename)
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")

    return Response(
        content=pptx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )
 
 
# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health", summary="Health Check", tags=["System"])
async def health_check():
    return {"status": "healthy", "service": "slide-forge"}
 
 
if __name__ == "__main__":
      uvicorn.run("api:app", host="0.0.0.0", port=4001)
