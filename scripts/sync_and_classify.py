#!/usr/bin/env python3
"""
sync_and_classify.py — NuBrakes Photo Sync & Classification Pipeline
Phase 2: Duplicate Detection + Best-Photo Scoring + Hero-Image Selection

Workflow:
  1. Scans Google Drive originals/ folder for new images
  2. Downloads each new image to a local temp/ folder
  3. Computes file hash (exact duplicate check) + perceptual hash (near-duplicate check)
  4. Skips exact duplicates; flags near-duplicates and continues
  5. Makes the original publicly accessible
  6. Resizes locally for OpenAI classification
  7. Sends to OpenAI Vision for classification
  8. Computes best_photo_score (1–100) and hero_score (1–100)
  9. Appends full metadata row to Google Sheets
 10. Cleans up temp files

Usage:
  python scripts/sync_and_classify.py
  python scripts/sync_and_classify.py --dry-run    # preview only, no API calls
  python scripts/sync_and_classify.py --limit 10   # process max 10 new images
  python scripts/sync_and_classify.py --model gpt-4o
"""

import os
import re
import sys
import json
import uuid
import time
import base64
import shutil
import hashlib
import argparse
import mimetypes
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone

# ── Third-party ──────────────────────────────────────────────────────────────
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageOps
import imagehash
from tqdm import tqdm
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# ─── Paths ───────────────────────────────────────────────────────────────────
ROOT                 = Path(__file__).parent.parent
SERVICE_ACCOUNT_FILE = ROOT / "credentials" / "google-service-account.json"
TEMP_DIR             = ROOT / "temp"

load_dotenv(ROOT / ".env")

# ─── Environment variables ───────────────────────────────────────────────────
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "")
GOOGLE_SHEET_ID     = os.getenv("GOOGLE_SHEET_ID", "")
ORIGINALS_FOLDER_ID = os.getenv("GOOGLE_ORIGINALS_FOLDER_ID", "")
WEB_FOLDER_ID       = os.getenv("GOOGLE_WEB_FOLDER_ID", "")
THUMBS_FOLDER_ID    = os.getenv("GOOGLE_THUMBS_FOLDER_ID", "")

# ─── Image processing settings ───────────────────────────────────────────────
WEB_MAX_PX    = 1200
THUMB_MAX_PX  = 400
WEB_QUALITY   = 85
THUMB_QUALITY = 80

# ─── Supported file extensions ───────────────────────────────────────────────
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# ─── Google API scopes ───────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

# ─── Near-duplicate threshold ────────────────────────────────────────────────
# Perceptual hash Hamming distance (out of 64 bits).
# <= 5  → very likely near-duplicate
# <= 10 → probably near-duplicate
# >  10 → probably unique
PHASH_THRESHOLD = 10

# ─── Google Sheets headers ───────────────────────────────────────────────────
# Phase 1 core columns — these already exist in the sheet.
# Do NOT reorder — column positions matter for existing data.
PHASE1_HEADERS = [
    "id", "filename", "uploaded_at", "processed_at",
    "original_drive_url", "web_drive_url", "thumbnail_drive_url",
    "original_file_id", "web_file_id", "thumbnail_file_id",
    "technician_name", "market", "category", "tags", "quality_score",
    "marketing_use_case", "hero_candidate", "contains_customer",
    "contains_license_plate", "contains_sensitive_info",
    "recommended_caption", "approval_status",
]

# Phase 2 new columns — added to the right of Phase 1 columns.
# migrate_sheet_headers() safely appends these if they don't exist yet.
PHASE2_HEADERS = [
    "file_hash",          # MD5 of original file bytes
    "perceptual_hash",    # pHash string (imagehash)
    "duplicate_status",   # Unique | Near Duplicate | Exact Duplicate
    "duplicate_of",       # filename of the original if duplicate
    "duplicate_confidence", # 0–100 confidence score
    "duplicate_group_id", # UUID shared by a group of similar photos
    "best_photo_score",   # 1–100 combined marketing asset score
    "quality_tier",       # Excellent | Good | Usable | Low Quality | Do Not Use
    "score_reason",       # plain-English explanation of score
    "recommended_usage",  # short usage recommendation
    "hero_score",         # 1–100 hero-image suitability score
    "hero_reason",        # plain-English explanation of hero score
    "hero_use_case",      # Email Hero | Landing Page Hero | Social Post Hero | Customer.io Campaign | Not Recommended
]

ALL_HEADERS = PHASE1_HEADERS + PHASE2_HEADERS


