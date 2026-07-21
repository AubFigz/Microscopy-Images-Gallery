"""
scan_desktop.py  -  PREVIEW ONLY (writes nothing to AWS).

Walks the microscope Desktop and shows how every image would be catalogued.
It reads the WHOLE folder path, not just the file name, because cell line,
date, and experiment context (matrigel, chamber, primary, tumor, CBD, dose
response, transfection, well plate...) often live in the folder names.

Writes preview_manifest.csv for review in Excel.

    python scan_desktop.py "D:\\Users\\zeiss\\Desktop"

Edit the lists below to match your lab, then re-run until it looks right.
"""

import argparse
import csv
import datetime
import os
import re

# ---- EDIT THESE TO MATCH YOUR LAB -------------------------------------------

PEOPLE = [
    "Abdullah", "AlmUtaiRy", "Carol", "Colton", "Jenniffer Kalil", "Jowaher",
    "Manasi", "Megan", "Stephen Zimberg", "XiaoLu", "Yousef",
]

SKIP_FOLDERS = [
    "to ignore", "Setup Images Gallery", "Other PC Images", "zeiss", "shark",
    "Screenshots", "Recycle Bin", "ApoTomeFocusCalibrations",
]

# cell-line prefixes: letters, optionally followed by a number (BRL 46, NTA, BT-20)
KNOWN_PREFIXES = {"BRL", "BSL", "BTL", "BT", "ETL", "NDE", "NTA", "NTAL", "PTL",
                  "PNTAL", "CAMA", "TP", "DCIS", "DF", "GWI", "SKBR", "MCF", "MDA"}

# lines with an odd shape, matched first
SPECIAL_LINES = [
    (r"\bT47[-\s]?D\b", "T47-D"),
    (r"\bMDA[-\s]?MB[-\s]?231\b", "MDA-MB-231"),
    (r"\bBT[-\s]?20\b", "BT-20"),
    (r"\bEN[-\s]?OV[-\s]?(\d+)", "EN-OV"),
    (r"\bMCF[-\s]?10A\b", "MCF10A"),
    (r"\bSKBR[-\s]?(\d+)", "SKBR"),
]

# initials in file names -> photographer (JL excluded; can be part of a line name)
INITIALS = {
    "cm": "Carol Murphy", "carol": "Carol Murphy", "murphy": "Carol Murphy",
    "ash": "Abdullah",
    "jk": "Jenniffer Kalil", "jenn": "Jenniffer Kalil",
    "jennk": "Jenniffer Kalil", "kalil": "Jenniffer Kalil",
    "af": "Aubrey", "amf": "Aubrey",
}

# experiment context found in folder names -> a searchable "context" note
CONTEXT_PHRASES = [
    "straight matrigel", "matrigel", "chamber slide", "chamber",
    "dose response", "dose", "cbd", "transfection efficiency", "trans eff",
    "transfection", "primary", "tumor", "96 well plate", "48 well plate",
    "24 well plate", "6 well plate", "well plate", "spillover", "reseeded",
]
# -----------------------------------------------------------------------------

IMAGE_EXT = (".czi", ".tif", ".tiff")
PEOPLE_LOWER = {p.lower() for p in PEOPLE}
SKIP_LOWER = {s.lower() for s in SKIP_FOLDERS}
LASTNAME = {p.split()[-1].lower(): p for p in PEOPLE
            if len(p.split()) > 1 and p.split()[-1].isalpha() and len(p.split()[-1]) >= 3}


def is_skip(name):
    n = name.lower()
    return n in SKIP_LOWER or n.endswith("_files")


def cell_line_from(text):
    for pat, label in SPECIAL_LINES:
        m = re.search(pat, text, re.I)
        if m:
            num = m.group(1) if (m.groups() and m.group(1)) else ""
            return f"{label} {num}".strip()
    best = None
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z]{2,})[\s._-]?(\d+)?(?![A-Za-z])", text):
        if m.group(1).upper() in KNOWN_PREFIXES:
            if best is None or m.start() < best.start():
                best = m
    if best:
        return f"{best.group(1).upper()} {(best.group(2) or '').strip()}".strip()
    return ""


def strip_cellline(s, cell_line):
    parts = cell_line.split()
    if len(parts) >= 2:
        s = re.sub(re.escape(parts[0]) + r"[\s._,-]*" + re.escape(parts[1]), " ", s, flags=re.I)
    elif parts:
        s = re.sub(r"(?<![A-Za-z])" + re.escape(parts[0]) + r"(?![A-Za-z])", " ", s, flags=re.I)
    return s


def parse_date(name, cell_line):
    s = strip_cellline(name, cell_line)
    m = re.search(r"(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})", s)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        try:
            return datetime.date(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            pass
    for m in re.finditer(r"(\d{1,2})[-_.](\d{1,2})[-_.](\d{2,4})", s):
        mm, dd, yy = (int(g) for g in m.groups())
        if yy < 100:
            yy += 2000
        try:
            return datetime.date(yy, mm, dd).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def extract_passage(text):
    m = re.search(r"(?<![A-Za-z0-9])[Pp](\d+)(?![A-Za-z0-9])", text)
    return f"P{m.group(1)}" if m else ""


def mag_from_name(name):
    m = re.search(r"(?<![A-Za-z0-9])(\d{1,3})\s*[xX](?![A-Za-z0-9])", name)
    return f"{int(m.group(1))}x" if m else ""


def optics_from_name(name):
    if re.search(r"plas\s*dic", name, re.I):
        return "PlasDIC"
    if re.search(r"(?<![A-Za-z])DIC(?![A-Za-z])", name, re.I):
        return "DIC"
    if re.search(r"(?<![A-Za-z])PH\s*\d?(?![A-Za-z])", name, re.I):
        return "PH"
    return ""


def photographer_from(folders, fname):
    for f in folders:
        if f.lower() in PEOPLE_LOWER:
            return f
    stem = os.path.splitext(fname)[0]
    for token, full in LASTNAME.items():
        if re.search(rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])", stem, re.I):
            return full
    for token, full in INITIALS.items():
        if re.search(rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])", stem, re.I):
            return full
    return ""


