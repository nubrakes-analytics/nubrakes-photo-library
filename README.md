# NuBrakes Technician Photo Asset Library — Phase 1

A complete end-to-end system for collecting, classifying, reviewing, and deploying
technician photos for NuBrakes marketing. Syncs from Google Drive, classifies with
OpenAI Vision, stores metadata in Google Sheets, and serves a searchable gallery via
a single HTML file on GitHub Pages.

---

## How it works

```
Google Drive (originals uploaded by techs)
        ↓  sync_and_classify.py
Resizes → uploads web + thumb versions → classifies with OpenAI → writes to Google Sheets
        ↓  generate_gallery_data.py
metadata/photos.json
        ↓  gallery/index.html (GitHub Pages)
Marketing team browses, filters, approves photos
```

---

## Project structure

```
nubrakes-photo-library/
├── gallery/
│   └── index.html              ← Single-file gallery (deploy to GitHub Pages)
├── metadata/
│   ├── photos.json             ← Generated JSON loaded by the gallery
│   └── sample_photos.json      ← Sample data for testing without real photos
├── scripts/
│   ├── sync_and_classify.py    ← Main pipeline: Drive → Sheets → classify
│   └── generate_gallery_data.py← Export Sheets metadata → photos.json
├── credentials/
│   └── .gitkeep                ← Put your service account JSON here (git-ignored)
├── .env                        ← Your secrets (git-ignored, never committed)
├── .env.example                ← Template — copy this to .env
├── .gitignore
├── requirements.txt
└── README.md
```

---

## One-time setup

### 1. Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click **Select a project → New Project**
3. Name it something like `nubrakes-photo-library`
4. Click **Create**

### 2. Enable the APIs

Inside your new project:

1. Go to **APIs & Services → Library**
2. Search for **Google Drive API** → Enable
3. Search for **Google Sheets API** → Enable

### 3. Create a service account

1. Go to **APIs & Services → Credentials**
2. Click **Create Credentials → Service Account**
3. Name: `nubrakes-photo-library` (or anything you like)
4. Click **Create and Continue** → skip optional role fields → **Done**
5. Click the service account you just created
6. Go to the **Keys** tab → **Add Key → Create new key → JSON**
7. Download the JSON file
8. Rename it `google-service-account.json` and place it at:
   ```
   credentials/google-service-account.json
   ```
   > This file is git-ignored and will never be committed.

### 4. Share your Google Drive folders with the service account

The service account has its own email address (shown in the Credentials page,
looks like `nubrakes-photo-library@your-project.iam.gserviceaccount.com`).

You need to share **four** Drive folders with this email as **Editor**:

| Folder | Purpose |
|--------|---------|
| Originals | Where technicians upload full-res photos |
| Web | Script uploads resized 1200px versions here |
| Thumbs | Script uploads 400px thumbnail versions here |

To share: right-click any folder in Google Drive → **Share** → paste the service account email → set to **Editor** → Send.

### 5. Create a Google Sheet

