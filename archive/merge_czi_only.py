"""
merge_czi_only.py
Some acquisitions were split into two rows: a czi-only record (metadata, no
image) and a still record (image, no metadata), because the czi and tif folder
names were punctuated differently. This merges them by matching on the fields
they DO share, date_of_picture + normalized picture_label, which is unambiguous
for the day-series timepoints.

For each czi-only record with exactly one still sharing that date+label, it
copies the czi file + metadata onto the still and deletes the czi-only row.

Dry run by default (shows what it would do). Add --apply to make changes.

    python merge_czi_only.py            # preview
    python merge_czi_only.py --apply    # do it
"""

import re
import sys

import boto3

REGION = "us-east-1"
TABLE = "Images"
t = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)

# czi-derived fields to move from the czi-only row onto the still
CZI_FIELDS = ["file_czi_s3_key", "czi_metadata", "magnification", "optics_type",
              "objective", "numerical_aperture", "immersion", "microscope",
              "camera", "exposure_ms", "bit_depth", "pixel_size_um",
              "acquisition_date"]


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def scan_all(proj):
    names = {f"#a{i}": a for i, a in enumerate(proj)}
    items, kw = [], {"ProjectionExpression": ",".join(names),
                     "ExpressionAttributeNames": names}
    while True:
        r = t.scan(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def main(apply):
    light = scan_all(["id", "date_of_picture", "picture_label",
                      "acquisition_type", "magnification"])

    stills = {}
    for i in light:
        if i.get("acquisition_type") == "still":
            key = (i.get("date_of_picture", ""), norm(i.get("picture_label", "")))
            stills.setdefault(key, []).append(i)

    czi_only = [i for i in light if i.get("acquisition_type") == "czi_only"]
    merged = ambiguous = nomatch = 0

    for c in czi_only:
        key = (c.get("date_of_picture", ""), norm(c.get("picture_label", "")))
        matches = stills.get(key, [])
        label = c.get("picture_label", "")
        date = c.get("date_of_picture", "")

        if len(matches) != 1:
            tag = "NO still match" if not matches else f"AMBIGUOUS ({len(matches)} stills)"
            print(f"  skip [{tag}]: {date}  {label}")
            nomatch += not matches
            ambiguous += len(matches) > 1
            continue

        still = matches[0]
        print(f"  MERGE: {date}  {label}  ->  still {still['id'][:8]}")
        if apply:
            full = t.get_item(Key={"id": c["id"]})["Item"]
            names, vals, sets = {}, {}, []
            for n, f in enumerate(CZI_FIELDS):
                if full.get(f) not in (None, ""):
                    names[f"#f{n}"] = f
                    vals[f":v{n}"] = full[f]
                    sets.append(f"#f{n} = :v{n}")
            if sets:
                t.update_item(
                    Key={"id": still["id"]},
                    UpdateExpression="SET " + ", ".join(sets),
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=vals,
                )
                t.delete_item(Key={"id": c["id"]})
        merged += 1

    print(f"\n{merged} merge(s), {ambiguous} ambiguous, {nomatch} no-match")
    print("DRY RUN - add --apply to make these changes" if not apply else "APPLIED")


if __name__ == "__main__":
    main("--apply" in sys.argv)
