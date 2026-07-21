#!/bin/bash
# Double-click this to start the Microscopy Gallery.
# A browser tab opens automatically. Close this window to stop the gallery.

cd "$(dirname "$0")"

# use the local Python environment if one was set up here
if [ -f venv/bin/activate ]; then
  source venv/bin/activate
fi

# open the browser a couple seconds after the server starts
( sleep 2 && open "http://127.0.0.1:5000" ) &

echo "Starting the Microscopy Gallery..."
echo "A browser tab will open. Leave this window open while you use it."
echo "To stop, close this window."
python3 app.py
