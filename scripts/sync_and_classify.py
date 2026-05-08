#!/usr/bin/env python3
"""
sync_and_classify.py — NuBrakes Photo Sync & Classification Pipeline
Phase 1: Google Drive + Google Sheets + OpenAI Vision

Workflow:
  1. Scans Google Drive originals/ folder for new images
  2. Downloads each new image to a local temp/ folder
  3. Makes the original publicly accessible (viewer link)
  4. Resizes locally for OpenAI classification (no quota used)
  5. Sends the image to OpenAI Vision for classification
  6. Appends a metadata row to Google Sheets
  7. Cleans up temp files

Note: Service accounts have no Drive storage quota, so we don't upload
web/thumb copies. Instead, all three URLs point to the same original file.

Usage:
  python scripts/sync_and_classify.py
  python scripts/sync_and_classify.py --dry-run    # preview only, no API calls
  python scripts/sync_and_classify.py --limit 10   # process max 10 new images
  python scripts/sync_and_classify.py --model gpt-4o  # use higher-accuracy model
"""

import os
import re
import sys
import json
import uuid
import time
import base64
import shutil
import argparse
import mimetypes
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone

# ── Third-party ──────────────────────────────────────────────────────────────
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageOps
from tqdm import tqdm
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# ─── Paths ───────────────────────────────────────────────────────────────────
# ROOT is the project root (one level above scripts/)
ROOT                 = Path(__file__).parent.parent
SERVICE_ACCOUNT_FILE = ROOT / "credentials" / "google-service-account.json"
TEMP_DIR             = ROOT / "temp"

# Load .env from project root
load_dotenv(ROOT / ".env")

# ─── Environment variables ───────────────────────────────────────────────────
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "")
GOOGLE_SHEET_ID     = os.getenv("GOOGLE_SHEET_ID", "")
ORIGINALS_FOLDER_ID = os.getenv("GOOGLE_ORIGINALS_FOLDER_ID", "")
WEB_FOLDER_ID       = os.getenv("GOOGLE_WEB_FOLDER_ID", "")
THUMBS_FOLDER_ID    = os.getenv("GOOGLE_THUMBS_FOLDER_ID", "")

# ─── Image processing settings ───────────────────────────────────────────────
WEB_MAX_PX    = 1200   # longest side of web version (pixels)
THUMB_MAX_PX  = 400    # longest side of thumbnail (pixels)
WEB_QUALITY   = 85     # JPEG quality for web images
THUMB_QUALITY = 80     # JPEG quality for thumbnails

# ─── Supported file extensions ───────────────────────────────────────────────
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# ─── Google API scopes ───────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

# ─── Google Sheets column headers ────────────────────────────────────────────
# ORDER MATTERS — matches the row values we build later
SHEET_HEADERS = [
    "id",
    "filename",
    "uploaded_at",
    "processed_at",
    "original_drive_url",
    "web_drive_url",
    "thumbnail_drive_url",
    "original_file_id",
    "web_file_id",
    "thumbnail_file_id",
    "technician_name",
    "market",
    "category",
    "tags",
    "quality_score",
    "marketing_use_case",
    "hero_candidate",
    "contains_customer",
    "contains_license_plate",
    "contains_sensitive_info",
    "recommended_caption",
    "approval_status",
]

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
  "recommended_caption": "<short 1-sentence marketing caption>"
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

IMPORTANT:
  - contains_customer: true if any person other than the technician is visible
  - contains_license_plate: true if any vehicle plate number is readable
  - contains_sensitive_info: true if personal documents, addresses, or private info visible
  - hero_candidate: true only for exceptional, professionally composed images
  - recommended_caption: write as if for an email subject or social post (max 12 words)

