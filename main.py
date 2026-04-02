"""
Personalized Children's Book PDF Generator — Render Web Service
Uses Cloudflare R2 (S3-compatible) for file storage.

Endpoint: POST /generate-pdf
Body:
{
    "sku": "mothers-day-2026-softcover-200mm",
    "personalization": {"child_name": "Emma"},
    "order_id": "etsy-receipt-12345"
}

Also accepts legacy format:
{
    "book_id": "mothers-day-2026",
    "format_key": "softcover-200mm",
    "personalization": {"child_name": "Emma"},
    "order_id": "etsy-order-12345"
}

Returns: {
    "pdf_url": "https://pub-xxx.r2.dev/...",
    "story_text": "Once upon a time, Emma...",
    "audio_url_female": "https://pub-xxx.r2.dev/...",
    "audio_url_male": "https://pub-xxx.r2.dev/...",
    "order_id": "etsy-receipt-12345",
    "status": "success"
}
"""

import json
import os
import tempfile
from io import BytesIO

import boto3
import requests as http_requests
from flask import Flask, request, jsonify
from reportlab.lib.pagesizes import mm
from reportlab.lib.colors import Color
from reportlab.pdfgen import canvas
from PIL import Image

app = Flask(__name__)

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "glentales-books")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "")

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
VOICE_FEMALE = os.environ.get("VOICE_FEMALE", "qlnUbSLa6XkXV9pK52QP")
VOICE_MALE = os.environ.get("VOICE_MALE", "nzFihrBIvB34imQBuxub")
ELEVENLABS_MODEL = "eleven_multilingual_v2"

FORMATS = {
    "softcover-140mm": {"width_mm": 140, "height_mm": 140, "bleed_mm": 3, "safe_mm": 10, "cover_type": "softcover"},
    "softcover-200mm": {"width_mm": 200, "height_mm": 200, "bleed_mm": 3, "safe_mm": 10, "cover_type": "softcover"},
    "hardcover-200mm": {"width_mm": 200, "height_mm": 200, "bleed_mm": 3, "safe_mm": 15, "cover_type": "hardcover"},
    "hardcover-280mm": {"width_mm": 280, "height_mm": 280, "bleed_mm": 3, "safe_mm": 15, "cover_type": "hardcover"},
}


def get_r2_client():
    return boto3.client("s3", endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY, aws_secret_access_key=R2_SECRET_KEY, region_name="auto")


def r2_upload(key, data, content_type="application/octet-stream"):
    client = get_r2_client()
    client.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=data, ContentType=content_type)
    return f"{R2_PUBLIC_URL}/{key}"


def r2_download(key):
    client = get_r2_client()
    response = client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
    return response["Body"].read()


def r2_download_to_tempfile(key, suffix=".png"):
    data = r2_download(key)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(data)
    tmp.close()
    return tmp.name


def load_book_json(book_id):
    data = r2_download(f"books/{book_id}/book.json")
    return json.loads(data)


def replace_placeholders(text, personalization):
    for key, value in personalization.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def wrap_text(text, font_size, max_width):
    words = text.split()
    lines, current = [], ""
    avg_char_width = font_size * 0.5
    max_chars = int(max_width / avg_char_width)
    for word in words:
        test = f"{current} {word}".strip()
        if len(test) <= max_chars:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines

def parse_sku(sku):
    is_audio = sku.endswith("-audio")
    clean_sku = sku[:-6] if is_audio else sku
    for fmt_key in sorted(FORMATS.keys(), key=len, reverse=True):
        if clean_sku.endswith(fmt_key):
            book_id = clean_sku[:-(len(fmt_key) + 1)]
            return book_id, fmt_key, is_audio
    raise ValueError(f"Cannot parse SKU '{sku}'. No known format_key found.")