# ─── OpenAI classification prompt ────────────────────────────────────────────
CLASSIFICATION_PROMPT = """You are classifying technician photos for NuBrakes, a mobile brake repair company.
Analyze this image carefully and return ONLY a JSON object — no preamble, no explanation.

{
  "category": "<one of the categories below>",
  "tags": ["<descriptive tag>", ...],
  "quality_score": <integer 1-10>,
  "marketing_use_case": "<one of the use cases below>",
  "hero_candidate": <true or false>,
  "marketing_ready": <true or false>,
  "contains_customer": <true or false>,
  "contains_license_plate": <true or false>,
  "contains_sensitive_info": <true or false>,
  "recommended_caption": "<short 1-sentence marketing caption>",
  "sharpness": "<sharp|soft|blurry>",
  "lighting": "<excellent|good|poor|very poor>",
  "composition": "<strong|average|weak>"
}

CATEGORIES (pick exactly one):
  Technician Working Shot  — technician actively performing brake work
  Brake Parts              — close-up of brake components (rotor, pads, calipers)
  Before and After         — sequential shots showing repair progress
  Customer Handoff         — technician and customer at service completion
  Mobile Service Setup     — NuBrakes van, tools, or driveway setup
  Vehicle Shot             — car or wheel focus
  Hero Image Candidate     — exceptional composition for marketing hero
  Not Marketing Ready      — poor quality, blurry, too dark, or off-brand
  Other                    — doesn't fit the above

MARKETING USE CASES (pick exactly one):
  hero_image          — strong enough to lead an email campaign banner
  email_body          — good supporting image inside an email
  service_explainer   — clearly shows the brake service process
  testimonial         — great for social proof or customer story sections
  reminder_campaign   — suitable for appointment reminder emails
  do_not_use          — not suitable for any marketing purpose

QUALITY SCORE GUIDE:
  9-10  Sharp, well-lit, professional composition
  7-8   Good quality with minor issues
  5-6   Usable but with noticeable flaws
  3-4   Poor quality, significant problems
  1-2   Unusable

Return ONLY the JSON object."""


# ═════════════════════════════════════════════════════════════════════════════
# Validation
# ═════════════════════════════════════════════════════════════════════════════

def check_env():
    missing = []
    if not OPENAI_API_KEY:        missing.append("OPENAI_API_KEY  (in .env)")
    if not GOOGLE_SHEET_ID:       missing.append("GOOGLE_SHEET_ID  (in .env)")
    if not ORIGINALS_FOLDER_ID:   missing.append("GOOGLE_ORIGINALS_FOLDER_ID  (in .env)")
    if not WEB_FOLDER_ID:         missing.append("GOOGLE_WEB_FOLDER_ID  (in .env)")
    if not THUMBS_FOLDER_ID:      missing.append("GOOGLE_THUMBS_FOLDER_ID  (in .env)")
    if not SERVICE_ACCOUNT_FILE.exists():
        missing.append(f"credentials/google-service-account.json")
    if missing:
        print("\nERROR: Missing required configuration:")
        for item in missing:
            print(f"  ✗  {item}")
        sys.exit(1)
    print("✓ Configuration OK")


# ═════════════════════════════════════════════════════════════════════════════
# Google API helpers
# ═════════════════════════════════════════════════════════════════════════════

def build_google_clients():
    creds  = service_account.Credentials.from_service_account_file(
        str(SERVICE_ACCOUNT_FILE), scopes=SCOPES
    )
    drive  = build("drive",  "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    return drive, sheets


def list_drive_folder(drive, folder_id):
    files, page_token = [], None
    while True:
        resp = drive.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, createdTime)",
            pageToken=page_token,
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def get_sheet_data(sheets):
    """Return all rows from Sheet1 (including headers)."""
    try:
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="Sheet1",
        ).execute()
        return resp.get("values", [])
    except Exception:
        return []


def get_processed_filenames(sheets):
    """Return set of filenames already in the sheet."""
    rows = get_sheet_data(sheets)
    if len(rows) <= 1:
        return set()
    return {row[1] for row in rows[1:] if len(row) > 1}


def migrate_sheet_headers(sheets):
    """
    Safely add any Phase 2 columns that don't exist yet in the sheet header row.
    Existing columns are never moved or removed — new ones are appended to the right.
    This means old rows just have blank values for the new columns, which is fine.
    """
    rows = get_sheet_data(sheets)

    if not rows:
        # Brand new sheet — write all headers
        sheets.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="Sheet1!A1",
            valueInputOption="RAW",
            body={"values": [ALL_HEADERS]},
        ).execute()
        print("✓ Created sheet with all headers (Phase 1 + Phase 2)")
        return

    existing_headers = rows[0]

    # Find which Phase 2 headers are missing
    missing = [h for h in PHASE2_HEADERS if h not in existing_headers]
    if not missing:
        print("✓ Sheet headers already up to date")
        return

    # Append missing headers to the right of the existing ones
    next_col = len(existing_headers)  # 0-indexed
    col_letter = col_index_to_letter(next_col)
    new_range   = f"Sheet1!{col_letter}1"

    sheets.spreadsheets().values().update(
        spreadsheetId=GOOGLE_SHEET_ID,
        range=new_range,
        valueInputOption="RAW",
        body={"values": [missing]},
    ).execute()
    print(f"✓ Added {len(missing)} Phase 2 column(s) to sheet: {', '.join(missing)}")