Return ONLY the JSON object."""


# ═════════════════════════════════════════════════════════════════════════════
# Validation
# ═════════════════════════════════════════════════════════════════════════════

def check_env():
    """Validate all required environment variables and files before starting."""
    missing = []
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY  (in .env)")
    if not GOOGLE_SHEET_ID:
        missing.append("GOOGLE_SHEET_ID  (in .env)")
    if not ORIGINALS_FOLDER_ID:
        missing.append("GOOGLE_ORIGINALS_FOLDER_ID  (in .env)")
    if not WEB_FOLDER_ID:
        missing.append("GOOGLE_WEB_FOLDER_ID  (in .env)")
    if not THUMBS_FOLDER_ID:
        missing.append("GOOGLE_THUMBS_FOLDER_ID  (in .env)")
    if not SERVICE_ACCOUNT_FILE.exists():
        missing.append(
            f"credentials/google-service-account.json  "
            f"(not found at {SERVICE_ACCOUNT_FILE})"
        )
    if missing:
        print("\nERROR: Missing required configuration:")
        for item in missing:
            print(f"  ✗  {item}")
        print("\nSee .env.example and README.md for setup instructions.")
        sys.exit(1)
    print("✓ Configuration OK")


# ═════════════════════════════════════════════════════════════════════════════
# Google API helpers
# ═════════════════════════════════════════════════════════════════════════════

def build_google_clients():
    """Authenticate with the service account and return (drive, sheets) clients."""
    creds  = service_account.Credentials.from_service_account_file(
        str(SERVICE_ACCOUNT_FILE), scopes=SCOPES
    )
    drive  = build("drive",  "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    return drive, sheets


def list_drive_folder(drive, folder_id):
    """Return all non-trashed files in a Google Drive folder (handles pagination)."""
    files      = []
    page_token = None
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


def get_processed_filenames(sheets):
    """
    Return a set of filenames already recorded in Google Sheets.
    Used to skip images we've already processed.
    """
    try:
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="Sheet1!A:B",   # id + filename columns only
        ).execute()
        rows = resp.get("values", [])
        if len(rows) <= 1:
            return set()   # empty or headers only
        # rows[0] = header row, rows[1:] = data
        return {row[1] for row in rows[1:] if len(row) > 1}
    except Exception:
        return set()


def ensure_sheet_headers(sheets):
    """Write the header row to the sheet if it doesn't exist yet."""
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="Sheet1!1:1",
    ).execute()
    if not resp.get("values"):
        sheets.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="Sheet1!A1",
            valueInputOption="RAW",
            body={"values": [SHEET_HEADERS]},
        ).execute()
        print("✓ Created Google Sheets header row")


def download_drive_file(drive, file_id, dest_path):
    """Download a file from Google Drive to a local path."""
    request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def upload_to_drive(drive, local_path, filename, folder_id, make_public=True):
    """
    Upload a local file to a Google Drive folder (supports Shared Drives).
    If make_public=True, grants 'anyone with link can view' access.
    Returns the Google Drive file_id of the uploaded file.
    """
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
            pass  # Shared Drive may already inherit permissions

    return file_id


def make_file_public(drive, file_id):
    """Grant 'anyone with the link can view' access to an existing Drive file."""
    try:
        drive.permissions().create(
            fileId=file_id,
            body={"role": "reader", "type": "anyone"},
            supportsAllDrives=True,
        ).execute()
    except Exception:
        pass


def drive_view_url(file_id, size=None):
    """
    Return a public image URL for a Google Drive file that works in <img> tags.
    Uses the thumbnail API which is more reliable than the uc?id= redirect.
    size: e.g. 'w400' for thumbnail, 'w1200' for web. None = full size.
    """
    if size:
        return f"https://drive.google.com/thumbnail?id={file_id}&sz={size}"
    return f"https://lh3.googleusercontent.com/d/{file_id}"


def drive_download_url(file_id):
    """Return a direct download URL for a Google Drive file."""
    return f"https://drive.google.com/uc?export=download&id={file_id}"


def append_sheet_row(sheets, row_values):
    """Append a single data row to Google Sheets."""
    sheets.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="Sheet1!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row_values]},
    ).execute()


# ═════════════════════════════════════════════════════════════════════════════
# Image processing
# ═════════════════════════════════════════════════════════════════════════════

def resize_image(src_path, dest_path, max_px, quality):
    """
    Resize an image so its longest dimension is max_px.
    - Preserves aspect ratio
    - Corrects EXIF rotation (fixes upside-down phone photos)
    - Converts to RGB JPEG
    Returns (width, height) of the output image.
    """
    with Image.open(src_path) as img:
        img = ImageOps.exif_transpose(img)   # fix phone rotation
        img = img.convert("RGB")             # ensure RGB (handles PNG with alpha)
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        img.save(dest_path, "JPEG", quality=quality, optimize=True)
        return img.size


