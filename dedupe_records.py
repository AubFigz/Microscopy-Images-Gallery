"""
dedupe_records.py
Remove duplicate catalog records for a cell line, where the same acquisition was
exported twice by ZEN (verified byte-identical with compare_dupes.py first!).

For each group of records sharing cell_line + date + label + type, it keeps the
best one (prefers a record that has an mp4 / thumbnail / tif and the highest
frame_count) and deletes the rest.

Only DynamoDB records are deleted. S3 files are left alone on purpose: the
duplicate exports share the same czi object, so deleting S3 keys could remove a
file the surviving record still points to. Orphaned tif/thumbnail objects are
harmless (a few cents of storage).

    python dedupe_records.py "BRL 41"           # preview
    python dedupe_records.py "BRL 41" --apply   # delete the duplicates

ALWAYS run compare_dupes.py for that cell line first and confirm 0 DIFFERENT.
"""

import argparse
import re
from collections import defaultdict

from ingest import table


def normlabel(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def scan_cell_line(cell):
    names = {"#i": "id", "#c": "cell_line", "#d": "date_of_picture", "#l": "picture_label",
             "#a": "acquisition_type", "#f": "frame_count", "#t": "thumbnail_s3_key",
             "#m": "file_mp4_s3_key", "#g": "file_tiff_s3_key"}
    kw = {"ProjectionExpression": "#i,#c,#d,#l,#a,#f,#t,#m,#g",
          "ExpressionAttributeNames": names,
          "FilterExpression": "#c = :c", "ExpressionAttributeValues": {":c": cell}}
    items = []
    while True:
        r = table.scan(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def score(rec):
    """Higher is better: prefer records that actually have files attached."""
    return (1 if rec.get("file_mp4_s3_key") else 0,
            1 if rec.get("thumbnail_s3_key") else 0,
            1 if rec.get("file_tiff_s3_key") else 0,
            int(rec.get("frame_count") or 0))


def main(cell, apply):
    items = scan_cell_line(cell)
    groups = defaultdict(list)
    for it in items:
        groups[(it.get("date_of_picture", ""), normlabel(it.get("picture_label", "")),
                it.get("acquisition_type", ""))].append(it)

    dupes = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"{len(items)} {cell} record(s); {len(dupes)} duplicated group(s)\n")

    to_delete = []
    for k, recs in dupes.items():
        recs.sort(key=score, reverse=True)
        keep, drop = recs[0], recs[1:]
        print(f'  "{keep.get("picture_label","")}" [{keep.get("acquisition_type","")}]  '
              f'keep {keep["id"][:8]} (frames={keep.get("frame_count",0)}), '
              f'drop {", ".join(d["id"][:8] for d in drop)}')
        to_delete += drop

    print(f"\n{len(to_delete)} record(s) {'deleted' if apply else 'would be deleted'}; "
          f"{len(items) - len(to_delete)} would remain")
    if apply:
        for d in to_delete:
            table.delete_item(Key={"id": d["id"]})
        print("done - S3 files were left in place (duplicates share the same czi)")
    else:
        print("DRY RUN - add --apply to delete")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Delete duplicate records for a cell line")
    p.add_argument("cell_line", help='e.g. "BRL 41"')
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    main(args.cell_line, args.apply)
