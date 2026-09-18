# -*- coding: utf-8 -*-
"""
translator.py
=============
كل منطق المعالجة: استخراج النص، الترجمة، إيجاد المساحة الفارغة،
ورسم الملاحظات العربية (Study Annotation) فوق ملفات PDF و PPTX.

مبدأ صارم: كل شيء في الذاكرة (RAM / io.BytesIO) فقط.
لا يوجد أي حفظ على القرص، لا قاعدة بيانات، لا أرشيف، لا سجل.
"""

from __future__ import annotations

import glob
import io
import math
import os

import arabic_reshaper
import httpx
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

import fitz  # PyMuPDF
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

# ============================================================
# الإعدادات (كلها قابلة للتعديل من Environment Variables)
# ============================================================

HANDWRITING_FONT = os.environ.get("HANDWRITING_FONT", "")          # مسار ملف خط يد عربي .ttf
HANDWRITING_SIZE = float(os.environ.get("HANDWRITING_SIZE", "15"))  # الحجم الأساسي بالنقاط
HANDWRITING_COLOR = os.environ.get("HANDWRITING_COLOR", "")         # "R,G,B" (يعدل اللون الأساسي)

# لوحة ألوان ثابتة (Global Annotation Style) — حبر مذاكرة هادي
PRIMARY_INK = (35, 70, 150)        # أزرق حبر — الترجمة الأساسية
SECONDARY_INK = (45, 125, 85)      # أخضر حبر — مصطلحات مهمة
NOTE_INK = (105, 75, 145)          # بنفسجي — تعريفات/ملاحظات
PINK_HIGHLIGHT = (240, 150, 180, 70)   # وردي شفاف — تحديد خفيف
YELLOW_HIGHLIGHT = (255, 235, 120, 85)  # أصفر شفاف — Highlighter

PDF_ZOOM = 2.0        # دقة الرسم فوق الصفحة (2x = 144 dpi) دون تغيير مقاس الصفحة
PDF_MARGIN = 8.0      # هامش آمن من حواف الصفحة (نقاط)
PLACE_GAP = 6.0       # مسافة فاصلة بين النص الأصلي والترجمة
MIN_FONT = 6.5        # أصغر خط مقبول قبل الاستسلام
TITLE_SIZE = 17.0     # اعتبار البلوك عنوانًا إذا كان خطه الأصلي بهذا الحجم أو أكبر
MAX_BLOCKS_PER_PAGE = 40

# مراحل المعالجة (bot.py يعرض رسائل التقدم العربية عليها)
STAGE_EXTRACT = "extract"
STAGE_TRANSLATE = "translate"
STAGE_FORMAT = "format"
STAGE_CHECK = "check"

# ============================================================
# Glossary — قاموس المصطلحات لضمان الاتساق
# ============================================================

GLOSSARY = {
    "Business Ethics": "أخلاقيات الأعمال",
    "business ethics": "أخلاقيات الأعمال",
    "Ethics": "الأخلاق",
    "ethics": "الأخلاق",
    "Moral reasoning": "التفكير الأخلاقي",
    "Moral responsibility": "المسؤولية الأخلاقية",
    "Ethical dilemma": "المعضلة الأخلاقية",
    "Globalization": "العولمة",
    "International business": "الأعمال الدولية",
    "Learning Objectives": "أهداف التعلم",
    "Principles of conduct": "مبادئ السلوك",
    "Personal rules": "القواعد الشخصية",
    "Study of morality": "دراسة الأخلاق",
}

# ============================================================
# الترجمة — دالة قابلة للتبديل
# ============================================================


def _apply_glossary(text: str) -> str:
    """إذا تسرب مصطلح إنجليزي معروف إلى الناتج، نستبدله بالمصطلح المعتمد."""
    result = text
    for en, ar in GLOSSARY.items():
        if en in result:
            result = result.replace(en, ar)
    return result


def _offline_fallback(text: str) -> str:
    """بدون مفتاح API: نستبدل المصطلحات المعروفة فقط ونرجع النص كما هو.
    (حتى لا ينهار البوت أبدًا عند غياب الإعداد — يُنصح بضبط TRANSLATION_API_URL)"""
    result = text
    for en, ar in GLOSSARY.items():
        result = result.replace(en, ar)
    return result