1. Go to [sheets.google.com](https://sheets.google.com) and create a blank spreadsheet
2. Name it `NuBrakes Photo Metadata` (or anything)
3. Share it with the service account email (same as above) as **Editor**
4. Copy the Sheet ID from the URL:
   ```
   https://docs.google.com/spreadsheets/d/THIS_IS_THE_SHEET_ID/edit
   ```

### 6. Configure your .env file

```bash
cp .env.example .env
```

Open `.env` and fill in your values:

```
OPENAI_API_KEY=sk-proj-...
GOOGLE_SHEET_ID=your_sheet_id_here
GOOGLE_ORIGINALS_FOLDER_ID=your_originals_folder_id
GOOGLE_WEB_FOLDER_ID=your_web_folder_id
GOOGLE_THUMBS_FOLDER_ID=your_thumbs_folder_id
```

To get a folder ID: open the folder in Google Drive — the ID is the last part of the URL:
```
https://drive.google.com/drive/folders/THIS_IS_THE_FOLDER_ID
```

### 7. Install Python dependencies

```bash
cd /path/to/nubrakes-photo-library
pip3 install -r requirements.txt
```

---

## Running the pipeline

### Step 1 — Sync and classify new photos

```bash
python3 scripts/sync_and_classify.py
```

This script:
1. Lists all photos in your Google Drive Originals folder
2. Skips any already in Google Sheets (deduplication by filename)
3. Downloads each new photo temporarily
4. Resizes to web (1200px) and thumb (400px) sizes
5. Uploads web + thumb versions to their Drive folders
6. Sends the image to OpenAI Vision for classification
7. Writes all metadata to a new row in Google Sheets

**Options:**

```bash
# Preview what would happen without making any changes
python3 scripts/sync_and_classify.py --dry-run

# Process only the first 10 photos (useful for testing)
python3 scripts/sync_and_classify.py --limit 10

# Use a different OpenAI model (default: gpt-4o-mini)
python3 scripts/sync_and_classify.py --model gpt-4o
```

### Step 2 — Export metadata to JSON

```bash
python3 scripts/generate_gallery_data.py
```

This reads your Google Sheet and writes `metadata/photos.json`, which the gallery loads.

```bash
# Export only approved photos
python3 scripts/generate_gallery_data.py --approved-only

# Write to a custom path
python3 scripts/generate_gallery_data.py --output gallery/data/photos.json
```

### Step 3 — Commit and push to update the gallery

```bash
git add metadata/photos.json
git commit -m "Refresh gallery data — $(date +%Y-%m-%d)"
git push
```

The gallery on GitHub Pages updates automatically within a minute.

---

## Viewing the gallery locally

Open `gallery/index.html` directly in your browser. It loads from `../metadata/photos.json`
by default, so the sample data will display immediately without any server needed.

> **Tip:** Use `metadata/sample_photos.json` as a reference for the expected JSON format.
> Rename it to `photos.json` temporarily to test the gallery with sample data.

---

## Deploying the gallery to GitHub Pages

1. Push this repo to GitHub (if not already done)
2. Go to your repo → **Settings → Pages**
3. Source: `main` branch, root `/`
4. Click **Save**
5. Your gallery will be live at:
   ```
   https://nubrakes-analytics.github.io/nubrakes-photo-library/gallery/
   ```

The gallery automatically loads `metadata/photos.json` from the same repo.
Every time you push an updated `photos.json`, the gallery reflects the new data.

---

## Ongoing workflow

### When technicians upload new photos

1. They drop photos into the Google Drive **Originals** folder (or a subfolder)
2. You run:
   ```bash
   python3 scripts/sync_and_classify.py
   python3 scripts/generate_gallery_data.py
   git add metadata/photos.json && git commit -m "Add new photos" && git push
   ```
3. The gallery updates automatically

### Reviewing and approving photos

1. Open the gallery at your GitHub Pages URL
2. Use the filters to browse by category, market, technician, or quality score
3. Toggle **Hero only** to see high-priority candidates
4. Click any photo to open the full modal — review the caption, safety flags, and metadata
5. Open your Google Sheet to update `approval_status` to `approved`, `pending`, or `rejected`
6. Re-run `generate_gallery_data.py` and push to reflect approvals in the gallery

### Exporting approved photos for Customer.io campaigns

1. In the gallery, enable the **Approved** filter to show only approved photos
2. Click photos to select them (checkbox or click the card overlay)
3. Use **Copy URLs** in the selection tray to copy web-size image URLs to your clipboard
4. Paste those URLs directly into Customer.io email templates as image source URLs

> The web-size images (1200px) are hosted publicly on Google Drive and load reliably
> in email clients. For hero images, use the `original_url` for highest resolution.

### Filtering for specific use cases

| Goal | Filter to use |
|------|--------------|
| Find email header images | Category: `Hero Image Candidate` + Approved |
| Find service explainer images | Category: `Brake Parts` or `Technician Working Shot` |
| Find social proof images | Category: `Customer Handoff` |
| Find van/mobile setup shots | Category: `Mobile Service Setup` |
| Find Dallas-specific photos | Market: `Dallas` |
| Find top quality photos | Quality: `8+` or `9+` |

---

## photos.json format

Each object in the `photos` array represents one photo:

```json
{
  "id": "a1b2c3d4-0001",
  "filename": "2026-05-08_dallas_marcus-j_working_001.jpg",
  "market": "Dallas",
  "technician": "Marcus Johnson",
  "category": "Technician Working Shot",
  "tags": ["brake pads", "driveway service", "branded uniform"],
  "quality_score": 8,
  "marketing_use_case": "email_body",
  "hero_candidate": false,
  "contains_customer": false,
  "contains_license_plate": false,
  "contains_sensitive_info": false,
  "recommended_caption": "Expert brake service delivered right to your driveway.",
  "thumbnail_url": "https://drive.google.com/uc?id=THUMB_FILE_ID",
  "web_url": "https://drive.google.com/uc?id=WEB_FILE_ID",
  "original_url": "https://drive.google.com/uc?id=ORIG_FILE_ID",
  "uploaded_at": "2026-05-08",
  "processed_at": "2026-05-08",
  "approval_status": "approved",
  "approved_for_marketing": true
}
```

| Field | Description |
|-------|-------------|
| `id` | Unique UUID |
| `filename` | Original filename (format: `YYYY-MM-DD_market_tech_desc_NNN.jpg`) |
| `market` | City/market parsed from filename |
| `technician` | Technician full name parsed from filename |
| `category` | AI-assigned photo category |
| `tags` | Comma-separated descriptive tags from AI |
| `quality_score` | 1–10 quality rating from AI |
| `marketing_use_case` | `hero_image`, `email_body`, `service_explainer`, `reminder_campaign` |
| `hero_candidate` | `true` if AI flagged as high-priority marketing image |
| `contains_customer` | `true` if customer is visible (consent required before use) |
| `contains_license_plate` | `true` if license plate is visible (may need blurring) |
| `contains_sensitive_info` | `true` if any other sensitive info visible |
| `recommended_caption` | AI-suggested caption for email/social use |
| `thumbnail_url` | 400px thumbnail (used in gallery grid) |
| `web_url` | 1200px web-optimized (used in modal and emails) |
| `original_url` | Full-resolution original (use for print/downloads) |
| `approval_status` | `approved`, `pending`, or `rejected` |
| `approved_for_marketing` | `true` when `approval_status` is `approved` |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `GOOGLE_SHEET_ID not set` | Check your `.env` file has the correct Sheet ID |
| `Service account file not found` | Place `google-service-account.json` in `credentials/` |
| `403 The caller does not have permission` | Share the Drive folder and Sheet with the service account email |
| `OpenAI API error` | Check `OPENAI_API_KEY` in `.env` and your OpenAI account credits |
| Gallery shows no photos | Make sure `metadata/photos.json` exists and has the correct format |
| Thumbnails don't load in gallery | Confirm Drive files have public access (set by the sync script automatically) |
| Photos already in Sheet getting re-added | The script deduplicates by `filename` — filenames must be unique |
| `Pillow` rotation issues on phone photos | Handled automatically by `ImageOps.exif_transpose()` |

---

## Security notes

- **Never commit** `credentials/google-service-account.json` or `.env` — both are git-ignored
- The service account only has access to folders and sheets you explicitly share with it
- Drive files are made publicly readable (viewer link) so the gallery can load them without auth
- Photos containing customers (`contains_customer: true`) should only be used after obtaining signed consent from the customer

---

## Adding new technician markets

No code changes needed. Just:
1. Tell technicians to name photos with the market in the filename:
   ```
   2026-05-10_phoenix_tech-name_description_001.jpg
   ```
2. Upload to the same Originals folder in Drive
3. Run `sync_and_classify.py` — the market is parsed automatically from the filename
4. The gallery filter dropdown will include `Phoenix` automatically on next data refresh
