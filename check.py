"""
check.py
Summarize the Images table and print a link to view one image.

    python check.py           # summary + samples + one viewable link
    python check.py --wipe     # delete ALL records (asks nothing; use with care)

Both paginate, so they work correctly once the table has hundreds of rows.
"""

import sys
import boto3

REGION = "us-east-1"
TABLE = "Images"
BUCKET = "microscopy-images"

ddb = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
s3 = boto3.client("s3", region_name=REGION)

# light projection (skip the big czi_metadata blob) so pages hold many rows
ATTRS = ["id", "date_of_picture", "acquisition_type", "magnification", "optics_type",
         "picture_label", "file_tiff_s3_key", "file_czi_s3_key", "thumbnail_s3_key"]
NAMES = {f"#a{i}": a for i, a in enumerate(ATTRS)}
PROJ = ",".join(NAMES)


def scan_all():
    items, kw = [], {"ProjectionExpression": PROJ, "ExpressionAttributeNames": NAMES}
    while True:
        r = ddb.scan(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def wipe():
    kw, n = {"ProjectionExpression": "id"}, 0
    while True:
        r = ddb.scan(**kw)
        for i in r["Items"]:
            ddb.delete_item(Key={"id": i["id"]})
            n += 1
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    print(f"deleted {n} record(s)")


if __name__ == "__main__":
    if "--wipe" in sys.argv:
        wipe()
        sys.exit(0)

    items = scan_all()
    print(f"{len(items)} record(s)\n")

    # breakdown by type + how many carry czi metadata (magnification present as proxy)
    by_type, with_meta = {}, 0
    for i in items:
        by_type[i.get("acquisition_type", "?")] = by_type.get(i.get("acquisition_type", "?"), 0) + 1
        if i.get("magnification"):
            with_meta += 1
    print("by type:", ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))
    print(f"with czi imaging metadata: {with_meta} / {len(items)}\n")

    print("sample (first 10 by date):")
    for i in sorted(items, key=lambda x: x.get("date_of_picture", ""))[:10]:
        print(f"  {i.get('date_of_picture', '(undated)')}  [{i.get('acquisition_type', '')}]  "
              f"{i.get('magnification', '')} {i.get('optics_type', '')}  |  {i.get('picture_label', '')}")

    for i in items:
        key = i.get("thumbnail_s3_key") or i.get("file_tiff_s3_key")
        if key:
            url = s3.generate_presigned_url(
                "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=3600)
            print(f"\nView one image ({i.get('picture_label', '')}), valid 1 hour:\n{url}")
            break