# ═════════════════════════════════════════════════════════════════════════════
# OpenAI Vision classification
# ═════════════════════════════════════════════════════════════════════════════

def classify_image(openai_client, img_path, model="gpt-4o-mini"):
    """
    Send an image to OpenAI Vision and return a parsed classification dict.
    Falls back to a safe defaults dict if classification fails.
    """
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
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{image_b64}",
                        "detail": "low",   # faster + cheaper; enough for classification
                    },
                },
                {"type": "text", "text": CLASSIFICATION_PROMPT},
            ],
        }],
    )

    text = response.choices[0].message.content.strip()

    # Extract the JSON block even if the model adds extra text
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON in OpenAI response: {text[:200]}")

    result = json.loads(match.group())
    return result


# ═════════════════════════════════════════════════════════════════════════════
# Filename parsing
# ═════════════════════════════════════════════════════════════════════════════

def extract_from_filename(filename):
    """
    Try to extract market and technician name from a structured filename.
    Expected format: YYYY-MM-DD_market_techname_anything.ext
    Example: 2026-05-08_dallas_marcus-j_brakepads_001.jpg
             → market='Dallas', technician='Marcus J'
    Returns ('', '') if the filename doesn't match the expected format.
    """
    stem  = Path(filename).stem
    parts = stem.split("_")

    # Detect if first segment is a date (YYYY-MM-DD)
    start = 1 if (len(parts[0]) == 10 and parts[0].count("-") == 2) else 0

    market     = parts[start].replace("-", " ").title()     if len(parts) > start     else ""
    technician = parts[start + 1].replace("-", " ").title() if len(parts) > start + 1 else ""
    return market, technician


# ═════════════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="NuBrakes Photo Sync & Classify — Phase 1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/sync_and_classify.py
  python scripts/sync_and_classify.py --dry-run
  python scripts/sync_and_classify.py --limit 5 --model gpt-4o
