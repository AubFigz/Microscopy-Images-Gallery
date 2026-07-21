"""
relabel.py
Bring already-ingested records up to the current rules WITHOUT re-ingesting
(same record ids, no duplicates). For each record of the given cell line it:
  - re-applies the label fixes (CS -> chamber slide, f;ask -> flask,
    matigel -> matrigel, mammosph -> mammosphere, F# -> flask #, etc.)
  - sets passage to "primary" where it is blank
  - re-derives matrigel_type from the label (pure matrigel, 1:1 matrigel, ...)
    where it is not already set

Dry run by default; add --apply to write the changes.

    python relabel.py "BRL 40"            # preview
    python relabel.py "BRL 40" --apply    # do it

Uses fix_label_text and extract_matrigel from ingest.py, so keep it alongside.
"""

import argparse
import re

from ingest import table, fix_label_text, extract_matrigel


def scan_cell_line(cell):
    names = {"#i": "id", "#c": "cell_line", "#l": "picture_label",
             "#p": "passage", "#m": "matrigel_type"}
    kw = {"ProjectionExpression": "#i,#c,#l,#p,#m", "ExpressionAttributeNames": names,
          "FilterExpression": "#c = :c", "ExpressionAttributeValues": {":c": cell}}
    items = []
    while True:
        r = table.scan(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def main(cell, apply):
    items = scan_cell_line(cell)
    print(f"{len(items)} {cell} record(s)\n")

    changed = 0
    for it in items:
        old_label = it.get("picture_label", "")
        new_label = re.sub(r"[\s,._;:-]+", " ", fix_label_text(old_label)).strip()
        old_pass = it.get("passage", "")
        new_pass = old_pass or "primary"
        old_mat = it.get("matrigel_type", "")
        new_mat = extract_matrigel(old_label) or old_mat        # keep existing if none found

        if new_label == old_label and new_pass == old_pass and new_mat == old_mat:
            continue
        changed += 1
        matnote = f"   matrigel {old_mat or '(blank)'} -> {new_mat}" if new_mat != old_mat else ""
        print(f"  {it['id'][:8]}  \"{old_label}\" -> \"{new_label}\"   "
              f"passage {old_pass or '(blank)'} -> {new_pass}{matnote}")
        if apply:
            table.update_item(
                Key={"id": it["id"]},
                UpdateExpression="SET #l = :l, #p = :p, #m = :m",
                ExpressionAttributeNames={"#l": "picture_label", "#p": "passage",
                                          "#m": "matrigel_type"},
                ExpressionAttributeValues={":l": new_label, ":p": new_pass, ":m": new_mat})

    print(f"\n{changed} record(s) {'updated' if apply else 'would change'}")
    if not apply:
        print("DRY RUN - add --apply to write the changes")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Re-apply label/passage/matrigel rules in place")
    p.add_argument("cell_line", help='e.g. "BRL 40"')
    p.add_argument("--apply", action="store_true", help="write the changes")
    args = p.parse_args()
    main(args.cell_line, args.apply)
