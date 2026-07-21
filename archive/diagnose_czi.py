"""
diagnose_czi.py
Read-only. Re-scan a cell-line folder, find acquisitions that did NOT get a czi,
and for each check whether a close-named czi exists on the drive.

    python diagnose_czi.py "BRL 47 tiff convert"

Two buckets:
  MISSED  -> a czi with a very similar name exists (matching just failed) -> fixable
  NONE    -> nothing similar on the drive -> genuinely no czi, nothing to fix
Writes nothing to AWS.
"""

import os
import re
import sys
from difflib import SequenceMatcher

from ingest import stem_key, picture_label, cell_line_label, frame_num

ROOT = "/Volumes/Zimberg Lab/2025 Tiff conversion"


def gather(folder):
    by_dir, czis = {}, {}
    for dp, _, files in os.walk(folder):
        for f in files:
            if f.startswith("."):
                continue
            full = os.path.join(dp, f)
            if f.lower().endswith((".tif", ".tiff")):
                by_dir.setdefault(dp, []).append(full)
            elif f.lower().endswith(".czi"):
                czis[stem_key(f)] = full
    return by_dir, czis


def main(folder_name):
    folder = os.path.join(ROOT, folder_name)
    cell_line = cell_line_label(folder_name)
    by_dir, czis = gather(folder)

    missed, none_found = [], []
    for dp, frames in by_dir.items():
        org = [f for f in frames if re.search(r"_ORG\.(tif|tiff)$", f, re.I)]
        frames = org or frames
        first = sorted(frames, key=frame_num)[0]
        s = stem_key(os.path.basename(dp))
        if s in czis:                      # already matched, has czi
            continue

        label = picture_label(os.path.basename(first), cell_line)
        best_stem, best_r = "", 0.0
        for cs in czis:
            r = SequenceMatcher(None, s, cs).ratio()
            if r > best_r:
                best_stem, best_r = cs, r
        entry = (label, os.path.basename(dp), best_stem, best_r)
        (missed if best_r >= 0.80 else none_found).append(entry)

    total = len(missed) + len(none_found)
    print(f"{total} acquisition(s) without a matched czi "
          f"({len(czis)} czis on drive)\n")

    print(f"=== likely MISSED match (a close czi exists): {len(missed)} ===")
    for label, folder_name_, best_stem, r in sorted(missed, key=lambda x: -x[3]):
        print(f"  [{r:.2f}]  {label}")
        print(f"          folder: {folder_name_}")
        print(f"          czi   : {os.path.basename(czis[best_stem])}")

    print(f"\n=== probably NO czi on drive: {len(none_found)} ===")
    for label, folder_name_, best_stem, r in sorted(none_found, key=lambda x: -x[3]):
        near = os.path.basename(czis[best_stem]) if best_stem else "(none)"
        print(f"  [{r:.2f}]  {label}   folder: {folder_name_}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "BRL 47 tiff convert")