def _translate_gemini(text: str, context: str, api_key: str) -> str:
    """ترجمة عبر Google Gemini — مفتاح مجاني من Google AI Studio."""
    model = os.environ.get("TRANSLATION_MODEL", "").strip() or "gemini-3.6-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    prompt = (
        "You are a professional academic translator for university study materials.\n"
        "Translate the following English text into natural, clear, academic Arabic "
        "suitable for a university student's study notes. Keep numbers, proper names, "
        "and technical terms accurate. Do NOT add any information that is not in the source.\n"
        f"Context of the material: {context}\n\n"
        f"Text to translate:\n{text}\n\n"
        "Return ONLY the Arabic translation, nothing else."
    )
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    resp = httpx.post(url, json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    result = "".join(part.get("text", "") for part in parts).strip()
    if result.startswith("```"):  # إزالة أسوار Markdown لو وُجدت
        result = result.strip("`").strip()
        if result[:6].lower() in ("arabic", "text\n "):
            result = result.split("\n", 1)[-1].strip()
    return result


def _translate_openai(text: str, context: str, api_key: str) -> str:
    """ترجمة عبر OpenAI API الرسمية (ChatGPT) — مفتاح يبدأ بـ sk- (مدفوع بالاستخدام)."""
    model = os.environ.get("TRANSLATION_MODEL", "").strip() or "gpt-4o-mini"
    system = (
        "You are a professional academic translator for university study materials.\n"
        "Translate the given English text into natural, clear, academic Arabic "
        "suitable for a university student's study notes. Keep numbers, proper names, "
        "and technical terms accurate. Do NOT add any information that is not in the source.\n"
        "Return ONLY the Arabic translation, nothing else."
    )
    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": f"Context of the material: {context}\n\nText to translate:\n{text}",
                },
            ],
        },
        timeout=60,
    )
    resp.raise_for_status()
    choices = resp.json().get("choices") or []
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "").strip()


def _translate_deepl(text: str, context: str, api_key: str) -> str:
    """ترجمة عبر DeepL API — المفتاح المجاني ينتهي بـ :fx."""
    host = "https://api-free.deepl.com" if api_key.endswith(":fx") else "https://api.deepl.com"
    url = f"{host}/v2/translate"
    resp = httpx.post(
        url,
        json={"text": [text], "target_lang": "AR"},
        headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
        timeout=30,
    )
    resp.raise_for_status()
    translations = resp.json().get("translations") or []
    return translations[0].get("text", "") if translations else ""


def _translate_google_free(text: str) -> str:
    """الافتراضي بدون أي مفتاح: نقطة النهاية العامة المجانية لترجمة Google.
    تُستخدم فقط عندما لا يوجد TRANSLATION_API_KEY إطلاقًا."""
    resp = httpx.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "en", "tl": "ar", "dt": "t", "q": text},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    segments = data[0] if isinstance(data, list) and data else []
    return "".join(
        seg[0] for seg in segments if isinstance(seg, list) and seg and isinstance(seg[0], str)
    )


def active_provider() -> str:
    """تحديد محرك الترجمة الحالي (تُستخدم أيضًا في رسالة بدء التشغيل في bot.py)."""
    api_key = os.environ.get("TRANSLATION_API_KEY", "").strip()
    api_url = os.environ.get("TRANSLATION_API_URL", "").strip()
    provider = os.environ.get("TRANSLATION_PROVIDER", "").strip().lower()
    if not provider:  # تعرف تلقائي على المزوّد من شكل المفتاح
        if api_key.startswith("AIza") or api_key.startswith("AQ.") or api_key.startswith("AQ/"):
            provider = "gemini"
        elif api_key.endswith(":fx"):
            provider = "deepl"
        elif api_key.startswith("sk-"):
            provider = "openai"
        elif api_url:
            provider = "custom"
        else:
            provider = "free"
    return provider


