"""
rethumbnail.py
Regenerate thumbnails straight from the TIFF already stored in S3 (no drive, no
name matching). Fixes records whose thumbnail failed at ingest (huge canvases
that PIL refused, odd pixel modes) or that predate the crop fix.

    python rethumbnail.py --dry-run
    python rethumbnail.py --missing-only          # only records with no thumbnail
    python rethumbnail.py --cell-line "BRL 47"
    python rethumbnail.py                          # every record with a tiff
"""

import argparse
import os
import tempfile

from ingest import BUCKET, s3, table, make_thumbnail


def scan_records():
    names = {"#i": "id", "#c": "cell_line", "#l": "picture_label",
             "#t": "thumbnail_s3_key", "#g": "file_tiff_s3_key"}
    kw = {"ProjectionExpression": "#i,#c,#l,#t,#g", "ExpressionAttributeNames": names}
    items = []
    while True:
        r = table.scan(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def main(dry, only_cell, missing_only):
    recs = scan_records()
    todo = [r for r in recs
            if r.get("file_tiff_s3_key")
            and (not only_cell or r.get("cell_line") == only_cell)
            and (not missing_only or not r.get("thumbnail_s3_key"))]
    print(f"{len(todo)} record(s) to rethumbnail\n")

    done = fail = 0
    for r in todo:
        print(f"  {r.get('cell_line','')}  {r.get('picture_label','')}")
        if dry:
            done += 1
            continue
        tmp = os.path.join(tempfile.gettempdir(), r["id"] + ".tif")
        try:
            s3.download_file(BUCKET, r["file_tiff_s3_key"], tmp)
            key = r.get("thumbnail_s3_key") or f"thumbnails/{r['id']}.png"
            s3.put_object(Bucket=BUCKET, Key=key,
                          Body=make_thumbnail(tmp), ContentType="image/png")
            if not r.get("thumbnail_s3_key"):
                table.update_item(Key={"id": r["id"]},
                                  UpdateExpression="SET thumbnail_s3_key = :t",
                                  ExpressionAttributeValues={":t": key})
            done += 1
        except Exception as e:
            print(f"    ! failed: {e}")
            fail += 1
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    print(f"\n{done} thumbnail(s) {'would be regenerated' if dry else 'regenerated'}, {fail} failed")
    if dry:
        print("DRY RUN - add no flag to regenerate")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Regenerate thumbnails from the tiffs in S3")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--cell-line", default="")
    p.add_argument("--missing-only", action="store_true")
    args = p.parse_args()
    main(args.dry_run, args.cell_line, args.missing_only)
