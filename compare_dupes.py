"""
compare_dupes.py  -  READ-ONLY duplicate audit.
For acquisitions that share a name (same base name, different Image Export
number), compare the real image bytes to tell:
  IDENTICAL  = the same acquisition exported twice  -> safe to dedupe
  DIFFERENT  = different images that share a name    -> must keep both

Run one folder:
    python compare_dupes.py "/Volumes/Lab/2025" "BRL 41 tiff convert"
Run EVERY cell-line folder on the drive (audit what's already uploaded):
    python compare_dupes.py "/Volumes/Lab/2025" --all
"""
import hashlib
import os
import re
import sys
from collections import defaultdict


def norm(name):
    n = re.sub(r"\s*-?\s*(image export|change scaling)-\d+.*$", "", name.lower())
    n = re.sub(r"[\s_-]*\bcopy\b\s*$", "", n)
    return re.sub(r"[^a-z0-9]", "", n)


def first_frame(d):
    fs = [f for f in sorted(os.listdir(d))
          if f.lower().endswith((".tif", ".tiff")) and not f.startswith("._")]
    return os.path.join(d, fs[0]) if fs else None


def sig(path):
    h = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(1 << 20)
            if not b:
                break
            h.update(b)
    return os.path.getsize(path), h.hexdigest()


def audit(base, label):
    groups = defaultdict(list)
    for dp, dirs, files in os.walk(base):
        for d in dirs:
            if re.search(r"image export-\d+", d, re.I):
                groups[norm(d)].append(os.path.join(dp, d))
    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    if not dupes:
        print(f"{label:28} no duplicate-named acquisitions")
        return 0, 0
    identical = different = 0
    diff_examples = []
    for k, dirs in dupes.items():
        sigs = []
        for d in dirs:
            f = first_frame(d)
            sigs.append(sig(f) if f else None)
        if any(s is None for s in sigs):
            continue
        if len(set(sigs)) == 1:
            identical += 1
        else:
            different += 1
            if len(diff_examples) < 3:
                diff_examples.append(os.path.basename(dirs[0]))
    verdict = ("SAFE TO DEDUPE" if different == 0 else
               "DO NOT DEDUPE (distinct images share a name)")
    print(f"{label:28} groups={len(dupes):4}  identical={identical:4}  "
          f"different={different:4}   {verdict}")
    for e in diff_examples:
        print(f"{'':30}  e.g. different: {e}")
    return identical, different


if __name__ == "__main__":
    root = sys.argv[1]
    if len(sys.argv) > 2 and sys.argv[2] == "--all":
        folders = [d for d in sorted(os.listdir(root))
                   if d.endswith("tiff convert") and os.path.isdir(os.path.join(root, d))]
        print(f"auditing {len(folders)} cell-line folder(s)\n")
        for f in folders:
            audit(os.path.join(root, f), f)
        print("\nOnly run dedupe_records.py on folders marked SAFE TO DEDUPE.")
    else:
        folder = sys.argv[2]
        audit(os.path.join(root, folder), folder)