def _translate_custom(text: str, context: str, api_key: str, api_url: str) -> str:
    """أي خدمة ترجمة خاصة: POST JSON ويُقبل أي شكل استجابة شائع."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"text": text, "source": "en", "target": "ar", "context": context or ""}
    resp = httpx.post(api_url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    result = ""
    if isinstance(data, dict):
        for key in ("translated_text", "translation", "translatedText", "text", "ar"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                result = value
                break
        if not result:
            translations = data.get("translations") or (data.get("data") or {}).get("translations")
            if isinstance(translations, list) and translations:
                first = translations[0]
                if isinstance(first, dict):
                    result = first.get("translatedText", "") or first.get("text", "")
                elif isinstance(first, str):
                    result = first
    elif isinstance(data, str):
        result = data
    return result


def translate_text(text: str, context: str = "") -> str:
    """يستقبل نصًا إنجليزيًا + سياقًا، ويعيد ترجمة عربية أكاديمية طبيعية.

    المزوّدون المدعومون (يُحدَّدون عبر TRANSLATION_PROVIDER):
      gemini  : Google Gemini — مفتاح مجاني من https://aistudio.google.com/app/api-keys
      deepl   : DeepL API — مفتاح مجاني (ينتهي بـ :fx) من https://www.deepl.com/pro-api
      openai  : OpenAI API الرسمية — مفتاح يبدأ بـ sk- (مدفوع بالاستخدام)
      custom  : أي رابط JSON خاص عبر TRANSLATION_API_URL
      free    : بدون أي مفتاح — ترجمة مجانية تلقائية (الافتراضي)

    إذا تُرك TRANSLATION_PROVIDER فارغًا يتم التعرف على المزوّد تلقائيًا
    من شكل المفتاح، وبدون أي مفتاح تُستخدم الترجمة المجانية العامة.
    """
    if not text or not text.strip():
        return text

    api_key = os.environ.get("TRANSLATION_API_KEY", "").strip()
    api_url = os.environ.get("TRANSLATION_API_URL", "").strip()
    provider = active_provider()

    try:
        if provider == "gemini" and api_key:
            result = _translate_gemini(text, context, api_key)
        elif provider == "deepl" and api_key:
            result = _translate_deepl(text, context, api_key)
        elif provider == "openai" and api_key:
            result = _translate_openai(text, context, api_key)
        elif provider == "custom" and api_url:
            result = _translate_custom(text, context, api_key, api_url)
        else:
            result = _translate_google_free(text)  # الافتراضي بدون أي مفتاح
    except Exception:
        return _offline_fallback(text)

    if not result.strip():
        return _offline_fallback(text)
    return _apply_glossary(result)


# ============================================================
# أدوات مشتركة: الخط العربي + RTL + القياس
# ============================================================

_FONT_FILE_CACHE: list | None = None
_FONT_CACHE: dict[int, object] = {}


def _font_file() -> str | None:
    """إيجاد ملف الخط: متغير HANDWRITING_FONT أولًا، ثم خط يد عربي، ثم Noto Naskh Arabic كـfallback."""
    global _FONT_FILE_CACHE
    if _FONT_FILE_CACHE is not None:
        return _FONT_FILE_CACHE[0] if _FONT_FILE_CACHE else None

    found: str | None = None
    if HANDWRITING_FONT and os.path.exists(HANDWRITING_FONT):
        found = HANDWRITING_FONT
    else:
        patterns = [
            "/usr/share/fonts/**/NotoNaskhArabic*.ttf",
            "/usr/share/fonts/**/Noto*Naskh*Arabic*.ttf",
            "/usr/share/fonts/**/Amiri*.ttf",
            "/usr/share/fonts/**/*Arab*.ttf",
        ]
        for pattern in patterns:
            matches = sorted(glob.glob(pattern, recursive=True))
            if matches:
                found = matches[0]
                break
    _FONT_FILE_CACHE = [found] if found else []
    return found


def _load_font(px_size: float):
    key = max(6, int(round(px_size)))
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    font = None
    path = _font_file()
    if path:
        try:
            font = ImageFont.truetype(path, key)
        except Exception:
            font = None
    if font is None:
        try:
            font = ImageFont.load_default(key)
        except Exception:
            font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _rtl(text: str) -> str:
    """تشكيل الحروف العربية وترتيبها RTL قبل الرسم."""
    try:
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def _wrap_arabic(text: str, font, max_width: float, measurer: ImageDraw.ImageDraw) -> list[str]:
    """تقسيم النص العربي لأسطر تقيس بعرض المساحة المتاحة."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = current + " " + word
        try:
            width = measurer.textlength(_rtl(trial), font=font)
        except Exception:
            width = len(trial) * 7.0
        if width <= max_width or not lines:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _ink_for(text: str, is_title: bool) -> tuple[int, int, int]:
    """اختيار لون الحبر حسب أسلوب الملاحظة (ثابت عبر كل الملف)."""
    lowered = text.lower()
    for term in ("ethics", "dilemma", "responsibility", "مهم"):
        if term in lowered:
            return SECONDARY_INK
    if is_title:
        return PRIMARY_INK
    return PRIMARY_INK