def generate_tts(text, voice_id):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    body = {"text": text, "model_id": ELEVENLABS_MODEL, "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    resp = http_requests.post(url, headers=headers, json=body, timeout=120)
    resp.raise_for_status()
    return resp.content


def build_story_text(book, personalization):
    parts = []
    title = replace_placeholders(book.get("title_template", ""), personalization)
    dedication = replace_placeholders(book.get("dedication_template", ""), personalization)
    if title:
        parts.append(title + ".")
    if dedication:
        parts.append(dedication)
    for spread in book.get("spreads", []):
        raw_text = spread.get("text", "")
        if raw_text:
            parts.append(replace_placeholders(raw_text, personalization))
    return " ".join(parts)


def create_pdf(book, book_id, format_key, personalization):
    fmt = FORMATS[format_key]
    w_mm = fmt["width_mm"] + 2 * fmt["bleed_mm"]
    h_mm = fmt["height_mm"] + 2 * fmt["bleed_mm"]
    w_pt = w_mm * mm
    h_pt = h_mm * mm
    bleed = fmt["bleed_mm"] * mm
    safe = fmt["safe_mm"] * mm
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(w_pt, h_pt))
    cream = Color(0.98, 0.96, 0.93)
    dark = Color(0.15, 0.15, 0.15)
    white = Color(1, 1, 1)
    semi_bg = Color(0, 0, 0, alpha=0.45)
    title_size = fmt["width_mm"] * 0.12
    body_size = fmt["width_mm"] * 0.045
    dedication_size = fmt["width_mm"] * 0.06
    title = replace_placeholders(book.get("title_template", ""), personalization)
    dedication = replace_placeholders(book.get("dedication_template", ""), personalization)
    spreads = book.get("spreads", [])
    cover_path = r2_download_to_tempfile(f"books/{book_id}/images/cover.png")
    c.drawImage(cover_path, 0, 0, width=w_pt, height=h_pt, preserveAspectRatio=True, anchor="c")
    c.setFont("Helvetica-Bold", title_size)
    c.setFillColor(white)
    title_y = h_pt * 0.55
    title_lines = wrap_text(title, title_size, w_pt - 2 * safe)
    for i, line in enumerate(title_lines):
        tw = c.stringWidth(line, "Helvetica-Bold", title_size)
        c.drawString((w_pt - tw) / 2, title_y - i * title_size * 1.3, line)
    c.showPage()
    c.setFillColor(cream)
    c.rect(0, 0, w_pt, h_pt, fill=True, stroke=False)
    c.setFont("Helvetica-Oblique", dedication_size)
    c.setFillColor(dark)
    ded_lines = wrap_text(dedication, dedication_size, w_pt - 2 * safe)
    ded_y = h_pt * 0.55
    for i, line in enumerate(ded_lines):
        tw = c.stringWidth(line, "Helvetica-Oblique", dedication_size)
        c.drawString((w_pt - tw) / 2, ded_y - i * dedication_size * 1.4, line)
    c.showPage()
    for spread in spreads:
        spread_num = spread["spread_number"]
        raw_text = spread.get("text", "")
        text = replace_placeholders(raw_text, personalization)
        text_pos = spread.get("text_position", "bottom")
        img_path = r2_download_to_tempfile(f"books/{book_id}/images/spread-{spread_num:02d}.png")
        img = Image.open(img_path)
        img_w, img_h = img.size
        mid = img_w // 2
        for page_idx, crop_box in enumerate([(0, 0, mid, img_h), (mid, 0, img_w, img_h)]):
            cropped = img.crop(crop_box)
            crop_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
            cropped.save(crop_tmp.name)
            c.drawImage(crop_tmp.name, 0, 0, width=w_pt, height=h_pt, preserveAspectRatio=False)
            if page_idx == 1 and text:
                lines = wrap_text(text, body_size, w_pt - 2 * safe)
                block_h = len(lines) * body_size * 1.4 + body_size
                if text_pos == "top":
                    box_y = h_pt - safe - block_h
                else:
                    box_y = safe
                c.setFillColor(semi_bg)"""
Personalized Children's Book PDF Generator - Render Web Service
Uses Cloudflare R2 (S3-compatible) for file storage.
Endpoint: POST /generate-pdf
"""

import json
import os
import tempfile
from io import BytesIO

import boto3
import requests as http_requests
from flask import Flask, request, jsonify
from reportlab.lib.pagesizes import mm
from reportlab.lib.colors import Color
from reportlab.pdfgen import canvas
from PIL import Image