def col_index_to_letter(index):
    """Convert a 0-based column index to a Sheets column letter (0→A, 25→Z, 26→AA)."""
    result = ""
    index += 1  # 1-based
    while index > 0:
        index, rem = divmod(index - 1, 26)
        result = chr(65 + rem) + result
    return result


def ensure_sheet_headers(sheets):
    """Write the Phase 1 header row if the sheet is empty."""
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="Sheet1!1:1",
    ).execute()
    if not resp.get("values"):
        sheets.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="Sheet1!A1",
            valueInputOption="RAW",
            body={"values": [ALL_HEADERS]},
        ).execute()
        print("✓ Created Google Sheets header row")


def download_drive_file(drive, file_id, dest_path):
    request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def upload_to_drive(drive, local_path, filename, folder_id, make_public=True):
    mime = mimetypes.guess_type(str(local_path))[0] or "image/jpeg"
    with open(local_path, "rb") as f:
        media = MediaIoBaseUpload(BytesIO(f.read()), mimetype=mime, resumable=False)

    file = drive.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()

    file_id = file["id"]

    if make_public:
        try:
            drive.permissions().create(
                fileId=file_id,
                body={"role": "reader", "type": "anyone"},
                supportsAllDrives=True,
            ).execute()
        except Exception:
            pass  # Shared Drive may inherit permissions

    return file_id


def make_file_public(drive, file_id):
    try:
        drive.permissions().create(
            fileId=file_id,
            body={"role": "reader", "type": "anyone"},
            supportsAllDrives=True,
        ).execute()
    except Exception:
        pass


def drive_view_url(file_id, size=None):
    if size:
        return f"https://drive.google.com/thumbnail?id={file_id}&sz={size}"
    return f"https://lh3.googleusercontent.com/d/{file_id}"


def drive_download_url(file_id):
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def append_sheet_row(sheets, headers_in_sheet, row_values_dict):
    """
    Append a row to the sheet.
    headers_in_sheet: the actual header list currently in the sheet (may be longer than PHASE1).
    row_values_dict: dict of {column_name: value} for the new row.
    Values are placed in the correct columns; blanks fill any missing fields.
    """
    row = [str(row_values_dict.get(h, "")) for h in headers_in_sheet]
    sheets.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="Sheet1!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


# ═════════════════════════════════════════════════════════════════════════════
# Image processing
# ═════════════════════════════════════════════════════════════════════════════

def resize_image(src_path, dest_path, max_px, quality):
    with Image.open(src_path) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        img.save(dest_path, "JPEG", quality=quality, optimize=True)
        return img.size


# ═════════════════════════════════════════════════════════════════════════════
# Phase 2 — Duplicate Detection
# ═════════════════════════════════════════════════════════════════════════════

def compute_file_hash(path):
    """
    Compute MD5 hash of raw file bytes.
    Two files with the same hash are byte-for-byte identical (exact duplicates).
    """
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_perceptual_hash(path):
    """
    Compute a perceptual hash (pHash) of the image.
    pHash captures visual content — near-identical images have very similar hashes
    even if file bytes differ (different compression, slight crop, etc.).
    Returns a hex string like 'f8c4a2b1e3d5f789'.
    """
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            return str(imagehash.phash(img))
    except Exception as e:
        print(f"    ⚠ Could not compute perceptual hash: {e}")
        return ""


def load_existing_hashes(sheets):
    """
    Load all file_hash and perceptual_hash values already in the sheet.
    Returns a list of dicts:
      [{ 'filename': '...', 'file_hash': '...', 'perceptual_hash': '...', 'group_id': '...' }, ...]
    Used to compare new photos against existing ones.
    """
    rows = get_sheet_data(sheets)
    if len(rows) < 2:
        return []

    headers = rows[0]

    # Find column positions (may not exist in older sheets)
    def col(name):
        try:
            return headers.index(name)
        except ValueError:
            return None

    fn_col    = col("filename")
    fh_col    = col("file_hash")
    ph_col    = col("perceptual_hash")
    gid_col   = col("duplicate_group_id")

    if fn_col is None:
        return []

    existing = []
    for row in rows[1:]:
        def safe(c):
            return row[c] if c is not None and c < len(row) else ""

        existing.append({
            "filename":        safe(fn_col),
            "file_hash":       safe(fh_col),
            "perceptual_hash": safe(ph_col),
            "group_id":        safe(gid_col),
        })
    return existing