# ============================================================
# PDF — PyMuPDF + طبقة Annotation في الذاكرة
# ============================================================


def _candidate_rects(bbox: fitz.Rect, page_rect: fitz.Rect) -> list[tuple[fitz.Rect, str]]:
    """اقتراحات الترتيب: يمين ← يسار ← أسفل ← أعلى، داخل حدود الصفحة."""
    gap, m = PLACE_GAP, PDF_MARGIN
    candidates = [
        (fitz.Rect(bbox.x1 + gap, bbox.y0, page_rect.x1 - m, bbox.y1), "right"),
        (fitz.Rect(page_rect.x0 + m, bbox.y0, bbox.x0 - gap, bbox.y1), "left"),
        (fitz.Rect(bbox.x0, bbox.y1 + gap, bbox.x1, page_rect.y1 - m), "bottom"),
        (fitz.Rect(bbox.x0, page_rect.y0 + m, bbox.x1, bbox.y0 - gap), "top"),
    ]
    return [
        (rect, direction)
        for rect, direction in candidates
        if not rect.is_empty and rect.width > 30 and rect.height > 12
    ]


def _rect_fits(rect: fitz.Rect, obstacles: list[fitz.Rect]) -> bool:
    for obstacle in obstacles:
        if rect.intersects(obstacle):
            return False
    return True


def _pdf_text_blocks(page: fitz.Page) -> list[dict]:
    blocks: list[dict] = []
    data = page.get_text("dict")
    for raw in data.get("blocks", []):
        if raw.get("type") != 0:
            continue
        lines, sizes = [], []
        for line in raw.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
            if text:
                lines.append(text)
                for span in line.get("spans", []):
                    sizes.append(float(span.get("size", 12)))
        text = " ".join(lines).strip()
        if len(text) < 2 or not any(ch.isalpha() for ch in text):
            continue
        blocks.append(
            {
                "bbox": fitz.Rect(raw["bbox"]),
                "text": text,
                "size": max(sizes) if sizes else 12.0,
            }
        )
    return blocks


def _ocr_blocks(page: fitz.Page) -> list[dict]:
    """OCR اختياري فقط للصفحات التي لا تحتوي نصًا حقيقيًا (توفير CPU)."""
    try:
        import pytesseract
    except ImportError:
        return []
    try:
        pix = page.get_pixmap(dpi=150)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        data = pytesseract.image_to_data(image, lang="eng", output_type=pytesseract.Output.DICT)
    except Exception:
        return []

    scale = 72.0 / 150.0
    groups: dict[tuple, list] = {}
    for i in range(len(data["text"])):
        word = (data["text"][i] or "").strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        groups.setdefault(key, []).append(
            (data["left"][i], data["top"][i], data["width"][i], data["height"][i], word)
        )

    blocks = []
    for items in groups.values():
        text = " ".join(word for *_, word in items)
        if len(text) < 2 or not any(ch.isalpha() for ch in text):
            continue
        x0 = min(x for x, *_ in items) * scale
        y0 = min(y for _, y, *_ in items) * scale
        x1 = max(x + w for x, _, w, *_ in items) * scale
        y1 = max(y + h for _, y, _, h, _ in items) * scale
        blocks.append(
            {
                "bbox": fitz.Rect(x0, y0, x1, y1),
                "text": text,
                "size": 12.0,
            }
        )
    return blocks