""",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List new images without downloading, processing, or classifying",
    )
    parser.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="Process at most N new images (0 = unlimited)",
    )
    parser.add_argument(
        "--model", default="gpt-4o-mini",
        help="OpenAI vision model (default: gpt-4o-mini)",
    )
    return parser.parse_args()


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════

def main():
    args = parse_args()

    print("\nNuBrakes Photo Sync & Classify")
    print("=" * 42)
    print(f"Model : {args.model}")
    if args.dry_run:
        print("Mode  : DRY RUN (no changes will be made)")
    print()

    # ── Validate config ──────────────────────────────────────
    check_env()

    # ── Prepare temp folder ──────────────────────────────────
    TEMP_DIR.mkdir(exist_ok=True)

    # ── Connect to Google ─────────────────────────────────────
    print("\nConnecting to Google APIs…")
    try:
        drive, sheets = build_google_clients()
        print("✓ Google Drive and Sheets connected")
    except Exception as exc:
        print(f"ERROR: Google authentication failed: {exc}")
        print("  Check your credentials/google-service-account.json file.")
        sys.exit(1)

    # ── Ensure sheet is ready ─────────────────────────────────
    ensure_sheet_headers(sheets)

    # ── Scan originals folder ─────────────────────────────────
    print(f"\nScanning originals folder…")
    try:
        all_files = list_drive_folder(drive, ORIGINALS_FOLDER_ID)
    except Exception as exc:
        print(f"ERROR: Could not read Drive folder: {exc}")
        print("  Make sure the service account has access to the folder.")
        sys.exit(1)

    # Filter to supported image types
    image_files = [
        f for f in all_files
        if Path(f["name"]).suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    print(f"Found {len(image_files)} image(s) in originals/")

    # ── Skip already-processed files ─────────────────────────
    processed  = get_processed_filenames(sheets)
    new_files  = [f for f in image_files if f["name"] not in processed]
    print(f"Already processed : {len(processed)}")
    print(f"New to process    : {len(new_files)}")

    if not new_files:
        print("\n✓ All caught up — no new images to process.")
        return

    # Apply optional limit
    if args.limit:
        new_files = new_files[: args.limit]
        print(f"Limiting to {args.limit} image(s) (--limit)")

    # ── Dry run: just list what would be processed ────────────
    if args.dry_run:
        print(f"\n[DRY run] Would process {len(new_files)} image(s):")
        for f in new_files:
            print(f"  {f['name']}")
        print("\nDry run complete — no changes made.")
        return

    # ── Process each new image ────────────────────────────────
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
    success = 0
    errors  = 0

    for drive_file in tqdm(new_files, desc="Processing", unit="img"):
        filename   = drive_file["name"]
        file_id    = drive_file["id"]
        stem       = Path(filename).stem
        temp_orig  = TEMP_DIR / filename
        temp_web   = TEMP_DIR / f"{stem}_web.jpg"
        temp_thumb = TEMP_DIR / f"{stem}_thumb.jpg"

        try:
            # 1. Download the original from Drive
            download_drive_file(drive, file_id, temp_orig)

            # 2. Make the original publicly accessible
            make_file_public(drive, file_id)

            # 3. Create compressed web version
            resize_image(temp_orig, temp_web, WEB_MAX_PX, WEB_QUALITY)

            # 4. Create thumbnail
            resize_image(temp_orig, temp_thumb, THUMB_MAX_PX, THUMB_QUALITY)

            # 5. Upload web version to Drive
            web_file_id = upload_to_drive(
                drive, temp_web, f"{stem}_web.jpg", WEB_FOLDER_ID
            )

            # 6. Upload thumbnail to Drive
            thumb_file_id = upload_to_drive(
                drive, temp_thumb, f"{stem}_thumb.jpg", THUMBS_FOLDER_ID
            )

            # 7. Build shareable URLs
            orig_url  = drive_view_url(file_id)
            web_url   = drive_view_url(web_file_id,   size="w1200")
            thumb_url = drive_view_url(thumb_file_id, size="w400")

            # 8. Classify with OpenAI Vision (using the resized local copy)
            classification = classify_image(openai_client, temp_web, model=args.model)

            # 9. Try to extract market + tech name from filename
            market, technician = extract_from_filename(filename)

            # 10. Build the sheet row (order matches SHEET_HEADERS exactly)
            now          = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            uploaded_at  = (drive_file.get("createdTime") or "")[:10]
            tags_str     = ", ".join(classification.get("tags", []))

            row = [
                str(uuid.uuid4()),                                        # id
                filename,                                                 # filename
                uploaded_at,                                              # uploaded_at
                now,                                                      # processed_at
                orig_url,                                                 # original_drive_url
                web_url,                                                  # web_drive_url
                thumb_url,                                                # thumbnail_drive_url
                file_id,                                                  # original_file_id
                web_file_id,                                              # web_file_id
                thumb_file_id,                                            # thumbnail_file_id
                technician,                                               # technician_name
                market,                                                   # market
                classification.get("category", "Other"),                 # category
                tags_str,                                                 # tags
                str(classification.get("quality_score", 5)),             # quality_score
                classification.get("marketing_use_case", ""),            # marketing_use_case
                str(classification.get("hero_candidate", False)),        # hero_candidate
                str(classification.get("contains_customer", False)),     # contains_customer
                str(classification.get("contains_license_plate", False)),# contains_license_plate
                str(classification.get("contains_sensitive_info", False)),# contains_sensitive_info
                classification.get("recommended_caption", ""),           # recommended_caption
                "pending",                                                # approval_status
            ]

            # 10. Append row to Google Sheets
            append_sheet_row(sheets, row)
            success += 1

        except Exception as exc:
            tqdm.write(f"  ERROR — {filename}: {exc}")
            errors += 1

        finally:
            # Always clean up temp files (even on error)
            for tmp in [temp_orig, temp_web, temp_thumb]:
                if tmp.exists():
                    tmp.unlink()

        # Small pause to avoid rate-limit errors
        time.sleep(0.3)

    # ── Summary ───────────────────────────────────────────────
    print(f"\n{'=' * 42}")
    print(f"Done!")
    print(f"  ✓ Processed : {success}")
    if errors:
        print(f"  ✗ Errors    : {errors}")
    print(f"\nNext step:")
    print(f"  python scripts/generate_gallery_data.py")


if __name__ == "__main__":
    main()