def detect_duplicate(file_hash, phash_str, existing_hashes):
    """
    Compare this image's hashes against all previously processed images.

    Returns a dict:
      {
        "duplicate_status":     "Unique" | "Near Duplicate" | "Exact Duplicate",
        "duplicate_of":         filename of match (or ""),
        "duplicate_confidence": 0–100,
        "duplicate_group_id":   shared UUID for the group (or new UUID if unique),
      }
    """
    # ── Exact duplicate check ─────────────────────────────────
    for rec in existing_hashes:
        if rec["file_hash"] and rec["file_hash"] == file_hash:
            print(f"    🔴 EXACT DUPLICATE of {rec['filename']}")
            return {
                "duplicate_status":     "Exact Duplicate",
                "duplicate_of":         rec["filename"],
                "duplicate_confidence": 100,
                "duplicate_group_id":   rec["group_id"] or str(uuid.uuid4()),
            }

    # ── Near-duplicate check ──────────────────────────────────
    if phash_str:
        try:
            this_hash = imagehash.hex_to_hash(phash_str)
        except Exception:
            this_hash = None

        if this_hash is not None:
            best_distance = None
            best_match    = None

            for rec in existing_hashes:
                if not rec["perceptual_hash"]:
                    continue
                try:
                    other_hash = imagehash.hex_to_hash(rec["perceptual_hash"])
                    distance   = this_hash - other_hash  # Hamming distance (0–64)
                    if best_distance is None or distance < best_distance:
                        best_distance = distance
                        best_match    = rec
                except Exception:
                    continue

            if best_distance is not None and best_distance <= PHASH_THRESHOLD:
                # Confidence: 100 at distance 0, scales down to ~85 at threshold
                confidence = int(100 - (best_distance / PHASH_THRESHOLD) * 15)
                print(f"    🟡 NEAR DUPLICATE of {best_match['filename']} "
                      f"(Hamming={best_distance}, confidence={confidence}%)")
                return {
                    "duplicate_status":     "Near Duplicate",
                    "duplicate_of":         best_match["filename"],
                    "duplicate_confidence": confidence,
                    "duplicate_group_id":   best_match["group_id"] or str(uuid.uuid4()),
                }

    # ── Unique ────────────────────────────────────────────────
    print(f"    🟢 UNIQUE image")
    return {
        "duplicate_status":     "Unique",
        "duplicate_of":         "",
        "duplicate_confidence": 0,
        "duplicate_group_id":   str(uuid.uuid4()),
    }


# ═════════════════════════════════════════════════════════════════════════════
# Phase 2 — Best-Photo Scoring
# ═════════════════════════════════════════════════════════════════════════════

def compute_best_photo_score(classification, duplicate_status):
    """
    Compute a combined marketing asset score from 1 to 100.
    The logic is intentionally transparent and easy to edit.

    Base:   quality_score × 10  (OpenAI gives 1–10, so this gives 10–100)
    Bonus:  up to +25 for great content and marketing potential
    Penalty: up to -40 for flags, duplicates, bad technical quality
    """
    quality = classification.get("quality_score", 5)
    score   = quality * 10   # base: 10–100

    reasons = []

    # ── Category bonuses ──────────────────────────────────────
    cat = classification.get("category", "")
    if cat == "Hero Image Candidate":
        score += 10; reasons.append("+10 hero-quality composition")
    elif cat == "Technician Working Shot":
        score += 7;  reasons.append("+7 technician working shot")
    elif cat == "Customer Handoff":
        score += 7;  reasons.append("+7 customer handoff")
    elif cat == "Mobile Service Setup":
        score += 5;  reasons.append("+5 mobile service setup")
    elif cat == "Brake Parts":
        score += 4;  reasons.append("+4 brake parts close-up")
    elif cat == "Not Marketing Ready":
        score -= 15; reasons.append("-15 not marketing ready")

    # ── Hero candidate bonus ──────────────────────────────────
    if classification.get("hero_candidate"):
        score += 8; reasons.append("+8 hero candidate")

    # ── Marketing use case bonus/penalty ─────────────────────
    use_case = classification.get("marketing_use_case", "")
    if use_case == "hero_image":
        score += 8;  reasons.append("+8 hero image use case")
    elif use_case in ("email_body", "service_explainer"):
        score += 5;  reasons.append("+5 strong email/explainer use")
    elif use_case == "testimonial":
        score += 4;  reasons.append("+4 testimonial use case")
    elif use_case == "do_not_use":
        score -= 20; reasons.append("-20 marked do_not_use")

    # ── Technical quality bonuses ─────────────────────────────
    sharpness   = classification.get("sharpness", "")
    lighting    = classification.get("lighting", "")
    composition = classification.get("composition", "")

    if sharpness == "sharp":
        score += 3; reasons.append("+3 sharp image")
    elif sharpness == "blurry":
        score -= 8; reasons.append("-8 blurry image")

    if lighting == "excellent":
        score += 3; reasons.append("+3 excellent lighting")
    elif lighting == "poor":
        score -= 5; reasons.append("-5 poor lighting")
    elif lighting == "very poor":
        score -= 10; reasons.append("-10 very poor lighting")

    if composition == "strong":
        score += 3; reasons.append("+3 strong composition")
    elif composition == "weak":
        score -= 5; reasons.append("-5 weak composition")

    # ── Safety / sensitivity penalties ───────────────────────
    if classification.get("contains_sensitive_info"):
        score -= 15; reasons.append("-15 contains sensitive info")
    if classification.get("contains_license_plate"):
        score -= 10; reasons.append("-10 license plate visible")

    # ── Duplicate penalty ─────────────────────────────────────
    if duplicate_status == "Exact Duplicate":
        score -= 25; reasons.append("-25 exact duplicate")
    elif duplicate_status == "Near Duplicate":
        score -= 10; reasons.append("-10 near duplicate")

    # ── Clamp to 1–100 ────────────────────────────────────────
    score = max(1, min(100, score))
    reason_str = "; ".join(reasons) if reasons else "base score only"

    return score, reason_str