app = Flask(__name__)

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "glentales-books")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "")

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
VOICE_FEMALE = os.environ.get("VOICE_FEMALE", "qlnUbSLa6XkXV9pK52QP")
VOICE_MALE = os.environ.get("VOICE_MALE", "nzFihrBIvB34imQBuxub")
ELEVENLABS_MODEL = "eleven_multilingual_v2"

FORMATS = {
    "softcover-140mm": {"width_mm": 140, "height_mm": 140, "bleed_mm": 3, "safe_mm": 10, "cover_type": "softcover"},
    "softcover-200mm": {"width_mm": 200, "height_mm": 200, "bleed_mm": 3, "safe_mm": 10, "cover_type": "softcover"},
    "hardcover-200mm": {"width_mm": 200, "height_mm": 200, "bleed_mm": 3, "safe_mm": 15, "cover_type": "hardcover"},
    "hardcover-280mm": {"width_mm": 280, "height_mm": 280, "bleed_mm": 3, "safe_mm": 15, "cover_type": "hardcover"},
}


def get_r2_client():
    return boto3.client("s3", endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY, aws_secret_access_key=R2_SECRET_KEY, region_name="auto")


def r2_upload(key, data, content_type="application/octet-stream"):
    client = get_r2_client()
    client.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=data, ContentType=content_type)
    return f"{R2_PUBLIC_URL}/{key}"


def r2_download(key):
    client = get_r2_client()
    response = client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
    return response["Body"].read()


def r2_download_to_tempfile(key, suffix=".png"):
    data = r2_download(key)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(data)
    tmp.close()
    return tmp.name


def load_book_json(book_id):
    data = r2_download(f"books/{book_id}/book.json")
    return json.loads(data)


def replace_placeholders(text, personalization):
    for key, value in personalization.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def wrap_text(text, font_size, max_width):
    words = text.split()
    lines, current = [], ""
    avg_char_width = font_size * 0.5
    max_chars = int(max_width / avg_char_width)
    for word in words:
        test = f"{current} {word}".strip()
        if len(test) <= max_chars:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def parse_sku(sku):
    is_audio = sku.endswith("-audio")
    clean_sku = sku[:-6] if is_audio else sku
    for fmt_key in sorted(FORMATS.keys(), key=len, reverse=True):
        if clean_sku.endswith(fmt_key):
            book_id = clean_sku[:-(len(fmt_key) + 1)]
            return book_id, fmt_key, is_audio
    raise ValueError(f"Cannot parse SKU '{sku}'. No known format_key found.")


