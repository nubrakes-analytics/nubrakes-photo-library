#!/usr/bin/env python3
"""
generate_gallery_data.py — Export Google Sheets metadata to JSON for the gallery.

Reads the NuBrakes photo metadata from Google Sheets and writes it to
metadata/photos.json so the HTML gallery can load it.

Run this after sync_and_classify.py to refresh the gallery with new photos.
Then commit and push metadata/photos.json to update GitHub Pages.

Usage:
  python scripts/generate_gallery_data.py
  python scripts/generate_gallery_data.py --output gallery/data/custom.json
  python scripts/generate_gallery_data.py --approved-only   # export only approved photos
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ─── Paths ───────────────────────────────────────────────────────────────────
ROOT                 = Path(__file__).parent.parent
SERVICE_ACCOUNT_FILE = ROOT / "credentials" / "google-service-account.json"
DEFAULT_OUTPUT       = ROOT / "metadata" / "photos.json"

load_dotenv(ROOT / ".env")

# ─── Config ───────────────────────────────────────────────────────────────────
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
SCOPES          = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


# ═════════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════════

def to_bool(value):
    """Convert a string like 'True'/'False'/'1'/'yes' to a Python bool."""
    return str(value).strip().lower() in ("true", "1", "yes")


def to_int(value, default=0):
    """Safely convert a value to int, returning default on failure."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def tags_from_string(raw):
    """Split a comma-separated tag string into a clean list."""
    if not raw:
        return []
    return [t.strip() for t in str(raw).split(",") if t.strip()]


def sheet_row_to_photo(headers, row):
    """
    Convert a Google Sheets row (list of strings) into a photo dict
    that matches the gallery's expected JSON format.
    """
    # Pad the row to the same length as headers (Sheets omits trailing blank cells)
    padded = row + [""] * (len(headers) - len(row))
    record = dict(zip(headers, padded))

    return {
        # Core identity
        "id":                     record.get("id", ""),
        "filename":               record.get("filename", ""),

        # Context
        "market":                 record.get("market", ""),
        "technician":             record.get("technician_name", ""),

        # Classification
        "category":               record.get("category", ""),
        "tags":                   tags_from_string(record.get("tags", "")),
        "quality_score":          to_int(record.get("quality_score", 0)),
        "marketing_use_case":     record.get("marketing_use_case", ""),
        "hero_candidate":         to_bool(record.get("hero_candidate", "")),

        # Safety flags
        "contains_customer":      to_bool(record.get("contains_customer", "")),
        "contains_license_plate": to_bool(record.get("contains_license_plate", "")),
        "contains_sensitive_info":to_bool(record.get("contains_sensitive_info", "")),

        # Caption
        "recommended_caption":    record.get("recommended_caption", ""),

        # URLs (Google Drive public links)
        "thumbnail_url":          record.get("thumbnail_drive_url", ""),
        "web_url":                record.get("web_drive_url", ""),
        "original_url":           record.get("original_drive_url", ""),

        # Dates
        "uploaded_at":            record.get("uploaded_at", ""),
        "processed_at":           record.get("processed_at", ""),

        # Approval
        "approval_status":        record.get("approval_status", "pending"),
        "approved_for_marketing": record.get("approval_status", "").lower() == "approved",
    }


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="Export Google Sheets photo metadata to JSON for the gallery",
    )
    parser.add_argument(
        "--output", default=str(DEFAULT_OUTPUT),
        help=f"Output JSON path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--approved-only", action="store_true",
        help="Only export rows where approval_status = 'approved'",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("\nNuBrakes Gallery Data Generator")
    print("=" * 42)

    # ── Validate ─────────────────────────────────────────────
    if not GOOGLE_SHEET_ID:
        print("ERROR: GOOGLE_SHEET_ID not set in .env")
        sys.exit(1)
    if not SERVICE_ACCOUNT_FILE.exists():
        print(f"ERROR: Service account file not found at {SERVICE_ACCOUNT_FILE}")
        sys.exit(1)

    # ── Connect to Sheets ─────────────────────────────────────
    print("Connecting to Google Sheets…")
    try:
        creds  = service_account.Credentials.from_service_account_file(
            str(SERVICE_ACCOUNT_FILE), scopes=SCOPES
        )
        sheets = build("sheets", "v4", credentials=creds)
        print("✓ Connected")
    except Exception as exc:
        print(f"ERROR: Google authentication failed: {exc}")
        sys.exit(1)

    # ── Read sheet ────────────────────────────────────────────
    print(f"Reading sheet: {GOOGLE_SHEET_ID}")
    try:
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="Sheet1",
        ).execute()
    except Exception as exc:
        print(f"ERROR: Could not read Google Sheets: {exc}")
        sys.exit(1)

    rows = resp.get("values", [])

    if len(rows) < 2:
        print("Sheet is empty or headers-only — no photos to export.")
        photos = []
    else:
        headers = rows[0]
        data    = rows[1:]
        photos  = [sheet_row_to_photo(headers, row) for row in data]

        if args.approved_only:
            before = len(photos)
            photos = [p for p in photos if p["approval_status"].lower() == "approved"]
            print(f"Filtered to approved only: {before} → {len(photos)}")

    # ── Write JSON ────────────────────────────────────────────
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    export = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total":        len(photos),
        "photos":       photos,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(export, f, indent=2, ensure_ascii=False)

    print(f"\n✓ Exported {len(photos)} photo(s) → {output_path}")

    # ── Quick stats ───────────────────────────────────────────
    if photos:
        from collections import Counter
        cats      = Counter(p["category"] for p in photos)
        approvals = Counter(p["approval_status"] for p in photos)
        heroes    = sum(1 for p in photos if p["hero_candidate"])
        print(f"\nSummary:")
        print(f"  Approval status : {dict(approvals)}")
        print(f"  Hero candidates : {heroes}")
        print(f"  Top category    : {cats.most_common(1)[0]}")

    print(f"\nNext steps:")
    print(f"  1. git add metadata/photos.json && git commit -m 'Refresh gallery data'")
    print(f"  2. git push  →  gallery updates automatically on GitHub Pages")


if __name__ == "__main__":
    main()
