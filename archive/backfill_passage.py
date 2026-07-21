"""
backfill_passage.py
Fill the passage field on records that were ingested before passage parsing
existed. It re-reads the source file names, extracts a passage token ONLY when
it is unambiguous (a standalone P## like P2 or P4, e.g. "BRL47 P4 2-16-26 17";
it will NOT treat "flask P" as passage), and writes it onto the matching record.

To stay safe it only updates a record when that record's stored label already
contains the same P## token, so nothing is guessed across acquisitions.

Keep next to ingest.py.

Preview:  python backfill_passage.py "/Volumes/Zimberg Lab/2025 Tiff conversion" --dry-run
Apply:    python backfill_passage.py "/Volumes/Zimberg Lab/2025 Tiff conversion"
"""

import argparse
import os

from ingest import (table, collect, cell_line_label, parse_date,
                    extract_passage, norm_key)


def records_by_key():
    """(cell_line, date) -> list of {id, label, passage}."""
    names = {"#i": "id", "#c": "cell_line", "#d": "date_of_picture",
             "#l": "picture_label", "#p": "passage"}
    kw = {"ProjectionExpression": "#i,#c,#d,#l,#p", "ExpressionAttributeNames": names}
    out = {}
    while True:
        r = table.scan(**kw)
        for it in r["Items"]:
            out.setdefault((it.get("cell_line", ""), it.get("date_of_picture", "")), []).append(it)
        if "LastEvaluatedKey" not in r:
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def main(root, dry_run):
    recs = records_by_key()
    folders = [d for d in sorted(os.listdir(root))
               if d.endswith("tiff convert") and os.path.isdir(os.path.join(root, d))]

    updated = skipped = 0
    seen = set()
    for folder_name in folders:
        cell_line = cell_line_label(folder_name)
        for g in collect(os.path.join(root, folder_name)):
            frames = g["frames"]
            src = os.path.basename(frames[0] if frames else g["czi"])
            passage = extract_passage(src)
            if not passage:
                continue
            date_pic = parse_date(src, cell_line)
            for rec in recs.get((cell_line, date_pic), []):
                label = str(rec.get("picture_label", ""))
                # only tag records whose own label already shows this passage token
                if passage.lower() not in label.lower().replace(" ", ""):
                    continue
                if rec.get("passage"):
                    skipped += 1
                    continue
                if rec["id"] in seen:
                    continue
                seen.add(rec["id"])
                print(f"  set passage={passage}: {cell_line} {date_pic}  {label}")
                if not dry_run:
                    table.update_item(
                        Key={"id": rec["id"]},
                        UpdateExpression="SET passage = :p",
                        ExpressionAttributeValues={":p": passage})
                updated += 1

    print(f"\n{updated} record(s) {'would get' if dry_run else 'got'} a passage, "
          f"{skipped} already had one")
    if dry_run:
        print("DRY RUN - add no flag to apply")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Backfill passage on older records")
    p.add_argument("root", help='the "2025 Tiff conversion" folder')
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    main(args.root, args.dry_run)