def generate_tts(text, voice_id):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    body = {"text": text, "model_id": ELEVENLABS_MODEL, "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    resp = http_requests.post(url, headers=headers, json=body, timeout=120)
    resp.raise_for_status()
    return resp.content


def build_story_text(book, personalization):
    parts = []
    title = replace_placeholders(book.get("title_template", ""), personalization)
    dedication = replace_placeholders(book.get("dedication_template", ""), personalization)
    if title:
        parts.append(title + ".")
    if dedication:
        parts.append(dedication)
    for spread in book.get("spreads", []):
        raw_text = spread.get("text", "")
        if raw_text:
            parts.append(replace_placeholders(raw_text, personalization))
    return " ".join(parts)


def create_pdf(book, book_id, format_key, personalization):
    fmt = FORMATS[format_key]
    w_mm = fmt["width_mm"] + 2 * fmt["bleed_mm"]
    h_mm = fmt["height_mm"] + 2 * fmt["bleed_mm"]
    w_pt = w_mm * mm
    h_pt = h_mm * mm
    bleed = fmt["bleed_mm"] * mm
    safe = fmt["safe_mm"] * mm
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(w_pt, h_pt))
    cream = Color(0.98, 0.96, 0.93)
    dark = Color(0.15, 0.15, 0.15)
    white = Color(1, 1, 1)
    semi_bg = Color(0, 0, 0, alpha=0.45)
    title_size = fmt["width_mm"] * 0.12
    body_size = fmt["width_mm"] * 0.045
    dedication_size = fmt["width_mm"] * 0.06
    title = replace_placeholders(book.get("title_template", ""), personalization)
    dedication = replace_placeholders(book.get("dedication_template", ""), personalization)
    spreads = book.get("spreads", [])

    # Front Cover
    cover_path = r2_download_to_tempfile(f"books/{book_id}/images/cover.png")
    c.drawImage(cover_path, 0, 0, width=w_pt, height=h_pt, preserveAspectRatio=True, anchor="c")
    c.setFont("Helvetica-Bold", title_size)
    c.setFillColor(white)
    title_y = h_pt * 0.55
    title_lines = wrap_text(title, title_size, w_pt - 2 * safe)
    for i, line in enumerate(title_lines):
        tw = c.stringWidth(line, "Helvetica-Bold", title_size)
        c.drawString((w_pt - tw) / 2, title_y - i * title_size * 1.3, line)
    c.showPage()

    # Dedication
    c.setFillColor(cream)
    c.rect(0, 0, w_pt, h_pt, fill=True, stroke=False)
    c.setFont("Helvetica-Oblique", dedication_size)
    c.setFillColor(dark)
    ded_lines = wrap_text(dedication, dedication_size, w_pt - 2 * safe)
    ded_y = h_pt * 0.55
    for i, line in enumerate(ded_lines):
        tw = c.stringWidth(line, "Helvetica-Oblique", dedication_size)
        c.drawString((w_pt - tw) / 2, ded_y - i * dedication_size * 1.4, line)
    c.showPage()

    # Story Spreads
    for spread in spreads:
        spread_num = spread["spread_number"]
        raw_text = spread.get("text", "")
        text = replace_placeholders(raw_text, personalization)
        text_pos = spread.get("text_position", "bottom")
        img_path = r2_download_to_tempfile(f"books/{book_id}/images/spread-{spread_num:02d}.png")
        img = Image.open(img_path)
        img_w, img_h = img.size
        mid = img_w // 2
        for page_idx, crop_box in enumerate([(0, 0, mid, img_h), (mid, 0, img_w, img_h)]):
            cropped = img.crop(crop_box)
            crop_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
            cropped.save(crop_tmp.name)
            c.drawImage(crop_tmp.name, 0, 0, width=w_pt, height=h_pt, preserveAspectRatio=False)
            if page_idx == 1 and text:
                lines = wrap_text(text, body_size, w_pt - 2 * safe)
                block_h = len(lines) * body_size * 1.4 + body_size
                box_y = (h_pt - safe - block_h) if text_pos == "top" else safe
                c.setFillColor(semi_bg)
                c.roundRect(safe * 0.5, box_y - body_size * 0.3, w_pt - safe, block_h + body_size * 0.6, radius=6, fill=True, stroke=False)
                c.setFont("Helvetica", body_size)
                c.setFillColor(white)
                for j, line in enumerate(lines):
                    c.drawString(safe, box_y + block_h - (j + 1) * body_size * 1.4, line)
            c.showPage()
            os.unlink(crop_tmp.name)
        os.unlink(img_path)

    # Back Cover
    c.setFillColor(cream)
    c.rect(0, 0, w_pt, h_pt, fill=True, stroke=False)
    c.setFont("Helvetica-Oblique", dedication_size * 0.8)
    c.setFillColor(dark)
    msg = "Made with love"
    tw = c.stringWidth(msg, "Helvetica-Oblique", dedication_size * 0.8)
    c.drawString((w_pt - tw) / 2, h_pt * 0.5, msg)
    c.showPage()
    c.save()
    os.unlink(cover_path)
    return buf.getvalue()


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "glentales-pdf-generator"})


