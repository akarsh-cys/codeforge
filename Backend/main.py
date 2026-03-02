import json
import re
import uuid
import subprocess
import os
import sys
import base64
import textwrap
import boto3
from io import BytesIO
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum


try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    print("Pillow not installed. Run: pip install Pillow")


# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------
AWS_REGION    = os.getenv("AWS_REGION",        "us-east-1")
BEDROCK_MODEL = os.getenv("BEDROCK_MODEL_ID",  "us.anthropic.claude-3-7-sonnet-20250219-v1:0")
NOVA_CANVAS   = "amazon.nova-canvas-v1:0"
OUTPUT_DIR    = os.getenv("OUTPUT_DIR",         "./outputs")
ICONS_DIR     = os.path.join(OUTPUT_DIR, "icons")
PROJECT_DIR   = os.path.dirname(os.path.abspath(__file__))

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(ICONS_DIR,  exist_ok=True)

bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)


# -----------------------------------------------------------------------------
# ENUMS
# ----------------------------------------------------------------------------
class ContentType(str, Enum):
    TITLE        = "title"
    BULLETS      = "bullets"
    TWO_COLUMN   = "two_column"
    STAT_CALLOUT = "stat_callout"
    TIMELINE     = "timeline"
    TABLE        = "table"
    QUOTE        = "quote"
    DIAGRAM      = "diagram"
    THANK_YOU    = "thank_you"


class Theme(str, Enum):
    MIDNIGHT_EXECUTIVE = "midnight_executive"
    CORAL_ENERGY       = "coral_energy"
    OCEAN_GRADIENT     = "ocean_gradient"
    FOREST_MOSS        = "forest_moss"
    CHARCOAL_MINIMAL   = "charcoal_minimal"
    WARM_TERRACOTTA    = "warm_terracotta"


THEME_COLORS = {
    Theme.MIDNIGHT_EXECUTIVE: {"primary": "1E2761", "secondary": "CADCFC", "accent": "FFFFFF"},
    Theme.CORAL_ENERGY:       {"primary": "F96167", "secondary": "F9E795", "accent": "2F3C7E"},
    Theme.OCEAN_GRADIENT:     {"primary": "065A82", "secondary": "1C7293", "accent": "21295C"},
    Theme.FOREST_MOSS:        {"primary": "2C5F2D", "secondary": "97BC62", "accent": "F5F5F5"},
    Theme.CHARCOAL_MINIMAL:   {"primary": "36454F", "secondary": "F2F2F2", "accent": "212121"},
    Theme.WARM_TERRACOTTA:    {"primary": "B85042", "secondary": "E7E8D1", "accent": "A7BEAE"},
}


# -----------------------------------------------------------------------------
# DATA MODELS
# -----------------------------------------------------------------------------
@dataclass
class SlideOutlineItem:
    slide_number: int
    title: str
    content_type: ContentType
    description: str


@dataclass
class FlowStep:
    step_number: int
    label: str          # Short title (  4 words)
    description: str    # One sentence detail shown on diagram
    icon_prompt: str    # Nova Canvas image-gen prompt
    icon_path: str = "" # Filled after generation


@dataclass
class SlideData:
    slide_number: int
    content_type: ContentType
    content: dict
    layout_override: Optional[str] = None


# -----------------------------------------------------------------------------
# BEDROCK   CLAUDE
# -----------------------------------------------------------------------------
def call_bedrock(system: str, user: str, max_tokens: int = 4096) -> str:
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    try:
        response = bedrock.invoke_model(
            modelId=BEDROCK_MODEL,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body),
        )
    except Exception as e:
        msg = str(e)
        if "on-demand throughput isn't supported" in msg or "inference profile" in msg.lower():
            raise RuntimeError(
                f"\n  Invalid Model ID: '{BEDROCK_MODEL}'\n"
                f"  Fix: use a cross-region inference profile, e.g.:\n"
                f"    * us.anthropic.claude-3-7-sonnet-20250219-v1:0\n"
                f"    * us.anthropic.claude-3-5-sonnet-20241022-v2:0\n"
                f"  Docs: https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles.html"
            ) from e
        raise RuntimeError(f"\n  Bedrock call failed: {msg}") from e

    return json.loads(response["body"].read())["content"][0]["text"]


def extract_json(text: str):
    """
    Extract JSON from a Claude response string.
    Handles:
      - Raw JSON (array or object)
      - JSON wrapped in ```json ... ``` fences
      - JSON buried inside prose (finds the first [ or { block)
    Always returns the parsed Python object.
    """
    text = text.strip()

    # 1   Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2   Strip markdown fences
    match = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3   Find the first JSON array [...] or object {...} in the text
    for opener, closer in [("[", "]"), ("{", "}")]:
        start = text.find(opener)
        if start == -1:
            continue
        # Walk from the end to find the matching closer
        end = text.rfind(closer)
        if end == -1 or end <= start:
            continue
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not extract JSON from response:\n{text[:600]}")


