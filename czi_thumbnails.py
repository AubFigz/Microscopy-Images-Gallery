"""
czi_thumbnails.py
Render a viewable TIFF + thumbnail from the czi for czi-only records that were
uploaded before czi rendering existed (they show 'no preview'). Re-walks the
drive, matches each czi-only record by cell line + date + label, renders from
the local czi, uploads, and updates the record.

Keep next to ingest.py and czi_convert.py. Needs czifile, imagecodecs, tifffile.

Preview:  python czi_thumbnails.py "/Volumes/Zimberg Lab/2025 Tiff conversion" --dry-run
Apply:    python czi_thumbnails.py "/Volumes/Zimberg Lab/2025 Tiff conversion"
"""

import argparse
import os
import tempfile

import czi_convert
from ingest import (BUCKET, s3, table, collect, cell_line_label, parse_date,
                    picture_label, normlabel, norm_key, make_movie_mp4)


def czionly_records():
    """(cell_line, date, normlabel) -> record, for czi-only records missing a thumbnail."""
    names = {"#i": "id", "#c": "cell_line", "#d": "date_of_picture", "#l": "picture_label",
             "#a": "acquisition_type", "#t": "thumbnail_s3_key"}
    kw = {"ProjectionExpression": "#i,#c,#d,#l,#a,#t", "ExpressionAttributeNames": names}
    out = {}
    while True:
        r = table.scan(**kw)
        for it in r["Items"]:
            if it.get("acquisition_type") == "czi_only" and not it.get("thumbnail_s3_key"):
                out[(it.get("cell_line", ""), it.get("date_of_picture", ""),
                     normlabel(it.get("picture_label", "")))] = it
        if "LastEvaluatedKey" not in r:
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def main(root, dry_run):
    recs = czionly_records()
    print(f"{len(recs)} czi-only record(s) missing a thumbnail\n")
    folders = [d for d in sorted(os.listdir(root))
               if d.endswith("tiff convert") and os.path.isdir(os.path.join(root, d))]

    done = missing = 0
    for folder_name in folders:
        cell_line = cell_line_label(folder_name)
        for g in collect(os.path.join(root, folder_name)):
            if g["frames"] or not g.get("czi"):        # only czi-only (no frames)
                continue
            czi = g["czi"]
            name = os.path.basename(czi)
            key = (cell_line, parse_date(name, cell_line), normlabel(picture_label(name, cell_line)))
            rec = recs.get(key)
            if not rec:
                continue
            print(f"  render: {cell_line}  {os.path.basename(czi)}")
            if dry_run:
                if czi_convert.can_decode(czi):
                    done += 1
                else:
                    print(f"    ! camera codec, cannot decode - only ZEN can read it, skipping")
                    missing += 1
                continue

            if not czi_convert.can_decode(czi):
                print(f"    ! camera codec, cannot decode - only ZEN can read it, skipping")
                missing += 1
                continue

            prefix = f"{norm_key(cell_line)}/{rec.get('date_of_picture') or 'undated'}/{rec['id']}"
            tif_key, mp4_key = "", ""
            try:
                if czi_convert.is_movie(czi):
                    frames = czi_convert.czi_frames(czi)
                    tmp_dir = tempfile.mkdtemp()
                    import tifffile as _tf
                    paths = []
                    for n, fr in enumerate(frames):
                        p = os.path.join(tmp_dir, f"f{n:05d}.tif")
                        _tf.imwrite(p, fr)
                        paths.append(p)
                    tif_key = f"{prefix}.tif"
                    s3.upload_file(paths[0], BUCKET, tif_key)
                    tmp_mp4 = os.path.join(tempfile.gettempdir(), f"{rec['id']}.mp4")
                    make_movie_mp4(paths, tmp_mp4)
                    mp4_key = f"{prefix}.mp4"
                    s3.upload_file(tmp_mp4, BUCKET, mp4_key, ExtraArgs={"ContentType": "video/mp4"})
                    os.remove(tmp_mp4)
                    for p in paths:
                        os.remove(p)
                    os.rmdir(tmp_dir)
                    print(f"    rendered czi movie -> {len(frames)} frames -> mp4")
                else:
                    tmp_tif = os.path.join(tempfile.gettempdir(), f"{rec['id']}.tif")
                    if czi_convert.czi_to_tiff(czi, tmp_tif):
                        tif_key = f"{prefix}.tif"
                        s3.upload_file(tmp_tif, BUCKET, tif_key)
                        os.remove(tmp_tif)
            except Exception as e:
                print(f"    ! render failed: {type(e).__name__}: {e} - skipping")
                missing += 1
                continue

            thumb_key = f"thumbnails/{rec['id']}.png"
            s3.put_object(Bucket=BUCKET, Key=thumb_key,
                          Body=czi_convert.czi_thumbnail_png(czi), ContentType="image/png")
            expr = "SET thumbnail_s3_key = :t"
            vals = {":t": thumb_key}
            if tif_key:
                expr += ", file_tiff_s3_key = :f"
                vals[":f"] = tif_key
            if mp4_key:
                expr += ", file_mp4_s3_key = :m"
                vals[":m"] = mp4_key
            table.update_item(Key={"id": rec["id"]}, UpdateExpression=expr,
                              ExpressionAttributeValues=vals)
            done += 1

    print(f"\n{done} record(s) {'would be rendered' if dry_run else 'rendered'}")
    if dry_run:
        print("DRY RUN - add no flag to render and upload")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Backfill tif+thumbnail for czi-only records")
    p.add_argument("root", help='the "2025 Tiff conversion" folder')
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    main(args.root, args.dry_run)