def determine_quality_tier(score):
    """Map best_photo_score to a human-readable quality tier."""
    if score >= 85: return "Excellent"
    if score >= 70: return "Good"
    if score >= 50: return "Usable"
    if score >= 30: return "Low Quality"
    return "Do Not Use"


def determine_recommended_usage(quality_tier, use_case):
    """Suggest how marketing should use this photo."""
    if quality_tier == "Excellent":
        return "Hero image, email banner, landing page"
    if quality_tier == "Good":
        return "Email body, service explainer, social"
    if quality_tier == "Usable":
        if use_case == "reminder_campaign":
            return "Reminder campaigns, secondary email"
        return "Secondary email, blog, internal use"
    if quality_tier == "Low Quality":
        return "Internal reference only"
    return "Do not use in marketing"


# ═════════════════════════════════════════════════════════════════════════════
# Phase 2 — Hero-Image Selection
# ═════════════════════════════════════════════════════════════════════════════

def compute_hero_score(classification):
    """
    Compute a hero-image suitability score from 1 to 100.

    A great hero image is:
    - High quality and sharp
    - Visually strong with clear subject
    - Clean, uncluttered composition
    - Shows technician working, van setup, or customer handoff
    - No sensitive info or license plates
    """
    quality = classification.get("quality_score", 5)
    score   = quality * 8   # base: 8–80 (leaves room for bonuses)

    reasons = []

    # ── Hero candidate flag ───────────────────────────────────
    if classification.get("hero_candidate"):
        score += 20; reasons.append("+20 marked hero candidate by AI")

    # ── Category bonuses ──────────────────────────────────────
    cat = classification.get("category", "")
    if cat == "Hero Image Candidate":
        score += 15; reasons.append("+15 hero image category")
    elif cat == "Customer Handoff":
        score += 10; reasons.append("+10 customer handoff")
    elif cat == "Mobile Service Setup":
        score += 9;  reasons.append("+9 mobile service setup")
    elif cat == "Technician Working Shot":
        score += 8;  reasons.append("+8 technician working shot")
    elif cat == "Brake Parts":
        score += 3;  reasons.append("+3 brake parts")
    elif cat == "Not Marketing Ready":
        score -= 20; reasons.append("-20 not marketing ready")

    # ── Use case bonus ────────────────────────────────────────
    use_case = classification.get("marketing_use_case", "")
    if use_case == "hero_image":
        score += 12; reasons.append("+12 hero_image use case")
    elif use_case == "email_body":
        score += 5;  reasons.append("+5 email_body use case")

    # ── Technical quality ─────────────────────────────────────
    sharpness   = classification.get("sharpness", "")
    lighting    = classification.get("lighting", "")
    composition = classification.get("composition", "")

    if sharpness == "sharp":
        score += 5; reasons.append("+5 sharp")
    elif sharpness == "blurry":
        score -= 15; reasons.append("-15 blurry (disqualifying for hero)")

    if lighting == "excellent":
        score += 5; reasons.append("+5 excellent lighting")
    elif lighting == "poor":
        score -= 8; reasons.append("-8 poor lighting")
    elif lighting == "very poor":
        score -= 15; reasons.append("-15 very poor lighting")

    if composition == "strong":
        score += 5; reasons.append("+5 strong composition")
    elif composition == "weak":
        score -= 8; reasons.append("-8 weak composition")

    # ── Disqualifying penalties ───────────────────────────────
    if classification.get("contains_sensitive_info"):
        score -= 25; reasons.append("-25 sensitive info (disqualifying)")
    if classification.get("contains_license_plate"):
        score -= 20; reasons.append("-20 license plate visible")
    if classification.get("contains_customer"):
        score -= 5;  reasons.append("-5 customer visible (consent needed)")

    # ── Clamp to 1–100 ────────────────────────────────────────
    score = max(1, min(100, score))
    reason_str = "; ".join(reasons) if reasons else "base score only"

    return score, reason_str