@app.route("/generate-pdf", methods=["POST", "OPTIONS"])
def generate_pdf():
    if request.method == "OPTIONS":
        return ("", 204, {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers": "Content-Type"})
    try:
        data = request.get_json(silent=True) or {}
        personalization = data.get("personalization", {})
        order_id = data.get("order_id", "unknown")
        sku = data.get("sku")
        is_audio = False
        if sku:
            book_id, format_key, is_audio = parse_sku(sku)
        else:
            book_id = data.get("book_id")
            format_key = data.get("format_key")
        if not book_id:
            return jsonify({"error": "sku or book_id required"}), 400
        if format_key not in FORMATS:
            return jsonify({"error": f"Invalid format_key '{format_key}'. Must be one of: {list(FORMATS.keys())}"}), 400
        if not personalization.get("child_name"):
            return jsonify({"error": "personalization.child_name required"}), 400
        book = load_book_json(book_id)
        pdf_bytes = create_pdf(book, book_id, format_key, personalization)
        pdf_key = f"orders/{order_id}/{format_key}.pdf"
        pdf_url = r2_upload(pdf_key, pdf_bytes, content_type="application/pdf")
        story_text = build_story_text(book, personalization)
        result = {
            "status": "success", "order_id": order_id, "book_id": book_id,
            "format_key": format_key, "sku": sku or f"{book_id}-{format_key}",
            "is_audio": is_audio, "child_name": personalization.get("child_name"),"""
Personalized Children's Book PDF Generator - Render Web Service
Uses Cloudflare R2 (S3-compatible) for file storage.
Endpoint: POST /generate-pdf
"""

import json
import os
import tempfile
from io import BytesIO

import boto3
import requests as http_requests
from flask import Flask, request, jsonify
from reportlab.lib.pagesizes import mm
from reportlab.lib.colors import Color
from reportlab.pdfgen import canvas
from PIL import Image

app = Flask(__name__)

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "glentales-books")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "")

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
VOICE_FEMALE = os.environ.get("VOICE_FEMALE", "qlnUbSLa6XkXV9pK52QP")
VOICE_MALE = os.environ.get("VOICE_MALE", "nzFihrBIvB34imQBuxub")
ELEVENLABS_MODEL = "eleven_multilingual_v2"

FORMATS = {
    "softcover-140mm": {"width_mm": 140, "height_mm": 140, "bleed_mm": 3, "safe_mm": 10, "cover_type": "softcover"},
    "softcover-200mm": {"width_mm": 200, "height_mm": 200, "bleed_mm": 3, "safe_mm": 10, "cover_type": "softcover"},
    "hardcover-200mm": {"width_mm": 200, "height_mm": 200, "bleed_mm": 3, "safe_mm": 15, "cover_type": "hardcover"},
    "hardcover-280mm": {"width_mm": 280, "height_mm": 280, "bleed_mm": 3, "safe_mm": 15, "cover_type": "hardcover"},
}


def get_r2_client():
    return boto3.client("s3", endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY, aws_secret_access_key=R2_SECRET_KEY, region_name="auto")


def r2_upload(key, data, content_type="application/octet-stream"):
    client = get_r2_client()
    client.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=data, ContentType=content_type)
    return f"{R2_PUBLIC_URL}/{key}"


def r2_download(key):
    client = get_r2_client()
    response = client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
    return response["Body"].read()


def r2_download_to_tempfile(key, suffix=".png"):
    data = r2_download(key)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(data)
    tmp.close()
    return tmp.name


def load_book_json(book_id):
    data = r2_download(f"books/{book_id}/book.json")
    return json.loads(data)


def replace_placeholders(text, personalization):
    for key, value in personalization.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def wrap_text(text, font_size, max_width):
    words = text.split()
    lines, current = [], ""
    avg_char_width = font_size * 0.5
    max_chars = int(max_width / avg_char_width)
    for word in words:
        test = f"{current} {word}".strip()
        if len(test) <= max_chars:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def parse_sku(sku):
    is_audio = sku.endswith("-audio")
    clean_sku = sku[:-6] if is_audio else sku
    for fmt_key in sorted(FORMATS.keys(), key=len, reverse=True):
        if clean_sku.endswith(fmt_key):
            book_id = clean_sku[:-(len(fmt_key) + 1)]
            return book_id, fmt_key, is_audio
    raise ValueError(f"Cannot parse SKU '{sku}'. No known format_key found.")