def _fit_annotation(
    arabic: str, candidate: fitz.Rect, base_size: float, measurer: ImageDraw.ImageDraw
):
    """إيجاد حجم خط يجعل الترجمة تدخل المساحة (تصغير تدريجي). يعيد (font, lines, line_h, used_rect)."""
    size = base_size
    while size >= MIN_FONT:
        font = _load_font(size * PDF_ZOOM)
        lines = _wrap_arabic(arabic, font, candidate.width * PDF_ZOOM - 4, measurer)
        line_h = size * 1.45
        total_h = line_h * len(lines)
        if lines and total_h <= candidate.height - 2:
            max_line_w = 0.0
            for line in lines:
                try:
                    max_line_w = max(max_line_w, measurer.textlength(_rtl(line), font=font))
                except Exception:
                    max_line_w = candidate.width * PDF_ZOOM
            used = fitz.Rect(
                candidate.x0,
                candidate.y0,
                min(candidate.x1, candidate.x0 + max_line_w / PDF_ZOOM + 4),
                min(candidate.y1, candidate.y0 + total_h + 4),
            )
            return font, lines, line_h, used
        size -= 0.5
    return None


def _draw_arrow(draw: ImageDraw.ImageDraw, cx: float, y_start: float, upward: bool, ink, zoom: float):
    """سهم قلم صغير يدوي بين الترجمة والنص الأصلي (يُستخدم باعتدال)."""
    length = 7 * zoom
    y_end = y_start - length if upward else y_start + length
    color = ink + (200,)
    draw.line([(cx, y_start), (cx, y_end)], fill=color, width=max(1, int(1.2 * zoom)))
    head = 3 * zoom
    if upward:
        draw.line([(cx, y_end), (cx - head, y_end + head)], fill=color, width=1)
        draw.line([(cx, y_end), (cx + head, y_end + head)], fill=color, width=1)
    else:
        draw.line([(cx, y_end), (cx - head, y_end - head)], fill=color, width=1)
        draw.line([(cx, y_end), (cx + head, y_end - head)], fill=color, width=1)