def determine_hero_use_case(hero_score, classification):
    """
    Assign a hero use case based on hero_score and photo content.
    >= 80 → strong hero candidate → pick best channel
    60–79 → possible hero → secondary uses
    < 60  → not recommended as hero
    """
    if hero_score < 60:
        return "Not Recommended"

    use_case = classification.get("marketing_use_case", "")
    cat      = classification.get("category", "")

    if hero_score >= 80:
        if use_case == "hero_image" or cat == "Hero Image Candidate":
            return "Email Hero"
        if cat in ("Customer Handoff", "Mobile Service Setup"):
            return "Landing Page Hero"
        if cat == "Technician Working Shot":
            return "Customer.io Campaign"
        return "Social Post Hero"
    else:
        # 60–79: possible hero
        return "Social Post Hero"


# ═════════════════════════════════════════════════════════════════════════════
# OpenAI Vision classification
# ═════════════════════════════════════════════════════════════════════════════

def classify_image(openai_client, img_path, model="gpt-4o-mini"):
    media_type_map = {
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png":  "image/png",
        ".webp": "image/webp",
    }
    media_type = media_type_map.get(Path(img_path).suffix.lower(), "image/jpeg")

    with open(img_path, "rb") as f:
        image_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

    response = openai_client.chat.completions.create(
        model=model,
        max_tokens=700,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{image_b64}",
                        "detail": "low",
                    },
                },
                {"type": "text", "text": CLASSIFICATION_PROMPT},
            ],
        }],
    )

    text  = response.choices[0].message.content.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON in OpenAI response: {text[:200]}")

    return json.loads(match.group())


# ═════════════════════════════════════════════════════════════════════════════
# Filename parsing
# ═════════════════════════════════════════════════════════════════════════════

def extract_from_filename(filename):
    stem  = Path(filename).stem
    parts = stem.split("_")
    start = 1 if (len(parts[0]) == 10 and parts[0].count("-") == 2) else 0
    market     = parts[start].replace("-", " ").title()     if len(parts) > start     else ""
    technician = parts[start + 1].replace("-", " ").title() if len(parts) > start + 1 else ""
    return market, technician


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="NuBrakes Photo Sync & Classify — Phase 2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/sync_and_classify.py
  python scripts/sync_and_classify.py --dry-run
  python scripts/sync_and_classify.py --limit 5 --model gpt-4o