def generate_tts(text, voice_id):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    body = {"text": text, "model_id": ELEVENLABS_MODEL, "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    resp = http_requests.post(url, headers=headers, json=body, timeout=120)
    resp.raise_for_status()
    return resp.content            "pdf_url": pdf_url, "story_text": story_text,
        }
        if is_audio and ELEVENLABS_API_KEY:
            audio_female = generate_tts(story_text, VOICE_FEMALE)
            result["audio_url_female"] = r2_upload(f"orders/{order_id}/narration-female.mp3", audio_female, content_type="audio/mpeg")
            audio_male = generate_tts(story_text, VOICE_MALE)
            result["audio_url_male"] = r2_upload(f"orders/{order_id}/narration-male.mp3", audio_male, content_type="audio/mpeg")
        response = jsonify(result)
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response, 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)                c.roundRect(safe * 0.5, box_y - body_size * 0.3, w_pt - safe, block_h + body_size * 0.6, radius=6, fill=True, stroke=False)
                c.setFont("Helvetica", body_size)
                c.setFillColor(white)
                for j, line in enumerate(lines):
                    c.drawString(safe, box_y + block_h - (j + 1) * body_size * 1.4, line)
            c.showPage()
            os.unlink(crop_tmp.name)
        os.unlink(img_path)
    c.setFillColor(cream)
    c.rect(0, 0, w_pt, h_pt, fill=True, stroke=False)
    c.setFont("Helvetica-Oblique", dedication_size * 0.8)
    c.setFillColor(dark)
    msg = "Made with love"
    tw = c.stringWidth(msg, "Helvetica-Oblique", dedication_size * 0.8)
    c.drawString((w_pt - tw) / 2, h_pt * 0.5, msg)
    c.showPage()
    c.save()
    os.unlink(cover_path)
    return buf.getvalue()

def build_story_text(book, personalization):
    parts = []
    title = replace_placeholders(book.get("title_template", ""), personalization)
    dedication = replace_placeholders(book.get("dedication_template", ""), personalization)
    if title:
        parts.append(title + ".")
    if dedication:
        parts.append(dedication)
    for spread in book.get("spreads", []):
        raw_text = spread.get("text", "")
        if raw_text:
            parts.append(replace_placeholders(raw_text, personalization))
    return " ".join(parts)


def create_pdf(book, book_id, format_key, personalization):
    fmt = FORMATS[format_key]
    w_mm = fmt["width_mm"] + 2 * fmt["bleed_mm"]
    h_mm = fmt["height_mm"] + 2 * fmt["bleed_mm"]
    w_pt = w_mm * mm
    h_pt = h_mm * mm
    bleed = fmt["bleed_mm"] * mm
    safe = fmt["safe_mm"] * mm
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(w_pt, h_pt))
    cream = Color(0.98, 0.96, 0.93)
    dark = Color(0.15, 0.15, 0.15)
    white = Color(1, 1, 1)
    semi_bg = Color(0, 0, 0, alpha=0.45)
    title_size = fmt["width_mm"] * 0.12
    body_size = fmt["width_mm"] * 0.045
    dedication_size = fmt["width_mm"] * 0.06
    title = replace_placeholders(book.get("title_template", ""), personalization)
    dedication = replace_placeholders(book.get("dedication_template", ""), personalization)
    spreads = book.get("spreads", [])
    cover_path = r2_download_to_tempfile(f"books/{book_id}/images/cover.png")
    c.drawImage(cover_path, 0, 0, width=w_pt, height=h_pt, preserveAspectRatio=True, anchor="c")
    c.setFont("Helvetica-Bold", title_size)
    c.setFillColor(white)
    title_y = h_pt * 0.55
    title_lines = wrap_text(title, title_size, w_pt - 2 * safe)
    for i, line in enumerate(title_lines):
        tw = c.stringWidth(line, "Helvetica-Bold", title_size)
        c.drawString((w_pt - tw) / 2, title_y - i * title_size * 1.3, line)
    c.showPage()
    c.setFillColor(cream)
    c.rect(0, 0, w_pt, h_pt, fill=True, stroke=False)
    c.setFont("Helvetica-Oblique", dedication_size)
    c.setFillColor(dark)
    ded_lines = wrap_text(dedication, dedication_size, w_pt - 2 * safe)
    ded_y = h_pt * 0.55
    for i, line in enumerate(ded_lines):
        tw = c.stringWidth(line, "Helvetica-Oblique", dedication_size)
        c.drawString((w_pt - tw) / 2, ded_y - i * dedication_size * 1.4, line)
    c.showPage()
    for spread in spreads:
        spread_num = spread["spread_number"]
        raw_text = spread.get("text", "")
        text = replace_placeholders(raw_text, personalization)
        text_pos = spread.get("text_position", "bottom")
        img_path = r2_download_to_tempfile(f"books/{book_id}/images/spread-{spread_num:02d}.png")
        img = Image.open(img_path)
        img_w, img_h = img.size
        mid = img_w // 2
        for page_idx, crop_box in enumerate([(0, 0, mid, img_h), (mid, 0, img_w, img_h)]):
            cropped = img.crop(crop_box)
            crop_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
            cropped.save(crop_tmp.name)
            c.drawImage(crop_tmp.name, 0, 0, width=w_pt, height=h_pt, preserveAspectRatio=False)
            if page_idx == 1 and text:
                text_lines = wrap_text(text, body_size, w_pt - 2 * safe)
                block_h = len(text_lines) * body_size * 1.4 + body_size
                box_y = (h_pt - safe - block_h) if text_pos == "top" else safe
                c.setFillColor(semi_bg)"""
Personalized Children's Book PDF Generator - Render Web Service
Uses Cloudflare R2 (S3-compatible) for file storage.
Endpoint: POST /generate-pdf
"""