def _ensure_list(data) -> list:
    """
    Guarantee we have a plain list of dicts.
    Claude sometimes wraps the array inside an object, e.g.:
      {"slides": [...]}  or  {"outline": [...]}  or  {"steps": [...]}
    This unwraps any such wrapper automatically.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Look for the first value that is a non-empty list
        for v in data.values():
            if isinstance(v, list) and len(v) > 0:
                return v
        # Single-item dict that IS a slide/step   wrap it
        if any(k in data for k in ("slide_number", "step_number", "title")):
            return [data]
    raise ValueError(f"Expected a JSON array, got: {type(data).__name__}   {str(data)[:300]}")


# -----------------------------------------------------------------------------
# NOVA CANVAS   ICON GENERATION
# -----------------------------------------------------------------------------
# Nova Canvas supported image sizes (width x height must be from this list)
_NOVA_CANVAS_SIZES = [
    (1024, 1024), (768, 768), (512, 512),
    (1280, 720),  (1152, 896), (896, 1152),
    (1024, 576),  (576, 1024),
]

def _nearest_nova_size(size: int) -> tuple[int, int]:
    """Return the closest supported square Nova Canvas resolution."""
    squares = [(w, h) for w, h in _NOVA_CANVAS_SIZES if w == h]
    return min(squares, key=lambda s: abs(s[0] - size))


def generate_icon_image(prompt: str, size: int = 512) -> "Image.Image | None":
    """
    Call Nova Canvas to generate a flat-style icon PNG.
    
    Nova Canvas requires:
    - modelId: amazon.nova-canvas-v1:0  (no us. prefix)
    - body: taskType + textToImageParams + imageGenerationConfig
    - width/height must be from a supported list (512x512 is safe)
    - cfgScale range: 1.1   10.0
    
    Returns a PIL Image on success, or None on failure (fallback icons used).
    """
    if not PIL_AVAILABLE:
        return None

    w, h = _nearest_nova_size(size)  # always use a valid Nova Canvas size

    full_prompt = (
        f"Flat style business icon: {prompt}. "
        "White background, single centered icon, bold clean shapes, "
        "vivid solid colors, no text, no shadows, minimalist professional style."
    )

    body = {
        "taskType": "TEXT_IMAGE",
        "textToImageParams": {
            "text": full_prompt,
            "negativeText": "text, letters, words, watermark, complex background, photo, realistic"
        },
        "imageGenerationConfig": {
            "numberOfImages": 1,
            "width":    w,
            "height":   h,
            "quality":  "standard",
            "cfgScale": 8.0,
            "seed":     42,
        },
    }

    try:
        resp = bedrock.invoke_model(
            modelId    = NOVA_CANVAS,
            body       = json.dumps(body),
            accept     = "application/json",
            contentType= "application/json",
        )
        result    = json.loads(resp["body"].read())

        # Nova Canvas returns {"images": ["<base64>"], "error": null}
        if result.get("error"):
            print(f"    [WARN]  Nova Canvas error: {result['error']}")
            return None

        img_bytes = base64.b64decode(result["images"][0])
        img       = Image.open(BytesIO(img_bytes)).convert("RGBA")

        # Resize to requested size if needed
        if img.size != (size, size):
            img = img.resize((size, size), Image.LANCZOS)

        return img

    except Exception as e:
        err = str(e)
        # Print a clean error with actionable hint
        if "ValidationException" in err:
            print(f"    ValidationException for '{prompt[:45]}':")
            print(f"         {err}")
            print(f"         -> Check model ID is 'amazon.nova-canvas-v1:0' and region is us-east-1")
        elif "AccessDeniedException" in err:
            print(f"    Access Denied   enable model access in AWS Console:")
            print(f"         https://console.aws.amazon.com/bedrock/home#/modelaccess")
        else:
            print(f"    Nova Canvas failed for '{prompt[:45]}': {err}")
        return None


def _draw_fallback_icon(path: str, step_number: int) -> str:
    """
    Draw a clean, recognizable fallback icon when Nova Canvas is unavailable.
    Uses simple geometric shapes that look good on a white circle background.
    Each step number maps to a distinct icon shape and color.
    """
    SIZE   = 256
    COLORS = [
        (30, 100, 200),   # 1 - blue       (form/submit)
        (20, 150,  80),   # 2 - green      (verify/check)
        (200,  80,  20),  # 3 - orange     (analysis/chart)
        (120,  20, 160),  # 4 - purple     (decision/lock)
        (20,  150, 150),  # 5 - teal       (agreement)
        (180, 140,  20),  # 6 - gold       (money/disburse)
    ]
    c   = COLORS[(step_number - 1) % len(COLORS)]
    img = Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 255))
    d   = ImageDraw.Draw(img)
    cx, cy = SIZE // 2, SIZE // 2
    m = SIZE // 5

    idx = (step_number - 1) % 6
    if idx == 0:   # Document with lines
        d.rounded_rectangle([cx-m*2, cy-m*2, cx+m*2, cy+m*2], radius=12, fill=c)
        for offset in [-m//2, m//4, m]:
            d.rectangle([cx-m+6, cy+offset-4, cx+m-6, cy+offset+4], fill=(255,255,255))
    elif idx == 1: # Shield with checkmark
        pts = [cx, cy-int(m*2.2), cx+int(m*1.8), cy-m, cx+int(m*1.8), cy+m//2,
               cx, cy+int(m*2.2), cx-int(m*1.8), cy+m//2, cx-int(m*1.8), cy-m]
        d.polygon(pts, fill=c)
        d.line([cx-m, cy+m//4, cx-m//4, cy+m], fill=(255,255,255), width=10)
        d.line([cx-m//4, cy+m, cx+m, cy-m//2], fill=(255,255,255), width=10)
    elif idx == 2: # Bar chart
        bars = [(-int(m*1.5), m//2), (-m//4, -m//2), (int(m*1.1), -m)]
        for bx, top in bars:
            d.rectangle([cx+bx, cy+top, cx+bx+m-4, cy+m], fill=c)
        d.line([cx-int(m*1.7), cy+m+6, cx+int(m*1.7), cy+m+6], fill=(180,180,180), width=5)
    elif idx == 3: # Lock
        d.rounded_rectangle([cx-int(m*1.6), cy, cx+int(m*1.6), cy+int(m*2.2)], radius=12, fill=c)
        d.arc([cx-m, cy-int(m*1.5), cx+m, cy+m//2], start=0, end=180, fill=c, width=14)
        d.ellipse([cx-m//3, cy+m//2, cx+m//3, cy+int(m*1.4)], fill=(255,255,255))
    elif idx == 4: # Dollar circle
        d.ellipse([cx-int(m*1.8), cy-int(m*1.8), cx+int(m*1.8), cy+int(m*1.8)], fill=c)
        d.line([cx, cy-m, cx, cy+m], fill=(255,255,255), width=10)
        d.arc([cx-m+6, cy-m, cx+m-6, cy-m//4], start=180, end=0, fill=(255,255,255), width=8)
        d.arc([cx-m+6, cy-m//4, cx+m-6, cy+m//2], start=0, end=180, fill=(255,255,255), width=8)
    else:          # Arrow (disburse)
        pts = [cx-int(m*2), cy-m//2, cx+m//2, cy-m//2, cx+m//2, cy-m,
               cx+int(m*2), cy, cx+m//2, cy+m, cx+m//2, cy+m//2, cx-int(m*2), cy+m//2]
        d.polygon(pts, fill=c)

    img.save(path, "PNG")
    return path


def generate_all_icons(steps: list[FlowStep]) -> list[FlowStep]:
    """Generate and save one icon PNG per step. Fills step.icon_path."""
    total = len(steps)
    for step in steps:
        fname = f"icon_step{step.step_number:02d}_{uuid.uuid4().hex[:6]}.png"
        path  = os.path.join(ICONS_DIR, fname)

        print(f"    [{step.step_number}/{total}] Icon: {step.label}")
        icon_img = generate_icon_image(step.icon_prompt, size=256)

        if icon_img:
            icon_img.save(path, "PNG")
            step.icon_path = path
        else:
            # Nova Canvas failed   draw a clean geometric fallback icon
            step.icon_path = _draw_fallback_icon(path, step.step_number)

    return steps


def regenerate_step_icon(icon_prompt: str, step_number: int) -> str:
    """Regenerate a single icon for a given prompt. Returns the saved PNG path."""
    fname = f"icon_step{step_number:02d}_{uuid.uuid4().hex[:6]}.png"
    path  = os.path.join(ICONS_DIR, fname)
    print(f"    Regenerating icon for step {step_number}: {icon_prompt[:50]}")
    icon_img = generate_icon_image(icon_prompt, size=256)
    if icon_img:
        icon_img.save(path, "PNG")
        return path
    else:
        return _draw_fallback_icon(path, step_number)


def reassemble_diagram(steps_data: list[dict], title: str, user_query: str = "") -> dict:
    """
    Reassemble all three diagram layout PNGs from edited step data.
    steps_data: list of dicts with keys: step_number, label, description, icon_prompt, icon_path
    Returns: {diagram, diagram_grid, diagram_vertical} paths dict.
    """
    steps = [
        FlowStep(
            step_number=d["step_number"],
            label=d["label"],
            description=d["description"],
            icon_prompt=d.get("icon_prompt", ""),
            icon_path=d.get("icon_path", ""),
        )
        for d in steps_data
    ]
    palette   = _pick_diagram_palette(user_query or title)
    uuid_str  = uuid.uuid4().hex[:8]
    path_horiz = os.path.join(OUTPUT_DIR, f"flow_horiz_{uuid_str}.png")
    path_grid  = os.path.join(OUTPUT_DIR, f"flow_grid_{uuid_str}.png")
    path_vert  = os.path.join(OUTPUT_DIR, f"flow_vert_{uuid_str}.png")

    assemble_flow_diagram_image(steps, title, path_horiz, palette=palette)
    assemble_flow_diagram_grid(steps, title, path_grid, palette=palette)
    assemble_flow_diagram_vertical(steps, title, path_vert, palette=palette)

    return {
        "diagram":          path_horiz.replace("\\", "/"),
        "diagram_grid":     path_grid.replace("\\", "/"),
        "diagram_vertical": path_vert.replace("\\", "/"),
    }


# -----------------------------------------------------------------------------
# FLOW DIAGRAM   PIL ASSEMBLY
# -----------------------------------------------------------------------------

# Color palettes (keyword -> RGB tuples)
_DIAGRAM_PALETTES = {
    "loan|lending|finance|bank|invest|fund|credit": {
        "bg": (13, 17, 39), "card": (30, 39, 97), "card_border": (202, 220, 252),
        "circle_fill": (255, 255, 255), "circle_outline": (202, 220, 252),
        "badge_bg": (202, 220, 252), "badge_text": (30, 39, 97),
        "label_text": (255, 255, 255), "desc_text": (170, 190, 230),
        "arrow": (202, 220, 252), "title_bg": (20, 28, 75), "title_text": (255, 255, 255),
    },
    "health|medical|hospital|patient|care": {
        "bg": (2, 14, 26), "card": (6, 90, 130), "card_border": (156, 205, 207),
        "circle_fill": (255, 255, 255), "circle_outline": (156, 205, 207),
        "badge_bg": (156, 205, 207), "badge_text": (2, 14, 26),
        "label_text": (255, 255, 255), "desc_text": (156, 205, 207),
        "arrow": (156, 205, 207), "title_bg": (4, 60, 90), "title_text": (255, 255, 255),
    },
    "startup|product|launch|growth|innovation": {
        "bg": (26, 10, 11), "card": (180, 40, 50), "card_border": (249, 231, 149),
        "circle_fill": (255, 255, 255), "circle_outline": (249, 231, 149),
        "badge_bg": (249, 231, 149), "badge_text": (100, 10, 15),
        "label_text": (255, 255, 255), "desc_text": (249, 231, 149),
        "arrow": (249, 231, 149), "title_bg": (120, 25, 30), "title_text": (255, 255, 255),
    },
    "nature|green|forest|eco|environment|climate": {
        "bg": (10, 21, 9), "card": (44, 95, 45), "card_border": (151, 188, 98),
        "circle_fill": (255, 255, 255), "circle_outline": (151, 188, 98),
        "badge_bg": (151, 188, 98), "badge_text": (10, 30, 10),
        "label_text": (255, 255, 255), "desc_text": (151, 188, 98),
        "arrow": (151, 188, 98), "title_bg": (25, 60, 26), "title_text": (255, 255, 255),
    },
}

_DEFAULT_PALETTE = {
    "bg": (13, 17, 39), "card": (30, 39, 97), "card_border": (202, 220, 252),
    "circle_fill": (255, 255, 255),     # WHITE so Nova Canvas icons are visible
    "circle_outline": (202, 220, 252),
    "badge_bg": (202, 220, 252), "badge_text": (30, 39, 97),
    "label_text": (255, 255, 255), "desc_text": (170, 190, 230),
    "arrow": (202, 220, 252), "title_bg": (20, 28, 75), "title_text": (255, 255, 255),
}


def _pick_diagram_palette(query: str) -> dict:
    lower = query.lower()
    for keywords, palette in _DIAGRAM_PALETTES.items():
        if any(kw in lower for kw in keywords.split("|")):
            return palette
    return _DEFAULT_PALETTE


def _load_font(size: int, bold: bool = False) -> "ImageFont.FreeTypeFont":
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"    if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_text(text: str, max_chars: int) -> list[str]:
    return textwrap.wrap(text, width=max_chars) or [""]


def _remove_white_bg(img: "Image.Image", threshold: int = 210) -> "Image.Image":
    """
    Make near-white pixels transparent so the icon shows on dark backgrounds.
    Nova Canvas returns icons on white backgrounds   this strips that out.
    Uses numpy if available for speed, falls back to pure PIL.
    """
    img = img.convert("RGBA")
    try:
        import numpy as np
        arr = np.array(img, dtype=np.uint8)
        # Mask: all three channels above threshold -> transparent
        white_mask = (arr[:,:,0] > threshold) & (arr[:,:,1] > threshold) & (arr[:,:,2] > threshold)
        arr[white_mask, 3] = 0
        return Image.fromarray(arr, "RGBA")
    except ImportError:
        pass
    # Pure PIL fallback
    pixels = list(img.getdata())
    pixels = [(r, g, b, 0) if (r > threshold and g > threshold and b > threshold) else (r, g, b, a)
              for r, g, b, a in pixels]
    out = img.copy()
    out.putdata(pixels)
    return out


def _tint_icon(img: "Image.Image", tint_color: tuple, strength: float = 0.55) -> "Image.Image":
    """
    Shift icon colors toward tint_color so it pops against a dark circle.
    strength=0.0 -> original, strength=1.0 -> solid tint.
    """
    img = img.convert("RGBA")
    r0, g0, b0 = tint_color[:3]
    try:
        import numpy as np
        arr = np.array(img, dtype=np.float32)
        alpha = arr[:,:,3:4] / 255.0
        arr[:,:,0] = arr[:,:,0] + (r0 - arr[:,:,0]) * strength * (alpha[:,:,0] > 0)
        arr[:,:,1] = arr[:,:,1] + (g0 - arr[:,:,1]) * strength * (alpha[:,:,0] > 0)
        arr[:,:,2] = arr[:,:,2] + (b0 - arr[:,:,2]) * strength * (alpha[:,:,0] > 0)
        return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")
    except ImportError:
        pass
    # Pure PIL fallback
    pixels = list(img.getdata())
    result = []
    for r, g, b, a in pixels:
        if a == 0:
            result.append((r, g, b, 0))
        else:
            result.append((int(r + (r0-r)*strength), int(g + (g0-g)*strength), int(b + (b0-b)*strength), a))
    out = img.copy()
    out.putdata(result)
    return out


def _circle_crop(img: "Image.Image", size: int) -> "Image.Image":
    img  = img.resize((size, size), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size, size], fill=255)
    out  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def assemble_flow_diagram_image(
    steps: list[FlowStep],
    title: str,
    output_path: str,
    palette: dict | None = None,
) -> str:
    """
    Compose all step icons + text into a single high-resolution PNG
    suitable for embedding directly into a PPTX slide as a full-width image.

    Layout:
     ------------------------------------------------------ 
       TITLE BAR                                            
     ------------------------------------------------------ 
      [icon] -> [icon] -> [icon] -> [icon] -> [icon] -> [icon]  
                                                          
       LABEL    LABEL    LABEL    LABEL    LABEL    LABEL    
       desc     desc     desc     desc     desc     desc     
     ------------------------------------------------------ 
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow is required: pip install Pillow")

    if palette is None:
        palette = _DEFAULT_PALETTE

    # -- Canvas dimensions --------------------------------------------------
    W         = 2400   # 10" @ 240 dpi    matches a 10" PPTX slide width
    TITLE_H   = 110
    PAD_X     = 80
    PAD_TOP   = 55
    ICON_SZ   = 220
    BADGE_R   = 26
    ARROW_W   = 58
    LABEL_H   = 62
    DESC_H    = 88
    PAD_BOT   = 48

    n      = len(steps)
    step_w = (W - 2 * PAD_X - (n - 1) * ARROW_W) // n
    H      = TITLE_H + PAD_TOP + ICON_SZ + 14 + BADGE_R * 2 + 12 + LABEL_H + DESC_H + PAD_BOT

    canvas = Image.new("RGB", (W, H), palette["bg"])
    draw   = ImageDraw.Draw(canvas)

    # -- Title bar ---------------------------------------------------------
    draw.rectangle([0, 0, W, TITLE_H], fill=palette["title_bg"])
    draw.rectangle([0, TITLE_H - 4, W, TITLE_H], fill=palette["card_border"])

    title_font = _load_font(50, bold=True)
    tb         = draw.textbbox((0, 0), title, font=title_font)
    draw.text(
        ((W - (tb[2] - tb[0])) // 2, (TITLE_H - (tb[3] - tb[1])) // 2),
        title, fill=palette["title_text"], font=title_font
    )

    # -- Horizontal connector line (drawn behind icons) --------------------
    line_y   = TITLE_H + PAD_TOP + ICON_SZ // 2
    first_cx = PAD_X + step_w // 2
    last_cx  = PAD_X + (n - 1) * (step_w + ARROW_W) + step_w // 2

    conn_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(conn_layer).rectangle(
        [first_cx, line_y - 3, last_cx, line_y + 3],
        fill=(*palette["card_border"][:3], 60)
    )
    canvas = canvas.convert("RGBA")
    canvas = Image.alpha_composite(canvas, conn_layer)
    canvas = canvas.convert("RGB")
    draw   = ImageDraw.Draw(canvas)

    label_font = _load_font(28, bold=True)
    desc_font  = _load_font(22, bold=False)
    badge_font = _load_font(26, bold=True)

    for i, step in enumerate(steps):
        x0 = PAD_X + i * (step_w + ARROW_W)
        cx = x0 + step_w // 2

        # -- Glow ring -----------------------------------------------------
        glow_r   = ICON_SZ // 2 + 14
        circ_top = TITLE_H + PAD_TOP

        glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(glow_layer).ellipse(
            [cx - glow_r, circ_top - 14, cx + glow_r, circ_top + ICON_SZ + 14],
            fill=(*palette["card_border"][:3], 30)
        )
        canvas = canvas.convert("RGBA")
        canvas = Image.alpha_composite(canvas, glow_layer)
        canvas = canvas.convert("RGB")
        draw   = ImageDraw.Draw(canvas)

        # -- Step circle (WHITE fill   makes Nova Canvas icons visible) ----
        circ_left    = cx - ICON_SZ // 2
        circle_fill  = palette.get("circle_fill",   (255, 255, 255))
        circle_edge  = palette.get("circle_outline", palette["card_border"])
        draw.ellipse(
            [circ_left, circ_top, circ_left + ICON_SZ, circ_top + ICON_SZ],
            fill=circle_fill,
            outline=circle_edge, width=5
        )

        # -- Nova Canvas icon (paste directly onto white circle) -----------
        # Nova Canvas generates colorful icons on WHITE background.
        # Since our circle is also white, we simply paste the icon RGB
        # directly   no alpha stripping needed, no color inversion needed.
        # The icon content sits cleanly on the white circle.
        if step.icon_path and os.path.exists(step.icon_path):
            try:
                raw_icon = Image.open(step.icon_path).convert("RGB")
                # Shrink icon to fit inside circle with padding
                inner_sz = ICON_SZ - 50
                raw_icon = raw_icon.resize((inner_sz, inner_sz), Image.LANCZOS)
                ix = circ_left + (ICON_SZ - inner_sz) // 2
                iy = circ_top  + (ICON_SZ - inner_sz) // 2
                canvas.paste(raw_icon, (ix, iy))
                draw = ImageDraw.Draw(canvas)
                # Redraw circle border on top to keep clean rounded edge
                draw.ellipse(
                    [circ_left, circ_top, circ_left + ICON_SZ, circ_top + ICON_SZ],
                    outline=circle_edge, width=5
                )
            except Exception as ex:
                print(f"    Icon paste error step {step.step_number}: {ex}")

        # -- Step number badge ---------------------------------------------
        badge_y  = circ_top + ICON_SZ + 14
        draw.ellipse(
            [cx - BADGE_R, badge_y, cx + BADGE_R, badge_y + BADGE_R * 2],
            fill=palette["badge_bg"],
            outline=palette["card"], width=2
        )
        num_text = str(step.step_number)
        nb       = draw.textbbox((0, 0), num_text, font=badge_font)
        draw.text(
            (cx - (nb[2] - nb[0]) // 2, badge_y + BADGE_R - (nb[3] - nb[1]) // 2),
            num_text, fill=palette["badge_text"], font=badge_font
        )

        # -- Step label ----------------------------------------------------
        label_y    = badge_y + BADGE_R * 2 + 12
        label_text = step.label.upper()
        label_lines = _wrap_text(label_text, max_chars=14)
        ly = label_y
        for line in label_lines[:2]:
            lb = draw.textbbox((0, 0), line, font=label_font)
            draw.text(
                (cx - (lb[2] - lb[0]) // 2, ly),
                line, fill=palette["label_text"], font=label_font
            )
            ly += lb[3] - lb[1] + 4

        # -- Step description ----------------------------------------------
        desc_y     = label_y + LABEL_H + 4
        desc_lines = _wrap_text(step.description, max_chars=22)
        dy = desc_y
        for line in desc_lines[:4]:
            db = draw.textbbox((0, 0), line, font=desc_font)
            draw.text(
                (cx - (db[2] - db[0]) // 2, dy),
                line, fill=palette["desc_text"], font=desc_font
            )
            dy += db[3] - db[1] + 5

        # -- Arrow -> next step ---------------------------------------------
        if i < n - 1:
            ax  = x0 + step_w + 4
            ay  = TITLE_H + PAD_TOP + ICON_SZ // 2
            # Shaft
            draw.rectangle([ax, ay - 4, ax + ARROW_W - 16, ay + 4], fill=palette["arrow"])
            # Arrowhead
            tip = ax + ARROW_W - 8
            draw.polygon(
                [(ax + ARROW_W - 18, ay - 14), (tip, ay), (ax + ARROW_W - 18, ay + 14)],
                fill=palette["arrow"]
            )

    canvas.save(output_path, "PNG", dpi=(240, 240))
    print(f"    Done! Flow diagram image saved -> {output_path}")
    return output_path

def assemble_flow_diagram_grid(steps: list[FlowStep], title: str, output_path: str, palette: dict | None = None) -> str:
    if not PIL_AVAILABLE: raise RuntimeError("Pillow is required")
    if palette is None: palette = _DEFAULT_PALETTE

    W = 2400
    TITLE_H = 110
    PAD_TOP = 80
    PAD_BOT = 120
    ICON_SZ = 200
    cols = min(3, len(steps))
    rows = (len(steps) + cols - 1) // cols
    CELL_W = W // cols
    CELL_H = 480
    H = TITLE_H + PAD_TOP + rows * CELL_H + PAD_BOT
    
    canvas = Image.new("RGB", (W, H), palette["bg"])
    draw = ImageDraw.Draw(canvas)
    
    draw.rectangle([0, 0, W, TITLE_H], fill=palette["title_bg"])
    draw.rectangle([0, TITLE_H - 4, W, TITLE_H], fill=palette["card_border"])
    title_font = _load_font(50, bold=True)
    tb = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((W - (tb[2] - tb[0])) // 2, (TITLE_H - (tb[3] - tb[1])) // 2), title, fill=palette["title_text"], font=title_font)
    
    label_font = _load_font(30, bold=True)
    desc_font  = _load_font(24, bold=False)
    badge_font = _load_font(26, bold=True)
    
    for i, step in enumerate(steps):
        r = i // cols
        c = i % cols
        cx = c * CELL_W + CELL_W // 2
        cy = TITLE_H + PAD_TOP + r * CELL_H + ICON_SZ // 2
        
        draw.ellipse([cx - ICON_SZ//2, cy - ICON_SZ//2, cx + ICON_SZ//2, cy + ICON_SZ//2], fill=palette.get("circle_fill", (255,255,255)), outline=palette.get("circle_outline", palette["card_border"]), width=5)
        
        if step.icon_path and os.path.exists(step.icon_path):
            try:
                raw_icon = Image.open(step.icon_path).convert("RGB")
                raw_icon = raw_icon.resize((ICON_SZ - 40, ICON_SZ - 40), Image.LANCZOS)
                canvas.paste(raw_icon, (cx - (ICON_SZ - 40)//2, cy - (ICON_SZ - 40)//2))
                draw = ImageDraw.Draw(canvas)
            except: pass
            
        badge_r = 28
        draw.ellipse([cx - badge_r, cy - ICON_SZ//2 - 14, cx + badge_r, cy - ICON_SZ//2 - 14 + badge_r*2], fill=palette["badge_bg"], outline=palette["card"], width=2)
        nd = draw.textbbox((0, 0), str(step.step_number), font=badge_font)
        draw.text((cx - (nd[2]-nd[0])//2, cy - ICON_SZ//2 - 14 + badge_r - (nd[3]-nd[1])//2), str(step.step_number), fill=palette["badge_text"], font=badge_font)
        
        ty = cy + ICON_SZ//2 + 30
        lb = draw.textbbox((0, 0), step.label.upper(), font=label_font)
        draw.text((cx - (lb[2]-lb[0])//2, ty), step.label.upper(), fill=palette["label_text"], font=label_font)
        
        desc_lines = _wrap_text(step.description, max_chars=28)[:3]
        dy = ty + 45
        for line in desc_lines:
            db = draw.textbbox((0, 0), line, font=desc_font)
            draw.text((cx - (db[2]-db[0])//2, dy), line, fill=palette["desc_text"], font=desc_font)
            dy += db[3]-db[1] + 5

    canvas.save(output_path, "PNG", dpi=(240, 240))
    return output_path

def assemble_flow_diagram_vertical(steps: list[FlowStep], title: str, output_path: str, palette: dict | None = None) -> str:
    if not PIL_AVAILABLE: raise RuntimeError()
    if palette is None: palette = _DEFAULT_PALETTE

    W = 2400
    TITLE_H = 110
    PAD_TOP = 80
    PAD_BOT = 100
    ROW_H = 280
    ICON_SZ = 180
    
    H = TITLE_H + PAD_TOP + PAD_BOT + len(steps) * ROW_H
    
    canvas = Image.new("RGB", (W, H), palette["bg"])
    draw = ImageDraw.Draw(canvas)
    
    draw.rectangle([0, 0, W, TITLE_H], fill=palette["title_bg"])
    draw.rectangle([0, TITLE_H - 4, W, TITLE_H], fill=palette["card_border"])
    title_font = _load_font(50, bold=True)
    tb = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((W - (tb[2] - tb[0])) // 2, (TITLE_H - (tb[3] - tb[1])) // 2), title, fill=palette["title_text"], font=title_font)
    
    line_x = 400
    draw.rectangle([line_x - 3, TITLE_H + PAD_TOP, line_x + 3, H - PAD_BOT - ROW_H // 2], fill=(*palette["card_border"][:3], 150))
    
    label_font = _load_font(36, bold=True)
    desc_font  = _load_font(28, bold=False)
    badge_font = _load_font(28, bold=True)
    
    for i, step in enumerate(steps):
        circ_top = TITLE_H + PAD_TOP + i * ROW_H
        cx = line_x
        cy = circ_top + ICON_SZ // 2
        
        draw.ellipse([cx - ICON_SZ//2, circ_top, cx + ICON_SZ//2, circ_top + ICON_SZ], fill=palette.get("circle_fill", (255,255,255)), outline=palette.get("circle_outline", palette["card_border"]), width=5)
        
        if step.icon_path and os.path.exists(step.icon_path):
            try:
                raw_icon = Image.open(step.icon_path).convert("RGB")
                raw_icon = raw_icon.resize((ICON_SZ - 40, ICON_SZ - 40), Image.LANCZOS)
                canvas.paste(raw_icon, (cx - (ICON_SZ - 40)//2, circ_top + 20))
                draw = ImageDraw.Draw(canvas)
            except: pass
            
        badge_r = 30
        draw.ellipse([cx - badge_r, circ_top - 10, cx + badge_r, circ_top - 10 + badge_r*2], fill=palette["badge_bg"], outline=palette["card"], width=2)
        nd = draw.textbbox((0, 0), str(step.step_number), font=badge_font)
        draw.text((cx - (nd[2]-nd[0])//2, circ_top - 10 + badge_r - (nd[3]-nd[1])//2), str(step.step_number), fill=palette["badge_text"], font=badge_font)
        
        tx = cx + ICON_SZ//2 + 80
        ty = circ_top + 40
        draw.text((tx, ty), step.label.upper(), fill=palette["label_text"], font=label_font)
        
        desc_lines = _wrap_text(step.description, max_chars=70)[:2]
        dy = ty + 60
        for line in desc_lines:
            db = draw.textbbox((0, 0), line, font=desc_font)
            draw.text((tx, dy), line, fill=palette["desc_text"], font=desc_font)
            dy += db[3]-db[1] + 8

    canvas.save(output_path, "PNG", dpi=(240, 240))
    return output_path


# -----------------------------------------------------------------------------
# FLOW DIAGRAM   FULL PIPELINE (Claude -> Nova Canvas -> PIL)
# -----------------------------------------------------------------------------
def plan_flow_steps(user_query: str, max_steps: int = 6) -> list[FlowStep]:
    """Ask Claude to plan sequential process steps from the user query."""
    system = (
        "You are an expert process analyst. "
        "Respond with ONLY a valid JSON array   no markdown, no prose."
    )
    user = f"""
Analyze this user query and extract a sequential process flow:

\"\"\"{user_query}\"\"\"

Generate exactly {max_steps} clear, sequential steps.

Return a JSON array where each element is:
{{
  "step_number": <integer 1 to {max_steps}>,
  "label": "<short step title, max 4 words>",
  "description": "<one sentence, max 18 words, describing what happens>",
  "icon_prompt": "<12-18 word flat-style icon image-generation prompt, no text in image>"
}}

Steps must be logical, specific, and in strict sequential order.
"""
    for attempt in range(3):
        try:
            raw  = call_bedrock(system, user, max_tokens=2048)
            data = _ensure_list(extract_json(raw))
            steps = []
            for i, d in enumerate(data):
                if not isinstance(d, dict):
                    continue
                steps.append(FlowStep(
                    step_number = int(d.get("step_number", i + 1)),
                    label       = str(d.get("label", f"Step {i+1}")),
                    description = str(d.get("description", "")),
                    icon_prompt = str(d.get("icon_prompt", f"flat icon representing step {i+1}")),
                ))
            if steps:
                return steps
        except Exception as e:
            print(f"    [WARN]  plan_flow_steps attempt {attempt+1} failed: {e}")
    raise RuntimeError("plan_flow_steps failed after 3 attempts")


def build_flow_diagram_image(
    user_query: str,
    slide_title: str | None = None,
    max_steps: int = 6,
) -> dict:
    """
    Full pipeline:
      1. Claude plans steps
      2. Nova Canvas generates one icon per step
      3. PIL assembles all icons + text into one PNG

    Returns:
      {
        "image_path": "<path to PNG>",
        "steps":      [<FlowStep list>],
        "title":      "<resolved title>",
      }
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow is required: pip install Pillow")

    # 1   Plan steps
    print("\n    Planning flow steps with Claude...")
    steps = plan_flow_steps(user_query, max_steps=max_steps)
    for s in steps:
        print(f"     Step {s.step_number}: {s.label}")

    # 2   Generate icons with Nova Canvas
    print(f"\n  [DESIGN] Generating {len(steps)} icons with Nova Canvas...")
    steps = generate_all_icons(steps)

    # 3   Assemble diagram PNG
    resolved_title = slide_title or _infer_diagram_title(user_query)
    uuid_str       = uuid.uuid4().hex[:8]
    diagram_path_horiz = os.path.join(OUTPUT_DIR, f"flow_horiz_{uuid_str}.png")
    diagram_path_grid  = os.path.join(OUTPUT_DIR, f"flow_grid_{uuid_str}.png")
    diagram_path_vert  = os.path.join(OUTPUT_DIR, f"flow_vert_{uuid_str}.png")
    palette        = _pick_diagram_palette(user_query)

    print(f"\n      Assembling flow diagram images (x3 layouts)...")
    assemble_flow_diagram_image(steps, resolved_title, diagram_path_horiz, palette=palette)
    assemble_flow_diagram_grid(steps, resolved_title, diagram_path_grid, palette=palette)
    assemble_flow_diagram_vertical(steps, resolved_title, diagram_path_vert, palette=palette)

    return {
        "image_path": diagram_path_horiz,
        "image_paths": {
            "diagram": diagram_path_horiz,
            "diagram_grid": diagram_path_grid,
            "diagram_vertical": diagram_path_vert
        },
        "steps":      steps,
        "title":      resolved_title,
    }


def _infer_diagram_title(query: str) -> str:
    lower = query.lower()
    if "loan" in lower and any(w in lower for w in ["disbursal", "disbursement", "disburse"]):
        return "Loan Disbursal: End-to-End Process Flow"
    if "loan" in lower and "approval" in lower:
        return "Loan Approval Process Flow"
    if "loan" in lower or "lending" in lower:
        return "Loan Processing: Step-by-Step Flow"
    if "onboarding" in lower:
        return "Employee / User Onboarding Flow"
    if "hiring" in lower or "recruit" in lower:
        return "Recruitment & Hiring Process"
    if "deploy" in lower:
        return "Software Deployment Pipeline"
    q = query.strip().split("\n")[0].strip()
    return (q[:57] + "...") if len(q) > 60 else q.capitalize()


# -----------------------------------------------------------------------------
# PROCESS TOPIC DETECTOR
# -----------------------------------------------------------------------------
PROCESS_KEYWORDS = [
    "step", "process", "flow", "pipeline", "journey", "lifecycle",
    "stages", "procedure", "workflow", "how", "disbursal", "approval",
    "underwriting", "onboarding", "loan", "hiring", "deployment",
    "involved", "sequence", "phase", "cycle", "steps involved",
]

def is_process_topic(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in PROCESS_KEYWORDS)


# -----------------------------------------------------------------------------
# AUTO MODE DETECTION
# -----------------------------------------------------------------------------
def detect_mode(text: str) -> str:
    lines = [l for l in text.strip().splitlines() if l.strip()]
    words = len(text.split())
    return "topic" if (words <= 40 and len(lines) <= 3) else "content"


# -----------------------------------------------------------------------------
# SLIDE CONTENT JSON SCHEMAS
# -----------------------------------------------------------------------------
_SCHEMAS = {
    ContentType.TITLE: """{
  "title": "...",
  "subtitle": "...",
  "speaker_notes": "..."
}""",
    ContentType.BULLETS: """{
  "title": "...",
  "bullets": ["point 1", "point 2", "point 3", "point 4"],
  "image_suggestion": "short description for a relevant stock photo",
  "speaker_notes": "..."
}""",
    ContentType.TWO_COLUMN: """{
  "title": "...",
  "left_heading": "...",
  "left_bullets": ["...", "..."],
  "right_heading": "...",
  "right_bullets": ["...", "..."],
  "image_suggestion": "...",
  "speaker_notes": "..."
}""",
    ContentType.STAT_CALLOUT: """{
  "title": "...",
  "stats": [
    {"value": "95%", "label": "Customer Satisfaction"},
    {"value": "$4.2B", "label": "Market Size"},
    {"value": "3x",   "label": "YoY Growth"}
  ],
  "body_text": "optional supporting sentence",
  "speaker_notes": "..."
}""",
    ContentType.TIMELINE: """{
  "title": "...",
  "events": [
    {"year": "2020", "label": "Event label", "detail": "brief detail"},
    {"year": "2022", "label": "Event label", "detail": "brief detail"}
  ],
  "speaker_notes": "..."
}""",
    ContentType.TABLE: """{
  "title": "...",
  "headers": ["Column 1", "Column 2", "Column 3"],
  "rows": [
    ["cell", "cell", "cell"],
    ["cell", "cell", "cell"]
  ],
  "speaker_notes": "..."
}""",
    ContentType.QUOTE: """{
  "title": "...",
  "quote": "The actual quote text here",
  "attribution": "  Person Name, Title",
  "speaker_notes": "..."
}""",
    ContentType.THANK_YOU: """{
  "title": "Thank You",
  "message": "closing message",
  "contact": "email or website (optional)",
  "speaker_notes": "..."
}""",
}

_CT_LIST = " | ".join(ct.value for ct in ContentType if ct != ContentType.DIAGRAM)


# -----------------------------------------------------------------------------
# MODE 1   TOPIC
# -----------------------------------------------------------------------------
def outline_from_topic(topic: str, num_slides: int) -> list[SlideOutlineItem]:
    system = "You are Slide Forge AI, an elite presentation architect. Respond with valid JSON only - no markdown, no prose."
    user = f"""
Create a professional presentation outline for the topic: "{topic}".

Generate exactly {num_slides} slides.
- Slide 1 must be type "title".
- Last slide must be type "thank_you".
- Use a variety of content types: {_CT_LIST}

Return a JSON array. Each element:
{{
  "slide_number": <int>,
  "title": "<string>",
  "content_type": "<one of the types above>",
  "description": "<one sentence: what this slide should cover>"
}}
"""
    for attempt in range(3):
        try:
            raw  = call_bedrock(system, user)
            data = extract_json(raw)
            result = _parse_outline(data)
            if result:
                return result
        except Exception as e:
            print(f"    outline_from_topic attempt {attempt+1} failed: {e}")
    raise RuntimeError("outline_from_topic failed after 3 attempts")


def fill_slide_from_topic(topic: str, item: SlideOutlineItem) -> SlideData:
    schema = _SCHEMAS[item.content_type]
    system = "You are Slide Forge AI, a world-class content strategist. Respond with valid JSON only."
    user = f"""
Presentation topic: "{topic}"
Slide {item.slide_number}: "{item.title}"
Content type: {item.content_type.value}
This slide should cover: {item.description}

Return JSON exactly matching this schema:
{schema}

Rules:
- Bullets must be 8-12 words each, punchy and professional.
- You MAY include an `image_suggestion` with a vivid, highly-descriptive prompt for a photorealistic image related to the domain. We want beautiful domain-specific images on visually important slides. If an image is not needed, omit the `image_suggestion` key entirely.
- Do NOT include any text outside the JSON.
"""
    for attempt in range(3):
        try:
            raw  = call_bedrock(system, user)
            data = extract_json(raw)
            # If Claude returned a list, grab the first dict
            if isinstance(data, list) and data:
                data = data[0] if isinstance(data[0], dict) else {}
            if isinstance(data, dict):
                return SlideData(
                    slide_number=item.slide_number,
                    content_type=item.content_type,
                    content=data,
                )
        except Exception as e:
            print(f"    fill_slide_from_topic attempt {attempt+1} failed: {e}")
    # Fallback: return a minimal valid slide so generation continues
    return SlideData(
        slide_number=item.slide_number,
        content_type=item.content_type,
        content={"title": item.title, "bullets": [item.description], "speaker_notes": ""},
    )


# -----------------------------------------------------------------------------
# MODE 2   CONTENT
# -----------------------------------------------------------------------------
def outline_from_content(raw_content: str, num_slides: int) -> list[SlideOutlineItem]:
    system = "You are an expert presentation designer. Respond with valid JSON only   no markdown, no prose."
    user = f"""
The user has provided the following content for a presentation:

\"\"\"
{raw_content}
\"\"\"

Organise this content into exactly {num_slides} slides.
- Slide 1 must be type "title".
- Last slide must be type "thank_you".
- Use a variety of content types: {_CT_LIST}
- ALL slide titles and descriptions must come ONLY from the content above.

Return a JSON array. Each element:
{{
  "slide_number": <int>,
  "title": "<string   derived from the content>",
  "content_type": "<one of the types above>",
  "description": "<one sentence: which part of the provided content this slide covers>"
}}
"""
    for attempt in range(3):
        try:
            raw  = call_bedrock(system, user)
            data = extract_json(raw)
            result = _parse_outline(data)
            if result:
                return result
        except Exception as e:
            with open("error_log.txt", "a", encoding="utf-8") as f:
                f.write(f"Attempt {attempt+1} failed: {str(e)}\n")
            print(f"    outline_from_content attempt {attempt+1} failed: {e}")
    raise RuntimeError("outline_from_content failed after 3 attempts")


def fill_slide_from_content(raw_content: str, item: SlideOutlineItem) -> SlideData:
    schema = _SCHEMAS[item.content_type]
    system = "You are Slide Forge AI, a world-class content strategist. Respond with valid JSON only."
    user = f"""
The user provided this content for the entire presentation:

\"\"\"
{raw_content}
\"\"\"

Fill Slide {item.slide_number}: "{item.title}"
Content type: {item.content_type.value}
This slide covers: {item.description}

STRICT RULE: Use ONLY information from the provided content above.

Return JSON exactly matching this schema:
{schema}

Rules:
- Bullets must be 8-12 words each, extracted/paraphrased from the content.
- You MAY include an `image_suggestion` with a vivid, highly-descriptive prompt for a photorealistic image related to the domain. We want beautiful domain-specific images on visually important slides. If an image is not needed, omit the `image_suggestion` key entirely.
- Do NOT include any text outside the JSON.
"""
    for attempt in range(3):
        try:
            raw  = call_bedrock(system, user)
            data = extract_json(raw)
            if isinstance(data, list) and data:
                data = data[0] if isinstance(data[0], dict) else {}
            if isinstance(data, dict):
                return SlideData(
                    slide_number=item.slide_number,
                    content_type=item.content_type,
                    content=data,
                )
        except Exception as e:
            print(f"    fill_slide_from_content attempt {attempt+1} failed: {e}")
    return SlideData(
        slide_number=item.slide_number,
        content_type=item.content_type,
        content={"title": item.title, "bullets": [item.description], "speaker_notes": ""},
    )


# -----------------------------------------------------------------------------
# SHARED HELPERS
# -----------------------------------------------------------------------------
def _parse_outline(data) -> list[SlideOutlineItem]:
    items = _ensure_list(data)
    result = []
    for d in items:
        if not isinstance(d, dict):
            continue
        ct_raw = str(d.get("content_type", "bullets")).strip().lower()
        try:
            ct = ContentType(ct_raw)
        except ValueError:
            print(f"    Unknown content_type '{ct_raw}'   defaulting to 'bullets'")
            ct = ContentType.BULLETS
        result.append(SlideOutlineItem(
            slide_number = int(d.get("slide_number", len(result) + 1)),
            title        = str(d.get("title", f"Slide {len(result)+1}")),
            content_type = ct,
            description  = str(d.get("description", "")),
        ))
    return result


def generate_slide_image(prompt: str, user_query: str = "") -> str | None:
    if not PIL_AVAILABLE:
        return None
        
    full_prompt = f"Professional presentation photorealistic image: {prompt}. High quality, cinematic lighting."
    if user_query:
        # Give context so domain logic fits (e.g. 'bank' or 'health')
        full_prompt += f" Domain/industry context: {user_query[:100]}. Render appropriately."

    body = {
        "taskType": "TEXT_IMAGE",
        "textToImageParams": {
            "text": full_prompt[:500],
            "negativeText": "text, words, letters, watermark, cartoon, illustration, low quality"
        },
        "imageGenerationConfig": {
            "numberOfImages": 1,
            "width":    1024,
            "height":   1024,
            "quality":  "standard",
            "cfgScale": 7.0,
            "seed":     42,
        },
    }

    try:
        resp = bedrock.invoke_model(
            modelId    = NOVA_CANVAS,
            body       = json.dumps(body),
            accept     = "application/json",
            contentType= "application/json",
        )
        result = json.loads(resp["body"].read())
        if result.get("error"):
            print(f"    [WARN]  Nova Canvas error: {result['error']}")
            return None
            
        img_bytes = base64.b64decode(result["images"][0])
        fname = f"slide_img_{uuid.uuid4().hex[:8]}.png"
        path  = os.path.join(OUTPUT_DIR, fname)
        
        with open(path, "wb") as f:
            f.write(img_bytes)
            
        return path
    except Exception as e:
        print(f"    Slide Image generation failed: {e}")
        return None


def fill_all_slides(outline: list[SlideOutlineItem], mode: str, source: str) -> list[SlideData]:
    slides = []
    total  = len(outline)
    for item in outline:
        print(f"  [{item.slide_number}/{total}] {item.title}  ({item.content_type.value})")
        slide = fill_slide_from_topic(source, item) if mode == "topic" else fill_slide_from_content(source, item)
        
        # Inject realistic image generation if Claude provided a suggestion
        suggestion = slide.content.get("image_suggestion")
        if suggestion:
            print(f"    Generating image: {suggestion}")
            img_path = generate_slide_image(suggestion, user_query=source)
            if img_path:
                print(f"    Generating image: DONE")
                slide.content["generated_image_path"] = img_path.replace("\\", "/")
                
        slides.append(slide)
    return slides


# -----------------------------------------------------------------------------
# PPTXGENJS SCRIPT BUILDER
# -----------------------------------------------------------------------------
def _build_js(slides: list[SlideData], theme: Theme, output_path: str, domain_bg_path: str = None) -> str:
    colors         = json.dumps(THEME_COLORS[theme])
    slides_js      = json.dumps([asdict(s) for s in slides], indent=2)
    output_path_js = output_path.replace("\\", "/")
    bg_js          = f'"{domain_bg_path}"' if domain_bg_path else 'null'

    return f"""
const pptxgen = require("pptxgenjs");
const fs      = require("fs");
const pres    = new pptxgen();
pres.layout   = "LAYOUT_16x9";

const colors = {colors};
const slides = {slides_js};
const domainBg = {bg_js};

// -- Shared title bar -----------------------------------------
function titleBar(slide, title) {{
  slide.addShape(pres.shapes.RECTANGLE, {{
    x: 0, y: 0, w: 10, h: 0.85,
    fill: {{ color: colors.primary }}, line: {{ color: colors.primary }}
  }});
  slide.addText(title, {{
    x: 0.4, y: 0, w: 9.2, h: 0.85,
    fontSize: 22, bold: true, color: "FFFFFF",
    fontFace: "Calibri", valign: "middle", margin: 0
  }});
}}

// -- Slide renderers ------------------------------------------
function renderTitle(slide, c) {{
  slide.background = domainBg ? {{ path: domainBg }} : {{ color: colors.primary }};
  slide.addShape(pres.shapes.RECTANGLE, {{
    x: 0, y: 3.8, w: 10, h: 1.825,
    fill: {{ color: colors.secondary, transparency: 70 }}, line: {{ color: colors.primary }}
  }});
  slide.addText(c.title || "", {{
    x: 0.5, y: 1.1, w: 9, h: 1.8,
    fontSize: 44, bold: true, color: colors.accent,
    fontFace: "Calibri", align: "center", valign: "middle"
  }});
  slide.addText(c.subtitle || "", {{
    x: 0.5, y: 3.0, w: 9, h: 0.7,
    fontSize: 20, color: "FFFFFF",
    fontFace: "Calibri", align: "center"
  }});
}}

function renderTitleLeft(slide, c) {{
  slide.background = domainBg ? {{ path: domainBg }} : {{ color: colors.primary }};
  slide.addShape(pres.shapes.RECTANGLE, {{
    x: 0, y: 0, w: 3, h: 5.625,
    fill: {{ color: colors.secondary, transparency: 20 }}
  }});
  slide.addText(c.title || "", {{
    x: 0.5, y: 1.5, w: 8, h: 1.5,
    fontSize: 44, bold: true, color: colors.accent,
    fontFace: "Calibri", align: "left", valign: "middle"
  }});
  slide.addText(c.subtitle || "", {{
    x: 0.5, y: 3.2, w: 6, h: 1.0,
    fontSize: 20, color: colors.secondary,
    fontFace: "Calibri", align: "left"
  }});
}}

function renderTitleDark(slide, c) {{
  slide.background = {{ color: colors.primary }};
  slide.addShape(pres.shapes.RECTANGLE, {{
    x: 0.5, y: 0.5, w: 9, h: 4.625,
    fill: {{ color: colors.primary }},
    line: {{ color: colors.secondary, width: 3 }},
    rectRadius: 0.2
  }});
  slide.addText(c.title || "", {{
    x: 1.0, y: 1.5, w: 8, h: 2,
    fontSize: 48, bold: true, color: "FFFFFF",
    fontFace: "Georgia", align: "center", valign: "middle"
  }});
  slide.addText(c.subtitle || "", {{
    x: 1.0, y: 3.5, w: 8, h: 1.0,
    fontSize: 22, color: colors.secondary,
    fontFace: "Calibri", align: "center"
  }});
}}

function renderBullets(slide, c) {{
  slide.background = {{ color: "F8F9FA" }};
  titleBar(slide, c.title || "");
  const bullets = (c.bullets || []).map((b, i) => ({{
    text: b,
    options: {{ bullet: true, breakLine: i < (c.bullets.length - 1), fontSize: 16, color: "2D3436", fontFace: "Calibri" }}
  }}));
  
  if (c.generated_image_path) {{
      slide.addText(bullets, {{ x: 0.5, y: 1.1, w: 4.5, h: 4.1 }});
      slide.addImage({{ path: c.generated_image_path, x: 5.3, y: 1.1, w: 4.2, h: 4.2, sizing: {{type: "cover", w: 4.2, h: 4.2}} }});
  }} else {{
      slide.addText(bullets, {{ x: 0.5, y: 1.1, w: 8.5, h: 4.1 }});
      if (c.image_suggestion) {{
        slide.addText("Image: " + c.image_suggestion, {{
          x: 0.5, y: 5.25, w: 9, h: 0.3,
          fontSize: 9, color: "ADB5BD", italic: true, fontFace: "Calibri"
        }});
      }}
  }}
}}

function renderBulletsBox(slide, c) {{
  slide.background = {{ color: colors.secondary }};
  slide.addShape(pres.shapes.RECTANGLE, {{
    x: 0.5, y: 0.5, w: 9, h: 4.625,
    fill: {{ color: "FFFFFF" }},
    line: {{ color: colors.primary, width: 2 }},
    rectRadius: 0.1
  }});
  slide.addText(c.title || "", {{
    x: 0.8, y: 0.6, w: 8.4, h: 0.8,
    fontSize: 26, bold: true, color: colors.primary,
    fontFace: "Calibri", valign: "middle"
  }});
  const bullets = (c.bullets || []).map((b, i) => ({{
    text: b,
    options: {{ bullet: true, breakLine: i < (c.bullets.length - 1), fontSize: 16, color: "2D3436", fontFace: "Calibri" }}
  }}));
  slide.addText(bullets, {{ x: 0.8, y: 1.5, w: 8.4, h: 3.4 }});
}}

function renderTwoColumn(slide, c) {{
  slide.background = {{ color: "F8F9FA" }};
  titleBar(slide, c.title || "");
  const hasImg = !!c.generated_image_path;
  const colW = hasImg ? 3.0 : 4.4;
  const leftX = 0.3;
  const rightX = hasImg ? 3.5 : 5.3;
  const lineX = hasImg ? 3.4 : 4.9;

  slide.addShape(pres.shapes.RECTANGLE, {{
    x: leftX, y: 1.1, w: colW, h: 0.45,
    fill: {{ color: colors.primary }}, line: {{ color: colors.primary }}
  }});
  slide.addText(c.left_heading || "Left", {{
    x: leftX, y: 1.1, w: colW, h: 0.45,
    fontSize: 13, bold: true, color: "FFFFFF", fontFace: "Calibri",
    align: "center", valign: "middle", margin: 0
  }});
  const lb = (c.left_bullets || []).map((b, i) => ({{
    text: b,
    options: {{ bullet: true, breakLine: i < (c.left_bullets.length - 1), fontSize: 13, color: "2D3436", fontFace: "Calibri" }}
  }}));
  slide.addText(lb, {{ x: leftX, y: 1.65, w: colW, h: 3.7 }});

  slide.addShape(pres.shapes.RECTANGLE, {{
    x: rightX, y: 1.1, w: colW, h: 0.45,
    fill: {{ color: colors.secondary }}, line: {{ color: colors.secondary }}
  }});
  slide.addText(c.right_heading || "Right", {{
    x: rightX, y: 1.1, w: colW, h: 0.45,
    fontSize: 13, bold: true, color: colors.primary, fontFace: "Calibri",
    align: "center", valign: "middle", margin: 0
  }});
  const rb = (c.right_bullets || []).map((b, i) => ({{
    text: b,
    options: {{ bullet: true, breakLine: i < (c.right_bullets.length - 1), fontSize: 13, color: "2D3436", fontFace: "Calibri" }}
  }}));
  slide.addText(rb, {{ x: rightX, y: 1.65, w: colW, h: 3.7 }});

  slide.addShape(pres.shapes.LINE, {{
    x: lineX, y: 1.1, w: 0, h: 4.2,
    line: {{ color: colors.primary, width: 1.5 }}
  }});
  
  if (hasImg) {{
      slide.addImage({{ path: c.generated_image_path, x: 6.8, y: 1.1, w: 2.8, h: 4.2, sizing: {{type: "cover", w: 2.8, h: 4.2}} }});
  }}
}}

function renderTwoColumnCards(slide, c) {{
  slide.background = {{ color: "E2E8F0" }};
  titleBar(slide, c.title || "");
  slide.addShape(pres.shapes.RECTANGLE, {{
    x: 0.5, y: 1.1, w: 4.2, h: 4.0,
    fill: {{ color: "FFFFFF" }},
    line: {{ color: "CBD5E1" }},
    rectRadius: 0.1
  }});
  slide.addText(c.left_heading || "Left", {{
    x: 0.5, y: 1.1, w: 4.2, h: 0.6,
    fontSize: 18, bold: true, color: colors.primary, fontFace: "Calibri",
    align: "center", valign: "middle", margin: 0,
    fill: {{ color: colors.secondary, transparency: 80 }}
  }});
  const lb = (c.left_bullets || []).map((b, i) => ({{
    text: b,
    options: {{ bullet: true, breakLine: i < (c.left_bullets.length - 1), fontSize: 13, color: "1E293B", fontFace: "Calibri" }}
  }}));
  slide.addText(lb, {{ x: 0.6, y: 1.8, w: 4.0, h: 3.2 }});

  slide.addShape(pres.shapes.RECTANGLE, {{
    x: 5.1, y: 1.1, w: 4.2, h: 4.0,
    fill: {{ color: "FFFFFF" }},
    line: {{ color: "CBD5E1" }},
    rectRadius: 0.1
  }});
  slide.addText(c.right_heading || "Right", {{
    x: 5.1, y: 1.1, w: 4.2, h: 0.6,
    fontSize: 18, bold: true, color: colors.primary, fontFace: "Calibri",
    align: "center", valign: "middle", margin: 0,
    fill: {{ color: colors.secondary, transparency: 80 }}
  }});
  const rb = (c.right_bullets || []).map((b, i) => ({{
    text: b,
    options: {{ bullet: true, breakLine: i < (c.right_bullets.length - 1), fontSize: 13, color: "1E293B", fontFace: "Calibri" }}
  }}));
  slide.addText(rb, {{ x: 5.2, y: 1.8, w: 4.0, h: 3.2 }});
}}

function renderStatCallout(slide, c) {{
  slide.background = {{ color: colors.primary }};
  titleBar(slide, c.title || "");
  const stats = (c.stats || []).slice(0, 4);
  const count = stats.length || 1;
  const colW  = 9 / count;
  stats.forEach((s, i) => {{
    const x = 0.5 + i * colW;
    slide.addShape(pres.shapes.RECTANGLE, {{
      x, y: 1.3, w: colW - 0.3, h: 2.8,
      fill: {{ color: "FFFFFF", transparency: 10 }}, line: {{ color: colors.secondary }}
    }});
    slide.addText(s.value || "", {{
      x, y: 1.5, w: colW - 0.3, h: 1.4,
      fontSize: 48, bold: true, color: colors.secondary,
      fontFace: "Calibri", align: "center"
    }});
    slide.addText(s.label || "", {{
      x, y: 3.0, w: colW - 0.3, h: 0.8,
      fontSize: 14, color: "FFFFFF", fontFace: "Calibri", align: "center"
    }});
  }});
  if (c.body_text) {{
    slide.addText(c.body_text, {{
      x: 0.5, y: 4.5, w: 9, h: 0.7,
      fontSize: 14, color: colors.secondary,
      fontFace: "Calibri", align: "center", italic: true
    }});
  }}
}}

function renderTimeline(slide, c) {{
  slide.background = {{ color: "F8F9FA" }};
  titleBar(slide, c.title || "");
  const events = (c.events || []).slice(0, 6);
  const count  = events.length || 1;
  const stepW  = 9 / count;
  slide.addShape(pres.shapes.RECTANGLE, {{
    x: 0.5, y: 2.8, w: 9, h: 0.12,
    fill: {{ color: colors.primary }}, line: {{ color: colors.primary }}
  }});
  events.forEach((e, i) => {{
    const cx = 0.5 + i * stepW + stepW / 2;
    slide.addShape(pres.shapes.OVAL, {{
      x: cx - 0.18, y: 2.67, w: 0.36, h: 0.36,
      fill: {{ color: colors.primary }}, line: {{ color: "FFFFFF" }}
    }});
    slide.addText(e.year || "", {{
      x: cx - 0.6, y: 1.8, w: 1.2, h: 0.5,
      fontSize: 14, bold: true, color: colors.primary,
      fontFace: "Calibri", align: "center"
    }});
    slide.addText(e.label || "", {{
      x: cx - 0.7, y: 3.15, w: 1.4, h: 0.45,
      fontSize: 12, bold: true, color: "2D3436",
      fontFace: "Calibri", align: "center"
    }});
    slide.addText(e.detail || "", {{
      x: cx - 0.7, y: 3.7, w: 1.4, h: 1.5,
      fontSize: 10, color: "636E72",
      fontFace: "Calibri", align: "center"
    }});
  }});
}}

function renderTable(slide, c) {{
  slide.background = {{ color: "F8F9FA" }};
  titleBar(slide, c.title || "");
  const headers = (c.headers || []).map(h => ({{
    text: h,
    options: {{ bold: true, color: "FFFFFF", fill: {{ color: colors.primary }}, align: "center" }}
  }}));
  const rows = [headers, ...(c.rows || []).map(row =>
    row.map(cell => ({{ text: String(cell), options: {{ fontSize: 13, color: "2D3436" }} }}))
  )];
  slide.addTable(rows, {{
    x: 0.4, y: 1.1, w: 9.2,
    border: {{ pt: 1, color: "DEE2E6" }},
    fontSize: 13, fontFace: "Calibri"
  }});
}}

function renderQuote(slide, c) {{
  slide.background = {{ color: colors.primary }};
  titleBar(slide, c.title || "");
  slide.addShape(pres.shapes.RECTANGLE, {{
    x: 0.4, y: 1.1, w: 0.12, h: 4.0,
    fill: {{ color: colors.secondary }}, line: {{ color: colors.secondary }}
  }});
  slide.addText(c.quote || "", {{
    x: 0.8, y: 1.5, w: 8.5, h: 2.8,
    fontSize: 22, italic: true, color: "FFFFFF",
    fontFace: "Georgia", valign: "middle"
  }});
  slide.addText(c.attribution || "", {{
    x: 0.8, y: 4.5, w: 8.5, h: 0.6,
    fontSize: 14, color: colors.secondary,
    fontFace: "Calibri", align: "right"
  }});
}}

function renderThankYou(slide, c) {{
  slide.background = domainBg ? {{ path: domainBg }} : {{ color: colors.primary }};
  slide.addShape(pres.shapes.RECTANGLE, {{
    x: 2, y: 1.5, w: 6, h: 0.1,
    fill: {{ color: colors.secondary }}, line: {{ color: colors.secondary }}
  }});
  slide.addText(c.title || "Thank You", {{
    x: 0.5, y: 1.8, w: 9, h: 1.5,
    fontSize: 48, bold: true, color: "FFFFFF",
    fontFace: "Calibri", align: "center"
  }});
  slide.addText(c.message || "", {{
    x: 0.5, y: 3.4, w: 9, h: 1.0,
    fontSize: 18, color: colors.secondary,
    fontFace: "Calibri", align: "center"
  }});
  if (c.contact) {{
    slide.addText(c.contact, {{
      x: 0.5, y: 4.5, w: 9, h: 0.6,
      fontSize: 14, color: colors.secondary,
      fontFace: "Calibri", align: "center", italic: true
    }});
  }}
}}

// -- DIAGRAM renderer ---------------------------------------------------------
// Renders an EDITABLE flow diagram using shapes + icons (no flattened PNG),
// so that all labels and positions can be changed inside PowerPoint.
// -----------------------------------------------------------------------------
function renderDiagram(slide, c) {{
  slide.background = {{ color: "0D1117" }};

  // Title bar
  slide.addShape(pres.shapes.RECTANGLE, {{
    x: 0, y: 0, w: 10, h: 0.75,
    fill: {{ color: colors.primary }},
    line: {{ color: colors.primary }}
  }});
  slide.addText(c.title || "Process Flow", {{
    x: 0.3, y: 0, w: 9.4, h: 0.75,
    fontSize: 22, bold: true, color: "FFFFFF",
    fontFace: "Calibri", valign: "middle"
  }});

  const steps = (c.steps || []).slice(0, 6);
  if (!steps.length) {{
    slide.addText("No steps defined for diagram.", {{
      x: 0.5, y: 2.5, w: 9, h: 1.5,
      fontSize: 14, color: "CADCFC",
      fontFace: "Calibri", align: "center", italic: true
    }});
    return;
  }}

  const n = steps.length;
  const leftPad = 0.7;
  const rightPad = 0.7;
  const usableW = 10 - leftPad - rightPad;
  const trackY = 2.3;

  // Connector line
  slide.addShape(pres.shapes.RECTANGLE, {{
    x: leftPad,
    y: trackY,
    w: usableW,
    h: 0.08,
    fill: {{ color: colors.secondary }},
    line: {{ color: colors.secondary }}
  }});

  for (let i = 0; i < n; i++) {{
    const s = steps[i];
    const cx = leftPad + (usableW / Math.max(1, n - 1)) * i;
    const circleY = 1.1;

    // Circle for icon
    slide.addShape(pres.shapes.OVAL, {{
      x: cx - 0.7,
      y: circleY,
      w: 1.4,
      h: 1.4,
      fill: {{ color: "FFFFFF" }},
      line: {{ color: colors.secondary, width: 1.5 }}
    }});

    // Optional icon image
    if (s.icon_path) {{
      slide.addImage({{
        path: s.icon_path,
        x: cx - 0.55,
        y: circleY + 0.15,
        w: 1.1,
        h: 1.1,
        sizing: {{ type: "contain", w: 1.1, h: 1.1 }}
      }});
    }}

    // Step number badge
    slide.addShape(pres.shapes.OVAL, {{
      x: cx - 0.35,
      y: circleY + 1.4,
      w: 0.7,
      h: 0.7,
      fill: {{ color: colors.secondary }},
      line: {{ color: colors.primary, width: 1 }}
    }});
    slide.addText(String(s.step_number || i + 1), {{
      x: cx - 0.35,
      y: circleY + 1.4,
      w: 0.7,
      h: 0.7,
      fontSize: 12,
      bold: true,
      color: colors.primary,
      fontFace: "Calibri",
      align: "center",
      valign: "middle"
    }});

    // Label
    const labelFontSize = n > 5 ? 9 : (n > 4 ? 10 : 12);
    slide.addText((s.label || "").toUpperCase(), {{
      x: cx - 1.1,
      y: circleY + 2.1,
      w: 2.2,
      h: 0.5,
      fontSize: labelFontSize,
      bold: true,
      color: "FFFFFF",
      fontFace: "Calibri",
      align: "center"
    }});

    // Description
    const descFontSize = n > 4 ? 8 : 10;
    slide.addText(s.description || "", {{
      x: cx - 1.0,
      y: circleY + 2.6,
      w: 2.0,
      h: 0.8,
      fontSize: descFontSize,
      color: "E5E7EB",
      fontFace: "Calibri",
      align: "center"
    }});

    // Arrow to next step
    if (i < n - 1) {{
      slide.addShape(pres.shapes.LINE, {{
        x: cx + 0.9,
        y: trackY + 0.04,
        w: (usableW / Math.max(1, n - 1)) - 1.0,
        h: 0,
        line: {{ color: colors.secondary, width: 1.5, endArrowType: "triangle" }}
      }});
    }}
  }}
}}

// -- Router ---------------------------------------------------
const RENDERERS = {{
  title:             renderTitle,
  title_left:        renderTitleLeft,
  title_dark:        renderTitleDark,
  bullets:           renderBullets,
  bullets_box:       renderBulletsBox,
  two_column:        renderTwoColumn,
  two_column_cards:  renderTwoColumnCards,
  stat_callout:      renderStatCallout,
  timeline:          renderTimeline,
  table:             renderTable,
  quote:             renderQuote,
  diagram:           renderDiagram,
  thank_you:         renderThankYou,
}};

slides.forEach(sd => {{
  const slide = pres.addSlide();
  const fn    = RENDERERS[sd.layout_override] || RENDERERS[sd.content_type] || renderBullets;
  fn(slide, sd.content);
  if (sd.content && sd.content.speaker_notes) {{
    slide.addNotes(sd.content.speaker_notes);
  }}
}});

pres.writeFile({{ fileName: "{output_path_js}" }})
  .then(() => console.log("OK"))
  .catch(e => {{ console.error("ERR:" + e.message); process.exit(1); }});
"""


# -----------------------------------------------------------------------------
# PPTX EXPORT
# -----------------------------------------------------------------------------
def export_pptx(slides: list[SlideData], theme: Theme, filename: str | None = None) -> bytes:
    if filename is None:
        filename = f"presentation_{uuid.uuid4().hex[:8]}.pptx"

    domain_bg_path = None
    if slides and isinstance(slides[0].content, dict) and "domain_bg_path" in slides[0].content:
        domain_bg_path = slides[0].content["domain_bg_path"]

    output_path = os.path.abspath(os.path.join(OUTPUT_DIR, filename))
    script      = _build_js(slides, theme, output_path, domain_bg_path)

    script_path = os.path.join(PROJECT_DIR, f"_pptx_tmp_{uuid.uuid4().hex[:8]}.js")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)

    try:
        result = subprocess.run(
            ["node", script_path],
            capture_output=True, text=True, timeout=180,
            cwd=PROJECT_DIR,
        )
        if result.returncode != 0:
            raise RuntimeError(f"PptxGenJS error:\n{result.stdout}\n{result.stderr}")
            
        # Return the raw PPTX data instead of saving
        if not os.path.exists(output_path):
            raise RuntimeError(f"Output file not created: {output_path}")
            
        with open(output_path, "rb") as f:
            pptx_data = f.read()
    finally:
        # Cleanup files
        if os.path.exists(script_path):
            os.unlink(script_path)
        if os.path.exists(output_path):
            os.unlink(output_path)
        
    return pptx_data


# -----------------------------------------------------------------------------
# THEME AUTO-SELECTOR
# -----------------------------------------------------------------------------
def pick_theme(text: str) -> Theme:
    lower = text.lower()
    if any(w in lower for w in ["energy", "nature", "green", "environment", "eco", "forest", "climate"]):
        return Theme.FOREST_MOSS
    if any(w in lower for w in ["finance", "invest", "bank", "market", "stock", "fund", "revenue", "loan", "lending", "credit"]):
        return Theme.MIDNIGHT_EXECUTIVE
    if any(w in lower for w in ["health", "medical", "care", "hospital", "doctor", "patient"]):
        return Theme.OCEAN_GRADIENT
    if any(w in lower for w in ["startup", "product", "launch", "growth", "innovation", "brand"]):
        return Theme.CORAL_ENERGY
    if any(w in lower for w in ["history", "culture", "art", "heritage", "tradition"]):
        return Theme.WARM_TERRACOTTA
    return Theme.MIDNIGHT_EXECUTIVE


def extract_domain(query: str) -> str:
    system = "You are a senior analyst. Reply with exactly 1-2 words representing the core industry/domain."
    user = f"Extract the primary industry or domain from this query (e.g., Banking, Healthcare, Software, Education). Query: {query}"
    try:
        raw = call_bedrock(system, user, max_tokens=15)
        return raw.strip().strip('"').title()
    except Exception:
        return "Corporate"


# -----------------------------------------------------------------------------
# MAIN PIPELINE
# -----------------------------------------------------------------------------
def prepare_slides(user_input: str, provided_theme_id: str | None = None) -> tuple[list[SlideData], Theme]:
    mode       = detect_mode(user_input)
    
    # Use selected theme if valid, else pick based on content
    theme = None
    if provided_theme_id:
        try:
            theme = Theme(provided_theme_id)
        except ValueError:
            pass
            
    if not theme:
        theme = pick_theme(user_input)
        
    word_count = len(user_input.split())

    if word_count <= 40:
        num_slides = 10
    elif word_count <= 200:
        num_slides = 12
    else:
        num_slides = min(15, max(10, word_count // 80))

    has_process = is_process_topic(user_input)
    print(f"\n  Detecting domain for professional background...")
    domain = extract_domain(user_input)
    print(f"     Domain: {domain}")
    bg_prompt = f"Abstract, elegant, professional corporate presentation background for the {domain} industry. Dark cinematic lighting, beautiful minimalist textures, plenty of negative space."
    domain_bg_path = generate_slide_image(bg_prompt, user_query=user_input)

    print(f"\n  Mode        : {'TOPIC   LLM will generate content' if mode == 'topic' else 'CONTENT   LLM will structure your text'}")
    print(f"  Slides      : {num_slides}{' + 1 diagram' if has_process else ''}")
    print(f"  Theme       : {theme.value}")
    print(f"  Diagram     : {'YES   Nova Canvas icons + PIL image' if has_process else 'NO'}")

    # -- Step 1: Outline ------------------------------------------------------
    print(f"\n  [1/4] Building outline...")
    outline = (
        outline_from_topic(user_input, num_slides) if mode == "topic"
        else outline_from_content(user_input, num_slides)
    )

    print(f"\n  {'-'*54}")
    print(f"  OUTLINE")
    print(f"  {'-'*54}")
    for item in outline:
        print(f"  {item.slide_number:2d}. [{item.content_type.value:<15s}] {item.title}")
    if has_process:
        print(f"  --  [diagram        ] Process Flow Diagram  (auto-generated image)")
    print(f"  {'-'*54}\n")

    # -- Step 2: Fill regular slides ------------------------------------------
    print(f"  [2/4] Filling slide content...")
    slides = fill_all_slides(outline, mode, user_input)

    # -- Step 3: Diagram slide (image-based pipeline) -------------------------
    if has_process:
        print(f"\n  [3/4] Building process flow diagram (Claude -> Nova Canvas -> PIL)...")

        # Full pipeline: plan steps -> generate icons -> assemble PNG
        diagram_result = build_flow_diagram_image(
            user_query  = user_input,
            max_steps   = 6,
        )

        # Position: insert before thank_you
        thank_you = [s for s in slides if s.content_type == ContentType.THANK_YOU]
        other     = [s for s in slides if s.content_type != ContentType.THANK_YOU]
        diagram_num = (other[-1].slide_number + 1) if other else len(slides)

        diagram_slide = SlideData(
            slide_number = diagram_num,
            content_type = ContentType.DIAGRAM,
            content      = {
                "title"         : diagram_result["title"],
                # Path to the pre-assembled PNG   renderDiagram() embeds it
                "diagram_image" : diagram_result["image_path"].replace("\\", "/"),
                "diagram_images": {k: v.replace("\\", "/") for k, v in diagram_result.get("image_paths", {}).items()},
                # Full step info so the editor UI can display & modify each step
                "steps": [
                    {
                        "step_number": s.step_number,
                        "label":       s.label,
                        "description": s.description,
                        "icon_prompt": s.icon_prompt,
                        "icon_path":   s.icon_path.replace("\\", "/") if s.icon_path else "",
                    }
                    for s in diagram_result["steps"]
                ],
                "speaker_notes" : (
                    f"This diagram shows the {len(diagram_result['steps'])}-step process: "
                    f"{diagram_result['title']}. "
                    "Steps: " + ", ".join(s.label for s in diagram_result["steps"]) + "."
                ),
            },
        )

        # Re-number thank_you
        for s in thank_you:
            s.slide_number = diagram_num + 1

        slides = other + [diagram_slide] + thank_you

    else:
        print(f"\n  [3/4] Skipping diagram (not a process topic).")

    if slides and domain_bg_path:
        slides[0].content["domain_bg_path"] = domain_bg_path.replace("\\", "/")

    return slides, theme


def generate_layout_variants(
    base_slides: list[SlideData],
    num_variants: int = 4,
) -> list[list[SlideData]]:
    """
    Create layout/style variants for a single set of slides WITHOUT extra Bedrock calls.

    We keep content identical and only tweak `layout_override` for supported types
    using the layouts already implemented in the PPTXGenJS template:
      - title:       default | title_left | title_dark
      - bullets:     default | bullets_box
      - two_column:  default | two_column_cards

    This returns a list of slide lists: one per variant.
    """
    if not base_slides:
        return []

    title_layouts = ["", "title_left", "title_dark"]
    bullets_layouts = ["", "bullets_box"]
    two_col_layouts = ["", "two_column_cards"]

    variants: list[list[SlideData]] = []

    for v in range(max(1, num_variants)):
        variant_slides: list[SlideData] = []
        for s in base_slides:
            # shallow copy of content dict to avoid mutating original
            content_copy = dict(s.content)
            layout = s.layout_override or ""

            if s.content_type == ContentType.TITLE:
                layout = title_layouts[v % len(title_layouts)]
            elif s.content_type == ContentType.BULLETS:
                layout = bullets_layouts[v % len(bullets_layouts)]
            elif s.content_type == ContentType.TWO_COLUMN:
                layout = two_col_layouts[v % len(two_col_layouts)]

            variant_slides.append(
                SlideData(
                    slide_number=s.slide_number,
                    content_type=s.content_type,
                    content=content_copy,
                    layout_override=layout or None,
                )
            )
        variants.append(variant_slides)

    return variants

def run(user_input: str) -> tuple[bytes, list]:
    slides, theme = prepare_slides(user_input)
    pid = uuid.uuid4().hex[:8]
    print(f"\n  [4/4] Exporting .pptx...")
    pptx_data = export_pptx(slides, theme, filename=f"{pid}.pptx")
    return pptx_data, slides


# -----------------------------------------------------------------------------
#     EDIT YOUR INPUT HERE
# -----------------------------------------------------------------------------

# What are the different steps involved in a loan processing in a lending tech startup. I need you to give me step by step processes involved in loan disbursal, approving a loan, how the loan gets underwritten.
USER_INPUT = """
Explain the complete loan processing workflow followed by a modern lending tech platform.
Cover how a loan is applied for, evaluated, underwritten, approved, and disbursed, along with how technology enables faster and safer decision-making.
"""

# -- Multiline content example (uncomment to use) -----------------------------
# USER_INPUT = """
# Renewable energy is transforming the global power sector.
# Solar and wind have become the cheapest sources of electricity
# in history, with costs dropping over 90% in the last decade.
#
# Key Statistics:
# - Solar capacity grew 200% between 2018 and 2023
# - Wind energy now powers 15% of Europe's electricity needs
# - Global investment in renewables hit $500 billion in 2023
#
# Challenges: grid stability, energy storage, infrastructure upgrades.
# Opportunities: green hydrogen, offshore wind, AI-optimized smart grids.
# """


# -----------------------------------------------------------------------------
# ENTRY POINT
# -----------------------------------------------------------------------------
if __name__ == "__main__":

    print("=" * 57)
    print("    AI PowerPoint Generator  (AWS Bedrock + Nova Canvas)")
    print("=" * 57)

    user_input = USER_INPUT.strip()
    if not user_input:
        print("  No input provided. Edit USER_INPUT in the script.")
        sys.exit(1)

    if not PIL_AVAILABLE:
        print("  [ERROR]  Pillow is required for diagram generation.")
        print("       Run: pip install Pillow")
        sys.exit(1)

    print(f"\n  Input : {user_input[:90]}{'...' if len(user_input) > 90 else ''}")

    try:
        # pptx_path = run(user_input)
        # print("\n" + "=" * 57)
        # print(f"  Done!  File saved to:\n  {pptx_path}")
        # print(f"  Icons dir : {os.path.abspath(ICONS_DIR)}")
        # print(f"  Output dir: {os.path.abspath(OUTPUT_DIR)}")
        print("Backend script now configured to return raw bytes, saving locally handled via Streamlit UI.")
        print("=" * 57)
    except Exception as e:
        print(f"\n  Error: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)