""",
    )
    parser.add_argument("--dry-run",  action="store_true",
                        help="List new images without processing them")
    parser.add_argument("--limit",    type=int, default=0, metavar="N",
                        help="Process at most N new images (0 = unlimited)")
    parser.add_argument("--model",    default="gpt-4o-mini",
                        help="OpenAI vision model (default: gpt-4o-mini)")
    return parser.parse_args()


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════

def main():
    args = parse_args()

    print("\nNuBrakes Photo Sync & Classify — Phase 2")
    print("=" * 46)
    print(f"Model : {args.model}")
    if args.dry_run:
        print("Mode  : DRY RUN (no changes will be made)")
    print()

    check_env()

    TEMP_DIR.mkdir(exist_ok=True)

    # ── Connect to Google ─────────────────────────────────────
    print("\nConnecting to Google APIs…")
    try:
        drive, sheets = build_google_clients()
        print("✓ Google Drive and Sheets connected")
    except Exception as exc:
        print(f"ERROR: Google authentication failed: {exc}")
        sys.exit(1)

    # ── Migrate sheet headers (safely adds Phase 2 columns) ───
    print("\nChecking sheet headers…")
    ensure_sheet_headers(sheets)
    migrate_sheet_headers(sheets)

    # ── Load current headers from sheet ──────────────────────
    sheet_rows       = get_sheet_data(sheets)
    headers_in_sheet = sheet_rows[0] if sheet_rows else ALL_HEADERS

    # ── Load existing hashes for duplicate detection ──────────
    print("Loading existing photo hashes for duplicate detection…")
    existing_hashes = load_existing_hashes(sheets)
    print(f"  {len(existing_hashes)} existing photo(s) loaded")

    # ── Scan originals folder ─────────────────────────────────
    print(f"\nScanning originals folder…")
    try:
        all_files = list_drive_folder(drive, ORIGINALS_FOLDER_ID)
    except Exception as exc:
        print(f"ERROR: Could not read Drive folder: {exc}")
        sys.exit(1)

    image_files = [
        f for f in all_files
        if Path(f["name"]).suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    print(f"Found {len(image_files)} image(s) in originals/")

    processed  = get_processed_filenames(sheets)
    new_files  = [f for f in image_files if f["name"] not in processed]
    print(f"Already processed : {len(processed)}")
    print(f"New to process    : {len(new_files)}")

    if not new_files:
        print("\n✓ All caught up — no new images to process.")
        return

    if args.limit:
        new_files = new_files[: args.limit]
        print(f"Limiting to {args.limit} image(s) (--limit)")

    if args.dry_run:
        print(f"\n[DRY RUN] Would process {len(new_files)} image(s):")
        for f in new_files:
            print(f"  {f['name']}")
        print("\nDry run complete — no changes made.")
        return

    # ── Process each new image ────────────────────────────────
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    success = errors = exact_dups = near_dups = 0

    for drive_file in tqdm(new_files, desc="Processing", unit="img"):
        filename   = drive_file["name"]
        file_id    = drive_file["id"]
        stem       = Path(filename).stem
        temp_orig  = TEMP_DIR / filename
        temp_web   = TEMP_DIR / f"{stem}_web.jpg"
        temp_thumb = TEMP_DIR / f"{stem}_thumb.jpg"

        tqdm.write(f"\n→ {filename}")

        try:
            # ── 1. Download original ──────────────────────────
            download_drive_file(drive, file_id, temp_orig)

            # ── 2. Compute hashes ─────────────────────────────
            file_hash = compute_file_hash(temp_orig)
            phash_str = compute_perceptual_hash(temp_orig)
            tqdm.write(f"   file_hash={file_hash[:8]}… phash={phash_str}")

            # ── 3. Duplicate detection ────────────────────────
            dup_info = detect_duplicate(file_hash, phash_str, existing_hashes)
            dup_status = dup_info["duplicate_status"]

            if dup_status == "Exact Duplicate":
                exact_dups += 1
                # Skip OpenAI for exact duplicates — record to sheet and move on
                tqdm.write(f"   ⏭ Skipping OpenAI (exact duplicate of {dup_info['duplicate_of']})")

                now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                uploaded_at = (drive_file.get("createdTime") or "")[:10]
                market, technician = extract_from_filename(filename)

                # Make original public so gallery can load it
                make_file_public(drive, file_id)
                orig_url = drive_view_url(file_id)

                row_dict = {
                    "id": str(uuid.uuid4()), "filename": filename,
                    "uploaded_at": uploaded_at, "processed_at": now,
                    "original_drive_url": orig_url,
                    "web_drive_url": orig_url, "thumbnail_drive_url": orig_url,
                    "original_file_id": file_id, "web_file_id": file_id,
                    "thumbnail_file_id": file_id,
                    "technician_name": technician, "market": market,
                    "category": "Exact Duplicate", "tags": "",
                    "quality_score": "0", "marketing_use_case": "do_not_use",
                    "hero_candidate": "False", "contains_customer": "False",
                    "contains_license_plate": "False", "contains_sensitive_info": "False",
                    "recommended_caption": "", "approval_status": "rejected",
                    "file_hash": file_hash, "perceptual_hash": phash_str,
                    "duplicate_status": dup_status,
                    "duplicate_of": dup_info["duplicate_of"],
                    "duplicate_confidence": str(dup_info["duplicate_confidence"]),
                    "duplicate_group_id": dup_info["duplicate_group_id"],
                    "best_photo_score": "1", "quality_tier": "Do Not Use",
                    "score_reason": "Exact duplicate",
                    "recommended_usage": "Do not use in marketing",
                    "hero_score": "1", "hero_reason": "Exact duplicate",
                    "hero_use_case": "Not Recommended",
                }
                append_sheet_row(sheets, headers_in_sheet, row_dict)

                # Add to existing_hashes so subsequent photos compare against this one
                existing_hashes.append({
                    "filename": filename, "file_hash": file_hash,
                    "perceptual_hash": phash_str,
                    "group_id": dup_info["duplicate_group_id"],
                })
                success += 1
                continue

            elif dup_status == "Near Duplicate":
                near_dups += 1
                tqdm.write(f"   ⚠ Near duplicate — still classifying with OpenAI")

            # ── 4. Make original public ───────────────────────
            make_file_public(drive, file_id)

            # ── 5. Resize ─────────────────────────────────────
            resize_image(temp_orig, temp_web,   WEB_MAX_PX,   WEB_QUALITY)
            resize_image(temp_orig, temp_thumb, THUMB_MAX_PX, THUMB_QUALITY)

            # ── 6. Upload web + thumb to Drive ────────────────
            web_file_id   = upload_to_drive(drive, temp_web,   f"{stem}_web.jpg",   WEB_FOLDER_ID)
            thumb_file_id = upload_to_drive(drive, temp_thumb, f"{stem}_thumb.jpg", THUMBS_FOLDER_ID)

            orig_url  = drive_view_url(file_id)
            web_url   = drive_view_url(web_file_id,   size="w1200")
            thumb_url = drive_view_url(thumb_file_id, size="w400")

            # ── 7. Classify with OpenAI ───────────────────────
            classification = classify_image(openai_client, temp_web, model=args.model)
            tqdm.write(f"   category={classification.get('category')} "
                       f"quality={classification.get('quality_score')} "
                       f"hero={classification.get('hero_candidate')}")

            # ── 8. Compute scores ─────────────────────────────
            best_score, score_reason = compute_best_photo_score(classification, dup_status)
            quality_tier             = determine_quality_tier(best_score)
            recommended_usage        = determine_recommended_usage(
                                           quality_tier,
                                           classification.get("marketing_use_case", "")
                                       )
            hero_score, hero_reason  = compute_hero_score(classification)
            hero_use_case            = determine_hero_use_case(hero_score, classification)

            tqdm.write(f"   best_photo_score={best_score} ({quality_tier}) "
                       f"hero_score={hero_score} ({hero_use_case})")

            # ── 9. Build sheet row ────────────────────────────
            now         = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            uploaded_at = (drive_file.get("createdTime") or "")[:10]
            market, technician = extract_from_filename(filename)
            tags_str    = ", ".join(classification.get("tags", []))

            row_dict = {
                "id": str(uuid.uuid4()), "filename": filename,
                "uploaded_at": uploaded_at, "processed_at": now,
                "original_drive_url": orig_url,
                "web_drive_url": web_url, "thumbnail_drive_url": thumb_url,
                "original_file_id": file_id,
                "web_file_id": web_file_id, "thumbnail_file_id": thumb_file_id,
                "technician_name": technician, "market": market,
                "category": classification.get("category", "Other"),
                "tags": tags_str,
                "quality_score": str(classification.get("quality_score", 5)),
                "marketing_use_case": classification.get("marketing_use_case", ""),
                "hero_candidate": str(classification.get("hero_candidate", False)),
                "contains_customer": str(classification.get("contains_customer", False)),
                "contains_license_plate": str(classification.get("contains_license_plate", False)),
                "contains_sensitive_info": str(classification.get("contains_sensitive_info", False)),
                "recommended_caption": classification.get("recommended_caption", ""),
                "approval_status": "pending",
                # Phase 2 fields
                "file_hash": file_hash, "perceptual_hash": phash_str,
                "duplicate_status": dup_status,
                "duplicate_of": dup_info["duplicate_of"],
                "duplicate_confidence": str(dup_info["duplicate_confidence"]),
                "duplicate_group_id": dup_info["duplicate_group_id"],
                "best_photo_score": str(best_score),
                "quality_tier": quality_tier,
                "score_reason": score_reason,
                "recommended_usage": recommended_usage,
                "hero_score": str(hero_score),
                "hero_reason": hero_reason,
                "hero_use_case": hero_use_case,
            }
            append_sheet_row(sheets, headers_in_sheet, row_dict)

            # Add to existing_hashes for the rest of this run's duplicate checks
            existing_hashes.append({
                "filename": filename, "file_hash": file_hash,
                "perceptual_hash": phash_str,
                "group_id": dup_info["duplicate_group_id"],
            })
            success += 1

        except Exception as exc:
            tqdm.write(f"  ✗ ERROR — {filename}: {exc}")
            errors += 1

        finally:
            for tmp in [temp_orig, temp_web, temp_thumb]:
                if tmp.exists():
                    tmp.unlink()

        time.sleep(0.3)

    # ── Summary ───────────────────────────────────────────────
    print(f"\n{'=' * 46}")
    print("Done!")
    print(f"  ✓ Processed     : {success}")
    print(f"  🔴 Exact dups   : {exact_dups}")
    print(f"  🟡 Near dups    : {near_dups}")
    if errors:
        print(f"  ✗ Errors       : {errors}")
    print(f"\nNext step:")
    print(f"  python scripts/generate_gallery_data.py")


if __name__ == "__main__":
    main()
