"""
delete_record.py
Delete one record from the catalog and its S3 files (tif / czi / mp4 /
thumbnail). Before deleting any S3 object it checks no OTHER record points at
the same key, so a shared czi is never removed out from under another record.

Find the id on the image's detail page in the gallery.

    python delete_record.py d1d140b3-30f6-5660-9465-d5219990cdee            # preview
    python delete_record.py d1d140b3-30f6-5660-9465-d5219990cdee --apply
"""

import argparse

from ingest import BUCKET, s3, table

FILE_FIELDS = ["file_tiff_s3_key", "file_czi_s3_key", "file_mp4_s3_key", "thumbnail_s3_key"]


def all_keys_in_use(exclude_id):
    """Every S3 key referenced by any other record."""
    names = {f"#f{i}": f for i, f in enumerate(FILE_FIELDS)}
    names["#i"] = "id"
    kw = {"ProjectionExpression": ",".join(names), "ExpressionAttributeNames": names}
    used = set()
    while True:
        r = table.scan(**kw)
        for it in r["Items"]:
            if it.get("id") == exclude_id:
                continue
            for f in FILE_FIELDS:
                if it.get(f):
                    used.add(it[f])
        if "LastEvaluatedKey" not in r:
            return used
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def main(image_id, apply):
    rec = table.get_item(Key={"id": image_id}).get("Item")
    if not rec:
        print(f"no record with id {image_id}")
        return
    print(f'record: {rec.get("cell_line","")}  "{rec.get("picture_label","")}"  '
          f'{rec.get("date_of_picture","")}  [{rec.get("acquisition_type","")}]\n')

    used_elsewhere = all_keys_in_use(image_id)
    to_delete, shared = [], []
    for f in FILE_FIELDS:
        k = rec.get(f)
        if not k:
            continue
        (shared if k in used_elsewhere else to_delete).append((f, k))

    for f, k in to_delete:
        print(f"  delete S3: {f} -> {k}")
    for f, k in shared:
        print(f"  KEEP S3 (another record uses it): {f} -> {k}")

    if not apply:
        print("\nDRY RUN - add --apply to delete the record and the S3 files above")
        return

    for _, k in to_delete:
        s3.delete_object(Bucket=BUCKET, Key=k)
    table.delete_item(Key={"id": image_id})
    print(f"\ndeleted record {image_id} and {len(to_delete)} S3 file(s)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Delete a record and its S3 files")
    p.add_argument("image_id")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    main(args.image_id, args.apply)
