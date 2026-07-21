"""
ingest.py
Populate the Images DynamoDB table + latimer-microscopy-images S3 bucket.

Images are grouped by shared name-stem, so a time-lapse (one czi exported to
many _t#### frames) becomes ONE row, not one per frame:
  - movie  (many frames): store the czi + a thumbnail of the first frame,
            record frame_count. Individual frame tifs are NOT uploaded.
  - still  (single frame): store the tif + czi (if any) + a thumbnail.

For each group it also parses date_of_picture and picture_label from the name,
joins tp_number / race_ethnicity / date_specimen_received from the CSV, and
extracts czi metadata (promoted fields + full blob) once.

Keep this file next to czi_metadata.py.

Requires: boto3 czifile xmltodict pandas openpyxl pillow numpy

Safe preview (no uploads, no writes):
    python ingest.py "/Volumes/Zimberg Lab/2025 Tiff conversion" \
        "/Volumes/Zimberg Lab/Cell Lines 2.csv" \
        --cell-line-folder "BRL 47 tiff convert" --dry-run

Real run (optionally limit how many groups to start):
    python ingest.py "/Volumes/Zimberg Lab/2025 Tiff conversion" \
        "/Volumes/Zimberg Lab/Cell Lines 2.csv" \
        --cell-line-folder "BRL 47 tiff convert" --limit 3
"""

import argparse
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from io import BytesIO

import boto3
import numpy as np
import pandas as pd
from PIL import Image

from czi_metadata import extract_czi_metadata

# ---- config ----
BUCKET = os.environ.get("MICRO_BUCKET", "latimer-microscopy-images")
TABLE = os.environ.get("MICRO_TABLE", "Images")
REGION = os.environ.get("AWS_REGION", "us-east-1")
CREATED_BY = os.environ.get("MICRO_USER", "Aubry Figueroa")
THUMB_MAX = 512
CZI_MAX_UPLOAD_MB = int(os.environ.get("MICRO_CZI_MAX_MB", "500"))  # skip auto-upload above this

# czis we deliberately do NOT catalog. Use this for files that cannot be decoded
# (camera-codec movies) AND have no tif export to fall back on, so the record
# would be metadata with no viewable image.
#
# Entries are matched against the normalised stem (stem_key), so punctuation and
# a trailing "- Copy" do not matter: "BRL-42 flask day 32.czi", "BRL-42 flask
# day 32 - Copy.czi" and "BRL_42_flask_day_32.czi" all match the same entry.
# Remove a line here if the file is later exported from ZEN as tif frames.
IGNORE_CZI_NAMES = [
    "BRL-42 flask day 32.czi",     # camera codec, no tif export available
    "BRL-42 flask day 32_2.czi",   # camera codec, no tif export available
    "Continuous-25.czi",           # blank white frame - no usable image
]

# Who took the picture. Initials/surnames that appear in file names -> person.
# Matched as whole words, case-insensitive. Add new people here.
# NOTE: "JL" is deliberately absent - it can be part of a cell-line name.
PHOTOGRAPHERS = {
    "cm": "Carol Murphy",
    "carol": "Carol Murphy",
    "murphy": "Carol Murphy",
    "ash": "Abdullah",
    "jk": "Jenniffer Kalil",
    "jenn": "Jenniffer Kalil",
    "jennk": "Jenniffer Kalil",
    "kalil": "Jenniffer Kalil",
    "af": "Aubrey",
    "amf": "Aubrey",
}

s3 = boto3.client("s3", region_name=REGION)
table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)

CSV_FIELDS = {
    "date received": "date_specimen_received",
    "patient #": "tp_number",
    "race": "race_ethnicity",
}


# ---------- name parsing ----------
def norm_key(text):
    return re.sub(r"[^A-Za-z0-9]", "", str(text)).upper()


# --- idempotency: stable id per acquisition, and a scan of what's already stored ---
NS = uuid.UUID("6f1a2c00-0000-4000-a000-6d6963726f73")  # fixed namespace for this catalog


def acq_id(cell_line, source_rel):
    """Deterministic id so re-running never duplicates an acquisition."""
    return str(uuid.uuid5(NS, f"{cell_line}|{source_rel}"))


