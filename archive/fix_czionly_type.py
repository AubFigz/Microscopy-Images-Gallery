"""
fix_czionly_type.py
Records ingested as czi_only that now have a rendered tif are really stills -
they just have a tif we generated from the czi rather than one ZEN exported.
This flips acquisition_type to "still" and records the provenance in
tif_source so that fact is not lost.

Only touches records that actually HAVE a tif. A czi_only record with no tif
is left alone, because it genuinely has no viewable image.

    python fix_czionly_type.py            # preview
    python fix_czionly_type.py --apply
"""

import argparse

from ingest import table


def scan_czionly():
    names = {"#i": "id", "#c": "cell_line", "#l": "picture_label",
             "#a": "acquisition_type", "#g": "file_tiff_s3_key", "#s": "tif_source"}
    kw = {"ProjectionExpression": "#i,#c,#l,#a,#g,#s", "ExpressionAttributeNames": names,
          "FilterExpression": "#a = :a", "ExpressionAttributeValues": {":a": "czi_only"}}
    items = []
    while True:
        r = table.scan(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def main(apply):
    recs = scan_czionly()
    with_tif = [r for r in recs if r.get("file_tiff_s3_key")]
    without = [r for r in recs if not r.get("file_tiff_s3_key")]
    print(f"{len(recs)} czi_only record(s): {len(with_tif)} have a tif, "
          f"{len(without)} do not\n")

    for r in with_tif:
        print(f"  -> still: {r.get('cell_line','')}  {r.get('picture_label','')}")
        if apply:
            table.update_item(
                Key={"id": r["id"]},
                UpdateExpression="SET acquisition_type = :t, tif_source = :s",
                ExpressionAttributeValues={":t": "still", ":s": "rendered from czi"})
    for r in without:
        print(f"  leave as czi_only (no tif): {r.get('cell_line','')}  {r.get('picture_label','')}")

    print(f"\n{len(with_tif)} record(s) {'changed to still' if apply else 'would change to still'}")
    if not apply:
        print("DRY RUN - add --apply to write the change")
    else:
        print("Restart the gallery; the Type dropdown builds itself from the data,")
        print("so czi_only disappears once no records use it.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Reclassify rendered czi_only records as stills")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    main(args.apply)