import json
import os
import tempfile
from io import BytesIO

import boto3
import requests as http_requests
from flask import Flask, request, jsonify
from reportlab.lib.pagesizes import mm
from reportlab.lib.colors import Color
from reportlab.pdfgen import canvas
from PIL import Image

app = Flask(__name__)

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY = os.environ.get("R2_ACCESS_KEY", "")
R2_SECRET_KEY = os.environ.get("R2_SECRET_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "glentales-books")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL", "")

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
VOICE_FEMALE = os.environ.get("VOICE_FEMALE", "qlnUbSLa6XkXV9pK52QP")
VOICE_MALE = os.environ.get("VOICE_MALE", "nzFihrBIvB34imQBuxub")
ELEVENLABS_MODEL = "eleven_multilingual_v2"

FORMATS = {
    "softcover-140mm": {"width_mm": 140, "height_mm": 140, "bleed_mm": 3, "safe_mm": 10, "cover_type": "softcover"},
    "softcover-200mm": {"width_mm": 200, "height_mm": 200, "bleed_mm": 3, "safe_mm": 10, "cover_type": "softcover"},
    "hardcover-200mm": {"width_mm": 200, "height_mm": 200, "bleed_mm": 3, "safe_mm": 15, "cover_type": "hardcover"},
    "hardcover-280mm": {"width_mm": 280, "height_mm": 280, "bleed_mm": 3, "safe_mm": 15, "cover_type": "hardcover"},
}


def get_r2_client():
    return boto3.client("s3", endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY, aws_secret_access_key=R2_SECRET_KEY, region_name="auto")


def r2_upload(key, data, content_type="application/octet-stream"):
    client = get_r2_client()
    client.put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=data, ContentType=content_type)
    return f"{R2_PUBLIC_URL}/{key}"


def r2_download(key):
    client = get_r2_client()
    response = client.get_object(Bucket=R2_BUCKET_NAME, Key=key)
    return response["Body"].read()


def r2_download_to_tempfile(key, suffix=".png"):
    data = r2_download(key)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(data)
    tmp.close()
    return tmp.name


def load_book_json(book_id):
    data = r2_download(f"books/{book_id}/book.json")
    return json.loads(data)


def replace_placeholders(text, personalization):
    for key, value in personalization.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def wrap_text(text, font_size, max_width):
    words = text.split()
    lines, current = [], ""
    avg_char_width = font_size * 0.5
    max_chars = int(max_width / avg_char_width)
    for word in words:
        test = f"{current} {word}".strip()
        if len(test) <= max_chars:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def parse_sku(sku):
    is_audio = sku.endswith("-audio")
    clean_sku = sku[:-6] if is_audio else sku
    for fmt_key in sorted(FORMATS.keys(), key=len, reverse=True):
        if clean_sku.endswith(fmt_key):
            book_id = clean_sku[:-(len(fmt_key) + 1)]
            return book_id, fmt_key, is_audio
    raise ValueError(f"Cannot parse SKU '{sku}'. No known format_key found.")


