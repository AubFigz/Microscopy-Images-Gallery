#!/bin/bash
# run_upload.sh — incremental upload of all cell-line folders to S3 + DynamoDB.
# Safe to run repeatedly (and on a schedule): only new images are added.

cd "/Users/af/microscopy" || exit 1
source venv/bin/activate

ROOT="/Volumes/Lab/2025"
CSV="/Volumes/Lab/Cell Lines 2.csv"

if [ ! -d "$ROOT" ]; then
  echo "$(date): image drive not mounted at $ROOT — skipping"
  exit 1
fi

echo "$(date): starting incremental upload"
for folder in "$ROOT"/*"tiff convert"; do
  [ -d "$folder" ] || continue
  name=$(basename "$folder")
  echo "$(date): ingesting $name"
  python ingest.py "$ROOT" "$CSV" --cell-line-folder "$name"
done
echo "$(date): done"