def matrigel_from(text):
    t = text.lower()
    if "straight matrigel" in t:
        return "straight matrigel"
    if "matrigel" in t or "mgel" in t or re.search(r"(?<![a-z])mg(?![a-z])", t):
        return "matrigel"
    return ""


def context_from(text):
    t = text.lower()
    found = []
    for ph in CONTEXT_PHRASES:
        if ph in t and not any(ph in g for g in found):
            found.append(ph)
    return ", ".join(found)


def picture_label(name, cell_line):
    b = os.path.splitext(name)[0]
    b = re.sub(r"_C\d+_ORG$|_ORG$", "", b, flags=re.I)
    b = strip_cellline(b, cell_line)
    b = re.sub(r"(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})", "", b)
    b = re.sub(r"(\d{1,2})[-_.](\d{1,2})[-_.](\d{2,4})", "", b)
    b = re.sub(r"(?<![A-Za-z0-9])P\d+(?![A-Za-z0-9])", "", b, flags=re.I)
    for tok in list(INITIALS) + list(LASTNAME):
        b = re.sub(rf"(?<![A-Za-z]){re.escape(tok)}(?![A-Za-z])", "", b, flags=re.I)
    return re.sub(r"[\s,._-]+", " ", b).strip()


def main(root):
    rows = []
    skipped_dirs = 0
    for dirpath, dirnames, files in os.walk(root):
        keep = [d for d in dirnames if not is_skip(d)]
        skipped_dirs += len(dirnames) - len(keep)
        dirnames[:] = keep

        for f in files:
            if f.startswith(".") or not f.lower().endswith(IMAGE_EXT):
                continue
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root)
            folders = os.path.dirname(rel).split(os.sep) if os.path.dirname(rel) else []
            folder_text = " ".join(folders)
            path_text = folder_text + " " + f

            # cell line: file name first, then folders (deepest first)
            cell = cell_line_from(f)
            if not cell:
                for fld in reversed(folders):
                    cell = cell_line_from(fld)
                    if cell:
                        break
            # date: file name first, then folders (deepest first)
            date = parse_date(f, cell)
            if not date:
                for fld in reversed(folders):
                    date = parse_date(fld, cell)
                    if date:
                        break

            photographer = photographer_from(folders, f)
            passage = extract_passage(f) or extract_passage(folder_text)
            label = picture_label(f, cell)
            matrigel = matrigel_from(path_text)
            context = context_from(folder_text)

            ext = os.path.splitext(f)[1].lstrip(".").lower()
            mag = mag_from_name(f) if ext in ("tif", "tiff") else ""
            optics = optics_from_name(f) if ext in ("tif", "tiff") else ""
            flag = "" if cell else "NO CELL LINE"
            rows.append({
                "filepath": full, "type": ext, "cell_line": cell,
                "photographer": photographer, "date_of_picture": date,
                "passage": passage, "picture_label": label,
                "matrigel_type": matrigel, "folder_context": context,
                "mag_from_name": mag, "optics_from_name": optics, "flag": flag,
            })

    cols = ["filepath", "type", "cell_line", "photographer", "date_of_picture",
            "passage", "picture_label", "matrigel_type", "folder_context",
            "mag_from_name", "optics_from_name", "flag"]
    out = os.path.join(os.getcwd(), "preview_manifest.csv")
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    no_cell = sum(1 for r in rows if not r["cell_line"])
    by_person = {}
    for r in rows:
        p = r["photographer"] or "(none)"
        by_person[p] = by_person.get(p, 0) + 1
    with_ctx = sum(1 for r in rows if r["folder_context"] or r["matrigel_type"])

    print(f"{total} image file(s)  ({skipped_dirs} junk folders skipped)")
    print(f"{no_cell} with NO recognized cell line")
    print(f"{with_ctx} with matrigel/context from folder names")
    print(f"manifest: {out}\n")
    print("by photographer:")
    for p, n in sorted(by_person.items(), key=lambda x: -x[1]):
        print(f"  {n:5d}  {p}")
    print("\nsample rows:")
    for r in rows[:20]:
        print(f"  [{r['cell_line'] or '???':8}] {r['photographer'] or '-':15} "
              f"{r['date_of_picture'] or '----------':10} {r['passage'] or '   ':4}"
              f"{r['matrigel_type'][:8]:9}| {r['picture_label']} {('<'+r['folder_context']+'>') if r['folder_context'] else ''}")
    unmatched = [os.path.basename(r["filepath"]) for r in rows if not r["cell_line"]]
    if unmatched:
        print(f"\nfirst 20 still with no cell line:")
        for u in unmatched[:20]:
            print(f"  {u}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Preview how the Desktop would be catalogued")
    p.add_argument("root", help=r'the Desktop folder, e.g. "D:\Users\zeiss\Desktop"')
    args = p.parse_args()
    main(args.root)
