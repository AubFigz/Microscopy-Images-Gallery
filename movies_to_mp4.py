"""
movies_to_mp4.py
Generate a web-playable MP4 for movie acquisitions that don't have one yet
(the two BRL 47 time-lapses loaded before MP4 support existed), upload it to
S3, and attach file_mp4_s3_key to the matching DynamoDB record.

Keep next to ingest.py. Needs: imageio, imageio-ffmpeg, tifffile (+ the usual).
    pip install imageio imageio-ffmpeg

Usage (preview what it would do):
    python movies_to_mp4.py "/Volumes/Zimberg Lab/2025 Tiff conversion" --dry-run
Do it for real:
    python movies_to_mp4.py "/Volumes/Zimberg Lab/2025 Tiff conversion"
Re-encode even movies that already have an mp4:
    python movies_to_mp4.py "/Volumes/Zimberg Lab/2025 Tiff conversion" --force
"""

import argparse
import os
import tempfile

from ingest import (BUCKET, s3, table, collect, cell_line_label, parse_date,
                    picture_label, normlabel, norm_key, make_movie_mp4, acq_id)


def movie_records():
    """Movies indexed two ways: by record id, and by (cell_line, date, label).

    The id is the reliable one - it is the same deterministic id ingest.py
    generates from the source path. The date+label fallback exists for records
    ingested before ids were deterministic (BRL 47), and it is unreliable on its
    own because a record's date can come from the czi while the file name has none.
    """
    names = {"#i": "id", "#c": "cell_line", "#d": "date_of_picture",
             "#l": "picture_label", "#a": "acquisition_type", "#m": "file_mp4_s3_key",
             "#g": "file_tiff_s3_key"}
    kw = {"ProjectionExpression": "#i,#c,#d,#l,#a,#m,#g", "ExpressionAttributeNames": names}
    by_id, by_label = {}, {}
    while True:
        r = table.scan(**kw)
        for it in r["Items"]:
            if it.get("acquisition_type") == "movie":
                by_id[it["id"]] = it
                by_label[(it.get("cell_line", ""), it.get("date_of_picture", ""),
                          normlabel(it.get("picture_label", "")))] = it
        if "LastEvaluatedKey" not in r:
            return by_id, by_label
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def main(root, dry_run, force):
    by_id, by_label = movie_records()
    print(f"{len(by_id)} movie record(s) in the catalog\n")

    folders = [d for d in sorted(os.listdir(root))
               if d.endswith("tiff convert") and os.path.isdir(os.path.join(root, d))]

    # Gather every candidate source per record. The drive can hold duplicate
    # exports of the same acquisition (e.g. a partial 3-frame copy next to the
    # real 529-frame movie), and they map to the same record, so keep the
    # candidate with the MOST frames rather than whichever is walked last.
    best = {}          # record id -> (frames, cell_line, label, rec)
    missing = 0
    for folder_name in folders:
        cell_line = cell_line_label(folder_name)
        for g in collect(os.path.join(root, folder_name)):
            frames = g["frames"]
            if len(frames) <= 1:
                continue
            name = os.path.basename(frames[0])
            label = picture_label(name, cell_line)
            # the deterministic id from the source path is the reliable match
            source_rel = os.path.relpath(os.path.dirname(frames[0]), root)
            rec = by_id.get(acq_id(cell_line, source_rel))
            if not rec:      # older records with random ids
                rec = by_label.get((cell_line, parse_date(name, cell_line),
                                    normlabel(label)))
            if not rec:
                print(f"  no matching record: {cell_line}  {label}")
                missing += 1
                continue
            prev = best.get(rec["id"])
            if prev and len(prev[0]) >= len(frames):
                print(f"  skip duplicate source ({len(frames)} frames) for "
                      f"\"{label}\" - keeping the {len(prev[0])}-frame version")
                continue
            if prev:
                print(f"  better source found ({len(frames)} frames > "
                      f"{len(prev[0])}) for \"{label}\"")
            best[rec["id"]] = (frames, cell_line, label, rec)

    done = skipped = 0
    for rec_id, (frames, cell_line, label, rec) in best.items():
        if rec.get("file_mp4_s3_key") and not force:
            print(f"  already has mp4: {label}")
            skipped += 1
            continue
        print(f"  encoding {label}  ({len(frames)} frames)...")
        if dry_run:
            done += 1
            continue
        date_pic = rec.get("date_of_picture") or "undated"
        # older movie records were ingested before movies kept a tif; add one so
        # they have a still preview and rethumbnail.py can reach them
        if not rec.get("file_tiff_s3_key"):
            tif_key = f"{norm_key(cell_line)}/{date_pic}/{rec_id}.tif"
            s3.upload_file(frames[0], BUCKET, tif_key)
            table.update_item(Key={"id": rec_id},
                              UpdateExpression="SET file_tiff_s3_key = :t",
                              ExpressionAttributeValues={":t": tif_key})
            print(f"    added missing tif (first frame)")
        tmp = os.path.join(tempfile.gettempdir(), f"{rec_id}.mp4")
        make_movie_mp4(frames, tmp)
        mp4_key = f"{norm_key(cell_line)}/{date_pic}/{rec_id}.mp4"
        s3.upload_file(tmp, BUCKET, mp4_key, ExtraArgs={"ContentType": "video/mp4"})
        os.remove(tmp)
        table.update_item(
            Key={"id": rec_id},
            UpdateExpression="SET file_mp4_s3_key = :m, updated_at = :u",
            ExpressionAttributeValues={
                ":m": mp4_key,
                ":u": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc).isoformat()},
        )
        print(f"    uploaded -> {mp4_key}")
        done += 1

    print(f"\n{done} encoded, {skipped} already had mp4, {missing} with no matching record")
    if dry_run:
        print("DRY RUN - add no flag to actually encode and upload")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Backfill MP4s for existing movie records")
    p.add_argument("root", help='the "2025 Tiff conversion" folder')
    p.add_argument("--dry-run", action="store_true", help="show what would happen")
    p.add_argument("--force", action="store_true", help="re-encode even if an mp4 exists")
    args = p.parse_args()
    main(args.root, args.dry_run, args.force)