def _annotate_pdf_page(page: fitz.Page, translate_fn) -> None:
    page_rect = page.rect
    blocks = _pdf_text_blocks(page)
    if not blocks:
        blocks = _ocr_blocks(page)
    if not blocks:
        return

    # خريطة الإشغال: كل النصوص + الصور + الأشكال (Collision Detection)
    occupancy = [fitz.Rect(block["bbox"]) for block in blocks]
    try:
        for info in page.get_image_info():
            occupancy.append(fitz.Rect(info["bbox"]))
    except Exception:
        pass
    try:
        for drawing in page.get_drawings():
            occupancy.append(fitz.Rect(drawing["rect"]))
    except Exception:
        pass

    context = " ".join(block["text"] for block in blocks[:2])[:160]
    dummy = Image.new("RGBA", (8, 8))
    measurer = ImageDraw.Draw(dummy)

    annotations = []  # (font, lines, line_h, used_rect, ink, is_title, direction)
    for block in blocks[:MAX_BLOCKS_PER_PAGE]:
        text = block["text"]
        if text.isdigit() and len(text) <= 4:  # أرقام صفحات
            continue
        arabic = translate_fn(text, context)
        if not arabic or arabic.strip() == text:
            continue

        is_title = block["size"] >= TITLE_SIZE
        base_size = max(8.0, min(HANDWRITING_SIZE, block["size"] * 0.75))

        for candidate, direction in _candidate_rects(block["bbox"], page_rect):
            fitted = _fit_annotation(arabic, candidate, base_size, measurer)
            if fitted is None:
                continue
            font, lines, line_h, used = fitted
            if not _rect_fits(used, occupancy):
                continue
            ink = _ink_for(text, is_title)
            annotations.append((font, lines, line_h, used, ink, is_title, direction))
            occupancy.append(used)  # منع تداخل الملاحظات مع بعضها
            break

    if not annotations:
        return

    # رسم طبقة Annotation شفافة بحجم الصفحة × ZOOM ثم إدراجها فوق الصفحة الأصلية
    overlay = Image.new(
        "RGBA",
        (int(page_rect.width * PDF_ZOOM), int(page_rect.height * PDF_ZOOM)),
        (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(overlay)

    for font, lines, line_h, used, ink, is_title, direction in annotations:
        x = used.x0 * PDF_ZOOM
        y = used.y0 * PDF_ZOOM
        for index, line in enumerate(lines):
            display = _rtl(line)
            line_y = y + index * line_h * PDF_ZOOM
            if index == 0 and is_title:  # تظليل أصفر خفيف خلف العنوان فقط
                try:
                    lw = draw.textlength(display, font=font)
                except Exception:
                    lw = used.width * PDF_ZOOM
                draw.rectangle(
                    [x - 3, line_y - 2, x + lw + 3, line_y + line_h * PDF_ZOOM],
                    fill=YELLOW_HIGHLIGHT,
                )
            draw.text((x, line_y), display, font=font, fill=ink + (242,))
        # سهم صغير فقط للملاحظات العلوية/السفلية القصيرة
        if direction in ("top", "bottom") and len(lines) <= 3:
            cx = (used.x0 + used.width / 2) * PDF_ZOOM
            if direction == "bottom":
                _draw_arrow(draw, cx, used.y0 * PDF_ZOOM - 2, upward=True, ink=ink, zoom=PDF_ZOOM)
            else:
                _draw_arrow(
                    draw, cx, used.y1 * PDF_ZOOM + 2, upward=False, ink=ink, zoom=PDF_ZOOM
                )

    png_buffer = io.BytesIO()
    overlay.save(png_buffer, format="PNG")
    png_buffer.seek(0)
    page.insert_image(page_rect, stream=png_buffer.getvalue(), overlay=True)
    png_buffer.close()


def translate_pdf(pdf_bytes: bytes, progress=None, translate_fn=None) -> io.BytesIO:
    """يستقبل بايتات PDF ويعيد BytesIO للـPDF المترجم — دون لمس القرص إطلاقًا."""
    translate = translate_fn or translate_text
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise ValueError("تعذر قراءة ملف PDF") from exc

    if progress:
        progress(STAGE_EXTRACT)

    for page in doc:
        if progress:
            progress(STAGE_TRANSLATE)
        _annotate_pdf_page(page, translate)

    if progress:
        progress(STAGE_FORMAT)
        progress(STAGE_CHECK)

    output = io.BytesIO()
    doc.save(output, garbage=3, deflate=True)  # نفس مقاس الصفحات، بدون صفحات جديدة
    doc.close()
    output.seek(0)
    return output


# ============================================================
# PPTX — python-pptx: Text Box عربية حقيقية داخل الشريحة الأصلية
# ============================================================

EMU_GAP = 45720  # ≈ 0.05 بوصة فاصلة


def _pptx_font_name() -> str:
    name = os.environ.get("HANDWRITING_FONT_NAME", "")
    if name:
        return name
    path = _font_file()
    if path:
        return os.path.splitext(os.path.basename(path))[0]
    return "Arial"


def _shape_font_pt(shape) -> float:
    try:
        for paragraph in shape.text_frame.paragraphs:
            for run in paragraph.runs:
                if run.font.size:
                    return float(run.font.size.pt)
    except Exception:
        pass
    return 18.0


def _candidate_rects_emu(bbox, page_w: int, page_h: int):
    x0, y0, x1, y1 = bbox
    candidates = [
        ((x1 + EMU_GAP, y0, page_w, y1), "right"),
        ((0, y0, x0 - EMU_GAP, y1), "left"),
        ((x0, y1 + EMU_GAP, x1, page_h), "bottom"),
        ((x0, 0, x1, y0 - EMU_GAP), "top"),
    ]
    result = []
    for rect, direction in candidates:
        rx0, ry0, rx1, ry1 = rect
        if rx1 - rx0 > 228600 and ry1 - ry0 > 152400 and rx0 >= 0 and ry0 >= 0 and rx1 <= page_w and ry1 <= page_h:
            result.append((rect, direction))
    return result


def _rects_overlap(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _fits_emu(rect, obstacles) -> bool:
    return not any(_rects_overlap(rect, obstacle) for obstacle in obstacles)


def _add_arabic_textbox(slide, rect, lines, size_pt, font_name, color):
    box = slide.shapes.add_textbox(Emu(rect[0]), Emu(rect[1]), Emu(rect[2] - rect[0]), Emu(rect[3] - rect[1]))
    frame = box.text_frame
    frame.word_wrap = True
    for index, line in enumerate(lines):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        paragraph.alignment = PP_ALIGN.RIGHT
        paragraph._p.get_or_add_pPr().set("rtl", "1")  # اتجاه RTL حقيقي
        run = paragraph.add_run()
        run.text = line
        run.font.size = Pt(size_pt)
        run.font.name = font_name
        run.font.color.rgb = RGBColor(*color)
        # خط النصوص المركبة (العربية) في PowerPoint
        rPr = run._r.get_or_add_rPr()
        for tag in ("a:cs", "a:ea"):
            element = rPr.find(qn(tag))
            if element is None:
                element = rPr.makeelement(qn(tag), {})
                rPr.append(element)
            element.set("typeface", font_name)


def _place_pptx_annotation(slide, arabic: str, bbox, obstacles, page_w, page_h, base_pt, font_name):
    """اختيار أفضل مساحة فارغة (يمين←يسار←أسفل←أعلى) وإضافة Text Box عربية."""
    text_len = max(1, len(arabic))
    for candidate, _direction in _candidate_rects_emu(bbox, page_w, page_h):
        cx0, cy0, cx1, cy1 = candidate
        width_pt = (cx1 - cx0) / 12700.0
        height_pt = (cy1 - cy0) / 12700.0
        size = base_pt
        while size >= 8.0:
            chars_per_line = max(6, int(width_pt / (size * 0.52)))
            line_count = max(1, math.ceil(text_len / chars_per_line))
            needed_h = line_count * size * 1.45
            needed_w = min(width_pt, (text_len / line_count) * size * 0.52 + size)
            if needed_h <= height_pt:
                used_w_emu = int(needed_w * 12700)
                used_h_emu = int(needed_h * 12700)
                used = (cx0, cy0, min(cx1, cx0 + used_w_emu), min(cy1, cy0 + used_h_emu))
                if _fits_emu(used, obstacles):
                    lines = _wrap_plain(arabic, chars_per_line)
                    _add_arabic_textbox(slide, used, lines, size, font_name, PRIMARY_INK)
                    return used
            size -= 1.0
    return None


def _wrap_plain(text: str, chars_per_line: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = (current + " " + word).strip()
        if len(trial) <= chars_per_line or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def translate_pptx(pptx_bytes: bytes, progress=None, translate_fn=None) -> io.BytesIO:
    """يستقبل بايتات PPTX ويعيد BytesIO — الملف الأصلي كما هو + TextBox عربية حقيقية."""
    translate = translate_fn or translate_text
    try:
        prs = Presentation(io.BytesIO(pptx_bytes))
    except Exception as exc:
        raise ValueError("تعذر قراءة ملف PPTX") from exc

    if progress:
        progress(STAGE_EXTRACT)

    slide_w, slide_h = int(prs.slide_width), int(prs.slide_height)
    font_name = _pptx_font_name()

    for slide in prs.slides:
        if progress:
            progress(STAGE_TRANSLATE)

        # خريطة إشغال الشريحة من مواقع الأشكال الحقيقية (EMU)
        obstacles: list[tuple] = []
        text_shapes: list = []
        context = ""
        for shape in slide.shapes:
            if shape.left is None or shape.top is None:
                continue
            rect = (int(shape.left), int(shape.top), int(shape.left + shape.width), int(shape.top + shape.height))
            obstacles.append(rect)
            if shape.has_text_frame and shape.text_frame.text.strip():
                text_shapes.append((shape, rect))
                if not context:
                    context = shape.text_frame.text.strip()[:160]

        placed: list[tuple] = []
        for shape, bbox in text_shapes:
            text = shape.text_frame.text.strip()
            if len(text) < 2 or not any(ch.isalpha() for ch in text):
                continue
            if text.isdigit() and len(text) <= 4:
                continue
            arabic = translate(text, context)
            if not arabic or arabic.strip() == text:
                continue
            base_pt = max(9.0, min(HANDWRITING_SIZE, _shape_font_pt(shape) * 0.75))
            used = _place_pptx_annotation(
                slide, arabic, bbox, obstacles + placed, slide_w, slide_h, base_pt, font_name
            )
            if used:
                placed.append(used)

    if progress:
        progress(STAGE_FORMAT)
        progress(STAGE_CHECK)

    output = io.BytesIO()
    prs.save(output)
    output.seek(0)
    return output