def generate_tts(text, voice_id):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}
    body = {"text": text, "model_id": ELEVENLABS_MODEL, "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    resp = http_requests.post(url, headers=headers, json=body, timeout=120)
    resp.raise_for_status()
    return resp.content


def build_story_text(book, personalization):
    parts = []
    title = replace_placeholders(book.get("title_template", ""), personalization)
    dedication = replace_placeholders(book.get("dedication_template", ""), personalization)
    if title:
        parts.append(title + ".")
    if dedication:
        parts.append(dedication)
    for spread in book.get("spreads", []):
        raw_text = spread.get("text", "")
        if raw_text:
            parts.append(replace_placeholders(raw_text, personalization))
    return " ".join(parts)                c.roundRect(safe * 0.5, box_y - body_size * 0.3, w_pt - safe, block_h + body_size * 0.6, radius=6, fill=True, stroke=False)
                c.setFont("Helvetica", body_size)
                c.setFillColor(white)
                for j, line in enumerate(text_lines):
                    c.drawString(safe, box_y + block_h - (j + 1) * body_size * 1.4, line)
            c.showPage()
            os.unlink(crop_tmp.name)
        os.unlink(img_path)
    c.setFillColor(cream)
    c.rect(0, 0, w_pt, h_pt, fill=True, stroke=False)
    c.setFont("Helvetica-Oblique", dedication_size * 0.8)
    c.setFillColor(dark)
    msg = "Made with love"
    tw = c.stringWidth(msg, "Helvetica-Oblique", dedication_size * 0.8)
    c.drawString((w_pt - tw) / 2, h_pt * 0.5, msg)
    c.showPage()
    c.save()
    os.unlink(cover_path)
    return buf.getvalue()


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "glentales-pdf-generator"})


@app.route("/generate-pdf", methods=["POST", "OPTIONS"])
def generate_pdf():
    if request.method == "OPTIONS":
        return ("", 204, {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers": "Content-Type"})
    try:
        data = request.get_json(silent=True) or {}
        personalization = data.get("personalization", {})
        order_id = data.get("order_id", "unknown")
        sku = data.get("sku")
        is_audio = False
        if sku:
            book_id, format_key, is_audio = parse_sku(sku)
        else:
            book_id = data.get("book_id")
            format_key = data.get("format_key")
        if not book_id:
            return jsonify({"error": "sku or book_id required"}), 400
        if format_key not in FORMATS:
            return jsonify({"error": "Invalid format_key. Must be one of: " + str(list(FORMATS.keys()))}), 400
        if not personalization.get("child_name"):
            return jsonify({"error": "personalization.child_name required"}), 400
        book = load_book_json(book_id)
        pdf_bytes = create_pdf(book, book_id, format_key, personalization)
        pdf_key = "orders/" + order_id + "/" + format_key + ".pdf"
        pdf_url = r2_upload(pdf_key, pdf_bytes, content_type="application/pdf")
        story_text = build_story_text(book, personalization)
        result = {
            "status": "success", "order_id": order_id, "book_id": book_id,
            "format_key": format_key, "sku": sku or (book_id + "-" + format_key),
            "is_audio": is_audio, "child_name": personalization.get("child_name"),
            "pdf_url": pdf_url, "story_text": story_text,
        }
        if is_audio and ELEVENLABS_API_KEY:
            audio_female = generate_tts(story_text, VOICE_FEMALE)
            result["audio_url_female"] = r2_upload("orders/" + order_id + "/narration-female.mp3", audio_female, content_type="audio/mpeg")
            audio_male = generate_tts(story_text, VOICE_MALE)
            result["audio_url_male"] = r2_upload("orders/" + order_id + "/narration-male.mp3", audio_male, content_type="audio/mpeg")
        response = jsonify(result)
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response, 200
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