def normlabel(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def scan_existing():
    """(set of existing ids, set of (cell_line,date,normlabel) already backed by an image)."""
    ids, covered = set(), set()
    names = {"#i": "id", "#c": "cell_line", "#d": "date_of_picture",
             "#l": "picture_label", "#a": "acquisition_type"}
    kw = {"ProjectionExpression": "#i,#c,#d,#l,#a", "ExpressionAttributeNames": names}
    while True:
        r = table.scan(**kw)
        for it in r["Items"]:
            ids.add(it["id"])
            if it.get("acquisition_type") in ("still", "movie"):
                covered.add((it.get("cell_line", ""), it.get("date_of_picture", ""),
                             normlabel(it.get("picture_label", ""))))
        if "LastEvaluatedKey" not in r:
            return ids, covered
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def cell_line_label(folder_name):
    m = re.match(r"([A-Za-z]+)\s*[-_. ]?\s*(\d+)", folder_name)
    return f"{m.group(1).upper()} {m.group(2)}" if m else folder_name


def strip_cellline(s, cell_line):
    """Remove the known cell-line token in any punctuation form (BRL 47, BRL_47...),
    tolerating a misspelled prefix (e.g. BL 38 for BRL 38)."""
    parts = cell_line.split()
    if len(parts) >= 2:
        pat = re.escape(parts[0]) + r"[\s._,-]*" + re.escape(parts[1])
        s = re.sub(pat, " ", s, flags=re.I)
        # tolerant: first prefix letter required, the rest optional, then the number
        loose = (re.escape(parts[0][0])
                 + "".join(r"[\s._-]*" + re.escape(c) + "?" for c in parts[0][1:])
                 + r"[\s._-]*" + re.escape(parts[1]))
        s = re.sub(loose, " ", s, flags=re.I)
    return s


def parse_date(name, cell_line):
    s = strip_cellline(name, cell_line)
    s = re.sub(r"^\s*movie\s+", "", s, flags=re.I)
    # ISO first (the recommended format): YYYY-MM-DD
    m = re.search(r"(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})", s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # else the older M-D-YY / MM-DD-YY style
    for m in re.finditer(r"(\d{1,2})[-_.](\d{1,2})[-_.](\d{2,4})", s):
        mm, dd, yy = (int(g) for g in m.groups())
        if yy < 100:
            yy += 2000
        try:
            return datetime(yy, mm, dd).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def stem_key(name):
    """Punctuation-insensitive key shared by a czi and all frames of its tif."""
    base = re.sub(r"\.(czi|tif|tiff)$", "", name, flags=re.I)   # only a real extension
    base = re.sub(r"^\s*\(\d+\)\s*", "", base)                # "(1) name" -> "name"
    base = re.sub(r"_t\d+", "", base, flags=re.I)              # drop frame number
    base = re.sub(r"_C\d+_ORG$|_ORG$", "", base, flags=re.I)
    base = re.sub(r"\s*-?\s*(Image Export|Change Scaling)-\d+.*$", "", base, flags=re.I)
    base = re.sub(r"[\s_-]*\bcopy\b\s*$", "", base, flags=re.I)   # "… - Copy" duplicate marker
    return re.sub(r"[^a-z0-9]", "", base.lower())


def fix_label_text(s):
    """Fix common typos and expand abbreviations so labels read cleanly and
    search well (adjust as needed)."""
    s = re.sub(r"f[;:\s]?ask", "flask", s, flags=re.I)                       # f;ask -> flask
    s = re.sub(r"(?<![A-Za-z])dau(?![A-Za-z])", "day", s, flags=re.I)        # dau -> day
    s = re.sub(r"(?<![A-Za-z])matigel(?![A-Za-z])", "matrigel", s, flags=re.I)  # matigel -> matrigel
    # chamber slide: cham / chamb / CS (with optional number) all mean the same
    s = re.sub(r"(?<![A-Za-z])chamb?(?![A-Za-z])", "chamber slide", s, flags=re.I)
    s = re.sub(r"(?<![A-Za-z])CS(\d*)(?![A-Za-z])",
               lambda m: "chamber slide" + (" " + m.group(1) if m.group(1) else ""), s)
    s = re.sub(r"(?<![A-Za-z])F(\d)", r"flask \1", s)                        # F1 -> flask 1
    # expand abbreviations to full words so search matches either form
    s = re.sub(r"(?<![A-Za-z])mam+osph\w*", "mammosphere", s, flags=re.I)    # mammosph/mamosphere -> mammosphere
    s = re.sub(r"(?<![A-Za-z])spill(?![A-Za-z]|over)", "spillover", s, flags=re.I)  # spill -> spillover
    s = re.sub(r"[\s_-]*\bcopy\b", "", s, flags=re.I)        # drop "- Copy" duplicate marker
    return s


def extract_photographer(name, folder=""):
    """Who took the image, from initials/surname in the file name (or folder).
    Whole-word match only, so 'cm' will not fire inside another word."""
    for text in (os.path.splitext(name)[0], folder):
        if not text:
            continue
        for token, person in PHOTOGRAPHERS.items():
            if re.search(rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])", text, re.I):
                return person
    return ""


def extract_matrigel(name):
    """Substrate the cells were grown on, read from the file name.

    matrigel / matigel / mgel / MG  -> matrigel  (plus "pure" or a ratio if given)
    gelatin / gel                   -> gelatin

    These are DIFFERENT substrates and are stored as different values - gelatin
    is not recorded as matrigel. Matrigel is matched first because "mgel" and
    "matrigel" both contain "gel"; the word boundaries stop the gelatin pattern
    firing inside them.
    """
    n = name.lower()
    mg = r"(?:matrigel|matigel|mgel|(?<![a-z])mg(?![a-z]))"
    gel = r"(?<![a-z])(?:gelatin|gel)(?![a-z])"
    sep = r"(?:\s*(?:to|[_:\-])\s*|\s+)"       # 1_1, 1-1, 1:1, "1 to 1", "1 1"
    ratio = r"(\d)" + sep + r"(\d)"
    join = r"[\s_:\-]*"                        # tolerate a stray hyphen: "1-1- mg"

    if (re.search(r"pure\s*(?:matrigel|matigel|mgel|mg)(?![a-z])", n)
            or re.search(mg + r"\s*pure", n)):
        return "pure matrigel"
    m = re.search(mg + join + ratio, n) or re.search(ratio + join + mg, n)
    if m:
        return f"{m.group(1)}:{m.group(2)} matrigel"
    if re.search(mg, n):
        return "matrigel"
    if re.search(gel, n):
        return "gelatin"
    return ""


def picture_label(name, cell_line):
    base = re.sub(r"\.(czi|tif|tiff)$", "", name, flags=re.I)
    base = re.sub(r"^\s*\(\d+\)\s*", "", base)                # "(1) name" -> "name"
    base = re.sub(r"_t\d+", "", base, flags=re.I)
    base = re.sub(r"_C\d+_ORG$|_ORG$", "", base, flags=re.I)
    base = re.sub(r"\s*-?\s*(Image Export|Change Scaling)-\d+.*$", "", base, flags=re.I)
    base = re.sub(r"^\s*movie\s+", "", base, flags=re.I)
    base = strip_cellline(base, cell_line)
    base = re.sub(r"(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})", "", base)     # ISO date
    base = re.sub(r"(\d{1,2})[-_.](\d{1,2})[-_.](\d{2,4})", "", base)     # M-D-YY date
    base = re.sub(r"(?<![A-Za-z0-9])P\d+(?![A-Za-z0-9])", "", base, flags=re.I)  # passage
    base = re.sub(r"(?<![A-Za-z])(?:passage|pass|pa)\s*\d+", "", base, flags=re.I)  # pass 2 / pa2
    for tok in PHOTOGRAPHERS:                     # drop the photographer's initials
        base = re.sub(rf"(?<![A-Za-z]){re.escape(tok)}(?![A-Za-z])", "", base, flags=re.I)
    base = fix_label_text(base)
    return re.sub(r"[\s,._;:-]+", " ", base).strip()


def extract_passage(name):
    """Pull a passage token: P4, P12, 'pass 2', or 'passage 2' (kept out of label)."""
    m = re.search(r"(?<![A-Za-z0-9])P(\d+)(?![A-Za-z0-9])", name, flags=re.I)
    if m:
        return f"P{m.group(1)}"
    m = re.search(r"(?<![A-Za-z])(?:passage|pass|pa)\s*(\d+)", name, flags=re.I)
    return f"P{m.group(1)}" if m else ""


def frame_num(path):
    m = re.search(r"_t(\d+)", os.path.basename(path), flags=re.I)
    return int(m.group(1)) if m else 0


# ---------- clinical csv ----------
def load_clinical(csv_path):
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    rename = {}
    for c in df.columns:
        low = c.lower().strip()
        for prefix, key in CSV_FIELDS.items():
            if low.startswith(prefix):
                rename[c] = key
                break
    df = df.rename(columns=rename)
    clinical = {}
    for _, row in df.iterrows():
        cl = row.get("Cell Line")
        if pd.isna(cl) or not str(cl).strip():
            continue
        clinical[norm_key(cl)] = {
            v: ("" if pd.isna(row.get(v)) else str(row.get(v)).strip())
            for v in CSV_FIELDS.values() if v in df.columns
        }
    return clinical


# ---------- thumbnail ----------
def czi_convert_can_decode(czi):
    """True if our readers can actually decode this czi's pixels."""
    try:
        import czi_convert
        return czi_convert.can_decode(czi)
    except Exception:
        return False


def crop_padding_to_temp(path, image_id):
    """ZEN pads exports onto a large canvas. Write a copy with the black padding
    trimmed (pixel VALUES are untouched - only all-zero border is removed) and
    return its path, or None if there is no meaningful padding."""
    import tifffile
    arr = tifffile.imread(path)
    a = arr[..., :3].mean(axis=-1) if (arr.ndim == 3 and arr.shape[-1] in (3, 4)) else (
        arr.reshape((-1,) + arr.shape[-2:])[0] if arr.ndim > 2 else arr)
    m = a > 5
    if not m.any():
        return None
    rows = np.where(np.any(m, axis=1))[0]
    cols = np.where(np.any(m, axis=0))[0]
    h = rows[-1] - rows[0] + 1
    w = cols[-1] - cols[0] + 1
    if h * w > 0.9 * a.size:
        return None                      # no real padding, keep the original
    out = os.path.join(tempfile.gettempdir(), f"{image_id}_crop.tif")
    tifffile.imwrite(out, arr[..., rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
                     if arr.ndim > 2 else arr[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1])
    return out


def make_thumbnail(path):
    """PNG thumbnail from a tif. Reads with tifffile rather than PIL so it can
    handle very large canvases (PIL refuses >89 MP) and unusual pixel modes,
    both of which occur in these ZEN exports."""
    import tifffile
    arr = tifffile.imread(path)
    if arr.ndim > 2:                       # take a single 2D plane
        arr = arr[..., :3].mean(axis=-1) if arr.shape[-1] in (3, 4) else arr.reshape(
            (-1,) + arr.shape[-2:])[0]
    arr = np.asarray(arr, dtype=float)

    # ZEN can export a small image on a huge mostly-black canvas; crop to the
    # content first so the thumbnail isn't a speck in a black rectangle.
    m = arr > 5
    if m.any():
        rows = np.where(np.any(m, axis=1))[0]
        cols = np.where(np.any(m, axis=0))[0]
        if (rows[-1] - rows[0] + 1) * (cols[-1] - cols[0] + 1) < 0.9 * arr.size:
            arr = arr[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]

    lo, hi = np.percentile(arr, [1, 99])
    if hi <= lo:
        lo, hi = float(arr.min()), float(arr.max())
    arr = np.clip((arr - lo) / (hi - lo + 1e-9), 0, 1) * 255
    img = Image.fromarray(arr.astype("uint8"))
    img.thumbnail((THUMB_MAX, THUMB_MAX))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_movie_mp4(frames, out_path, fps=15, max_px=1024):
    """Encode a time-lapse's frames into a web-playable MP4.

    Uses one global brightness normalization across the movie (so it doesn't
    flicker) and downscales to a preview size. The czi remains the full master.
    Needs: imageio, imageio-ffmpeg, tifffile.
    """
    import imageio.v2 as imageio
    import tifffile

    def load(path):
        a = tifffile.imread(path).astype(float)
        if a.ndim == 3:
            a = a[..., :3].mean(axis=-1) if a.shape[-1] in (3, 4) else a[0]
        return a

    # ZEN exports each frame onto a canvas big enough for the WHOLE run. In a
    # tracking time-lapse the stage moves, so the imaged region sits in a
    # different place in every frame. Crop each frame to its OWN content box,
    # not the first frame's, or the picture drifts out of view as it plays.
    def content_box(a, thresh=5):
        m = a > thresh
        rows = np.where(np.any(m, axis=1))[0]
        cols = np.where(np.any(m, axis=0))[0]
        if len(rows) == 0 or len(cols) == 0:
            return None
        r0, r1, c0, c1 = rows[0], rows[-1], cols[0], cols[-1]
        # crop when there is meaningful black padding; leave full frames alone
        if (r1 - r0 + 1) * (c1 - c0 + 1) > 0.9 * a.size:
            return None
        return int(r0), int(r1), int(c0), int(c1)

    def load_c(path):
        a = load(path)
        box = content_box(a)
        return a[box[0]:box[1] + 1, box[2]:box[3] + 1] if box else a

    _b0 = content_box(load(frames[0]))
    if _b0:
        print(f"    cropping each frame to its own content: "
              f"{_b0[1]-_b0[0]+1} x {_b0[3]-_b0[2]+1}")

    # global 1/99 percentile from up to 10 sampled frames
    idxs = sorted(set(int(i) for i in np.linspace(0, len(frames) - 1, min(10, len(frames)))))
    sample = np.concatenate([load_c(frames[i]).ravel() for i in idxs])
    lo, hi = np.percentile(sample, [1, 99])
    if hi <= lo:
        lo, hi = float(sample.min()), float(sample.max())

    h, w = load_c(frames[0]).shape[:2]
    scale = min(1.0, max_px / max(h, w))
    nw, nh = max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)  # even dims

    writer = imageio.get_writer(out_path, fps=fps, codec="libx264",
                                quality=7, macro_block_size=None)
    try:
        for path in frames:
            a = np.clip((load_c(path) - lo) / (hi - lo + 1e-9), 0, 1) * 255
            im = Image.fromarray(a.astype("uint8")).convert("L").resize((nw, nh))
            writer.append_data(np.array(im.convert("RGB")))
    finally:
        writer.close()


# ---------- collect + group ----------
# resolved once stem_key exists
IGNORE_CZI = {stem_key(n) for n in IGNORE_CZI_NAMES}


def file_sig(path):
    """Cheap identity check for a file: size + hash of the first megabyte.
    Used to tell a duplicate ZEN export (identical bytes) from a different
    image that merely shares a name."""
    import hashlib
    try:
        with open(path, "rb") as fh:
            head = fh.read(1 << 20)
        return os.path.getsize(path), hashlib.md5(head).hexdigest()
    except OSError:
        return None


def collect(folder):
    """One acquisition per Image Export folder. Within a folder keep only the
    _ORG tifs (ignore ZEN's derived display copies). A folder with many _t####
    frames is a movie, one _ORG tif is a still. czis with no matching folder
    become czi-only acquisitions (metadata + file, no image)."""
    by_dir, czis = {}, {}
    for dirpath, _, files in os.walk(folder):
        for f in files:
            if f.startswith("."):          # skip .DS_Store and ._AppleDouble files
                continue
            full = os.path.join(dirpath, f)
            if f.lower().endswith((".tif", ".tiff")):
                by_dir.setdefault(dirpath, []).append(full)
            elif f.lower().endswith(".czi"):
                if stem_key(f) in IGNORE_CZI:
                    continue                      # deliberately not catalogued
                czis[stem_key(f)] = full

    used, acqs = set(), []
    by_stem = {}
    for dirpath, frames in by_dir.items():
        org = [f for f in frames if re.search(r"_ORG\.(tif|tiff)$", f, re.I)]
        frames = sorted(org or frames, key=frame_num)
        k = stem_key(os.path.basename(dirpath))
        if czis.get(k):
            used.add(k)
        by_stem.setdefault(k, []).append(frames)

    for k, cand_list in by_stem.items():
        # Folders can share a name for two very different reasons:
        #   - ZEN exported the SAME acquisition twice (byte-identical) -> one record
        #   - several genuinely different images were given the same name -> keep all
        # So compare the actual first-frame bytes rather than trusting the name.
        seen = {}
        for frames in cand_list:
            sig = file_sig(frames[0]) if frames else None
            prev = seen.get(sig) if sig else None
            if prev is not None:
                if len(frames) > len(prev["frames"]):   # identical export, keep more frames
                    prev["frames"] = frames
                continue
            acq = {"frames": frames, "czi": czis.get(k)}
            if sig:
                seen[sig] = acq
            acqs.append(acq)

    for k, p in czis.items():              # czis with no exported tif
        if k not in used:
            acqs.append({"frames": [], "czi": p})

    acqs.sort(key=lambda a: os.path.basename(a["frames"][0] if a["frames"] else a["czi"]))
    return acqs


def ingest_folder(root, csv_path, folder_name, dry_run, limit, movies_only=False, reingest=False):
    clinical = load_clinical(csv_path)
    folder = os.path.join(root, folder_name)
    cell_line = cell_line_label(folder_name)
    rec = clinical.get(norm_key(cell_line), {})

    existing_ids, covered = (set(), set()) if reingest else scan_existing()

    acqs = collect(folder)
    if limit:
        acqs = acqs[:limit]

    n_movie = sum(1 for a in acqs if len(a["frames"]) > 1)
    n_still = sum(1 for a in acqs if len(a["frames"]) == 1)
    n_czi = sum(1 for a in acqs if not a["frames"])
    print(f"cell line: {cell_line}   {len(acqs)} acquisition(s) "
          f"({n_movie} movie, {n_still} still, {n_czi} czi-only)\n")

    czi_cache = {}   # local czi path -> s3 key, so a shared czi uploads once
    pending_uploads = []   # (local_path, s3_key) for czis too large to auto-upload

    for g in acqs:
        frames = g["frames"]
        czi = g["czi"]
        is_czi_only = not frames
        is_movie = len(frames) > 1
        first = frames[0] if frames else None
        name = os.path.basename(first if first else czi)
        acq_type = "czi_only" if is_czi_only else ("movie" if is_movie else "still")

        date_pic = parse_date(name, cell_line)
        label = picture_label(name, cell_line)
        passage = extract_passage(name) or "primary"
        matrigel = extract_matrigel(name)
        photographer = extract_photographer(name, os.path.basename(os.path.dirname(first or czi)))

        promoted, blob = ({}, None)
        if czi:
            try:
                promoted, blob = extract_czi_metadata(czi)
            except Exception as e:
                print(f"  ! czi metadata failed for {os.path.basename(czi)}: {e}")
        if promoted.get("magnification") in ("0x", "0", "0.0x", "0.0"):
            promoted["magnification"] = ""    # czi reported no objective
        ad = str(promoted.get("acquisition_date", ""))
        if not date_pic and re.match(r"\d{4}-\d{2}-\d{2}", ad):
            date_pic = ad[:10]                # use the czi's date when the name has none

        source_rel = os.path.relpath(os.path.dirname(first) if first else czi, root)
        image_id = acq_id(cell_line, source_rel)
        already = image_id in existing_ids or (
            is_czi_only and (cell_line, date_pic, normlabel(label)) in covered)

        if dry_run:
            if movies_only and not is_movie:
                continue
            kind = f"movie ({len(frames)} frames)" if is_movie else acq_type
            mark = "  (already ingested)" if already else ""
            print(f"- {name}   [{kind}]{mark}")
            print(f"    date_of_picture : {date_pic or '(none)'}")
            print(f"    picture_label   : {label}")
            if matrigel:
                print(f"    matrigel_type   : {matrigel}")
            if photographer:
                print(f"    photographer    : {photographer}")
            print(f"    czi             : {os.path.basename(czi) if czi else '(no match)'}")
            if is_movie:
                print(f"    frames folder   : {os.path.basename(os.path.dirname(frames[0]))}")
                print(f"    first frame     : {os.path.basename(frames[0])}")
                print(f"    last frame      : {os.path.basename(frames[-1])}")
            if promoted:
                print(f"    magnification   : {promoted.get('magnification','')}")
                print(f"    optics_type     : {promoted.get('optics_type','')}")
            print()
            continue

        if already:
            print(f"= skip (already ingested)  {cell_line}  {date_pic or '(undated)'}  {label}")
            continue

        date_folder = date_pic or "undated"
        prefix = f"{norm_key(cell_line)}/{date_folder}/{image_id}"

        tif_key, czi_key, thumb_key = "", "", ""
        czi_pending = False
        if czi:
            if czi in czi_cache:
                czi_key = czi_cache[czi]
            else:
                czi_key = f"{prefix}.czi"
                size_mb = os.path.getsize(czi) / (1024 * 1024)
                if size_mb > CZI_MAX_UPLOAD_MB:
                    czi_pending = True
                    pending_uploads.append((czi, czi_key))
                    print(f"  ! czi is {size_mb / 1024:.1f} GB, skipping auto-upload "
                          f"(queued for separate upload)")
                else:
                    s3.upload_file(czi, BUCKET, czi_key)
                czi_cache[czi] = czi_key

        # every acquisition with frames keeps a tif (for movies this is the first
        # frame): it is the still preview and the source for the thumbnail
        if first and not is_czi_only:
            tif_key = f"{prefix}.tif"
            cropped = None
            try:
                cropped = crop_padding_to_temp(first, image_id)
            except Exception as e:
                print(f"  ! tif crop skipped for {name}: {e}")
            s3.upload_file(cropped or first, BUCKET, tif_key)
            if cropped:
                os.remove(cropped)

        if first:
            try:
                thumb_key = f"thumbnails/{image_id}.png"
                s3.put_object(Bucket=BUCKET, Key=thumb_key,
                              Body=make_thumbnail(first), ContentType="image/png")
            except Exception as e:
                print(f"  ! thumbnail skipped for {name}: {e}")
                thumb_key = ""

        # a time-lapse exported to frames: encode them into a web-playable mp4
        mp4_key = ""
        if is_movie:
            try:
                tmp = os.path.join(tempfile.gettempdir(), f"{image_id}.mp4")
                make_movie_mp4(frames, tmp)
                mp4_key = f"{prefix}.mp4"
                s3.upload_file(tmp, BUCKET, mp4_key,
                               ExtraArgs={"ContentType": "video/mp4"})
                os.remove(tmp)
                print(f"    encoded movie -> {len(frames)} frames -> mp4")
            except Exception as e:
                print(f"  ! mp4 skipped for {name}: {e}")

        # czi-only (no exported tif): render from the czi so the record is
        # viewable. A single-plane czi becomes a normal still; a multi-timepoint
        # czi becomes a movie (frames -> mp4). Either way tif_source records that
        # the image was generated here rather than exported by ZEN.
        tif_source = ""
        if is_czi_only and czi and not tif_key:
            if not czi_convert_can_decode(czi):
                # Fast time-lapses are often compressed by the CAMERA in a
                # proprietary codec (compression id >= 1000). Neither czifile nor
                # libCZI implements those - only ZEN can read them. This is NOT a
                # size problem: the pixels simply cannot be decoded here.
                print(f"  ! {name}: czi uses a camera codec our readers cannot "
                      f"decode - only ZEN can read it. Export it from ZEN as tif "
                      f"frames and re-ingest. Left as czi_only, metadata intact.")
            else:
                try:
                    import czi_convert
                    if czi_convert.is_movie(czi):
                        size_mb = os.path.getsize(czi) / (1024 * 1024)
                        if size_mb > CZI_MAX_UPLOAD_MB:
                            print(f"  ! {name}: czi movie is {size_mb/1024:.1f} GB - "
                                  f"too many frames to hold in memory. Export it from "
                                  f"ZEN as tif frames and re-ingest. Left as czi_only.")
                        else:
                            frames_r = czi_convert.czi_frames(czi)
                            tmp_dir = tempfile.mkdtemp()
                            paths = []
                            import tifffile as _tf
                            for n, fr in enumerate(frames_r):
                                p = os.path.join(tmp_dir, f"f{n:05d}.tif")
                                _tf.imwrite(p, fr)
                                paths.append(p)
                            tif_key = f"{prefix}.tif"
                            s3.upload_file(paths[0], BUCKET, tif_key)
                            tmp_mp4 = os.path.join(tempfile.gettempdir(), f"{image_id}.mp4")
                            make_movie_mp4(paths, tmp_mp4)
                            mp4_key = f"{prefix}.mp4"
                            s3.upload_file(tmp_mp4, BUCKET, mp4_key,
                                           ExtraArgs={"ContentType": "video/mp4"})
                            os.remove(tmp_mp4)
                            for p in paths:
                                os.remove(p)
                            os.rmdir(tmp_dir)
                            acq_type = "movie"
                            tif_source = "rendered from czi"
                            print(f"    rendered czi movie -> {len(frames_r)} frames -> mp4")
                    else:
                        tmp_tif = os.path.join(tempfile.gettempdir(), f"{image_id}.tif")
                        if czi_convert.czi_to_tiff(czi, tmp_tif):
                            tif_key = f"{prefix}.tif"
                            s3.upload_file(tmp_tif, BUCKET, tif_key)
                            os.remove(tmp_tif)
                            acq_type = "still"
                            tif_source = "rendered from czi"
                            print(f"    rendered czi -> tif + thumbnail")
                    thumb_key = f"thumbnails/{image_id}.png"
                    s3.put_object(Bucket=BUCKET, Key=thumb_key,
                                  Body=czi_convert.czi_thumbnail_png(czi),
                                  ContentType="image/png")
                except Exception as e:
                    print(f"  ! czi render failed for {name}: {type(e).__name__}: {e}")

        now = datetime.now(timezone.utc).isoformat()
        item = {
            "id": image_id,
            "cell_line": cell_line,
            "date_of_picture": date_pic,
            "picture_label": label,
            "acquisition_type": acq_type,
            "tif_source": tif_source,
            "frame_count": len(frames),
            "tp_number": rec.get("tp_number", ""),
            "race_ethnicity": rec.get("race_ethnicity", ""),
            "date_specimen_received": rec.get("date_specimen_received", ""),
            "passage": passage,
            "matrigel_type": matrigel,
            "photographer": photographer,
            "growth_medium": "",
            "file_tiff_s3_key": tif_key,
            "file_czi_s3_key": czi_key,
            "file_mp4_s3_key": mp4_key,
            "thumbnail_s3_key": thumb_key,
            "czi_upload_pending": "true" if czi_pending else "",
            "created_by": CREATED_BY,
            "created_at": now,
            "updated_at": now,
        }
        item.update(promoted)
        if blob:
            item["czi_metadata"] = blob
        item = {k: v for k, v in item.items() if v not in (None, "")}

        table.put_item(Item=item)
        existing_ids.add(image_id)
        if not is_czi_only:
            covered.add((cell_line, date_pic, normlabel(label)))
        print(f"+ {image_id[:8]}  {cell_line}  {date_pic or '(undated)'}  [{acq_type}]  {label}")

    if pending_uploads:
        print(f"\n{len(pending_uploads)} large movie czi(s) were not uploaded. "
              f"Run these to upload them (shows progress):")
        for local, key in pending_uploads:
            print(f'aws s3 cp "{local}" "s3://{BUCKET}/{key}"')


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Ingest one cell-line folder into S3 + DynamoDB")
    p.add_argument("root", help='the "2025 Tiff conversion" folder')
    p.add_argument("csv", help="Cell Lines 2.csv")
    p.add_argument("--cell-line-folder", required=True, help='e.g. "BRL 47 tiff convert"')
    p.add_argument("--dry-run", action="store_true", help="preview only; upload/write nothing")
    p.add_argument("--movies-only", action="store_true",
                   help="with --dry-run, list only the multi-frame (movie) acquisitions")
    p.add_argument("--limit", type=int, default=0, help="process only the first N acquisitions")
    p.add_argument("--reingest", action="store_true",
                   help="ignore what's already stored and process everything (overwrites)")
    args = p.parse_args()
    ingest_folder(args.root, args.csv, args.cell_line_folder, args.dry_run,
                  args.limit, args.movies_only, args.reingest)
