"""
czi_convert.py
Render a Zeiss .czi to a viewable image without ZEN, using czifile (already
installed for metadata). This lets the pipeline ingest raw .czi files directly
and produce the .tif, thumbnail, and (for time-lapses) MP4.

How a czi is reduced to something viewable:
  - keep the Y and X axes (the image)
  - if there are multiple timepoints (T > 1) it's a movie -> keep T as frames
  - for every other axis (scene, channel, Z, tile...) take the first index
So a multi-channel or z-stack still becomes its first channel / first plane.

Needs: czifile, imagecodecs, tifffile, numpy, pillow (all in requirements.txt).

Test on one file:
    python czi_convert.py "some_image.czi"
"""

import sys

import numpy as np


def _reduce(path):
    """Return (is_movie, array): (Y,X) for a still, or (T,Y,X) for a movie."""
    import czifile
    with czifile.CziFile(path) as czi:
        full = np.asarray(czi.asarray())
        axes = list(czi.axes)               # one letter per array dimension
    sizes = dict(zip(axes, full.shape))
    is_movie = sizes.get("T", 1) > 1
    sl = []
    for a in axes:
        if a in ("Y", "X"):
            sl.append(slice(None))
        elif a == "T" and is_movie:
            sl.append(slice(None))
        else:
            sl.append(0)                    # first scene / channel / Z / tile
    return is_movie, np.asarray(full[tuple(sl)])


def _to_8bit(a):
    a = a.astype(float)
    lo, hi = np.percentile(a, [1, 99])
    if hi <= lo:
        lo, hi = float(a.min()), float(a.max())
    return (np.clip((a - lo) / (hi - lo + 1e-9), 0, 1) * 255).astype("uint8")


def is_movie(path):
    return _reduce(path)[0]


def can_decode(path):
    """True if we can actually read pixel data from this czi. Some czis are
    compressed by the camera itself in a proprietary codec (compression id
    >= 1000) that czifile/imagecodecs cannot decompress - only ZEN can read
    those. This is the check ingest.py relies on before attempting to render
    a czi-only file into a tif/thumbnail/mp4."""
    try:
        _reduce(path)
        return True
    except Exception:
        return False


def czi_to_tiff(path, out_tiff):
    """Write a still czi's plane to a 16-bit TIFF. Returns True if written,
    False if the file is a movie (which has no single still TIFF)."""
    import tifffile
    movie, arr = _reduce(path)
    if movie:
        return False
    tifffile.imwrite(out_tiff, arr)
    return True


def czi_frames(path):
    """Return a list of 8-bit 2D frames for a movie czi (empty list for a still).
    Note: loads all frames into memory, so very large time-lapses are heavy."""
    movie, arr = _reduce(path)
    return [_to_8bit(arr[i]) for i in range(arr.shape[0])] if movie else []


def czi_thumbnail_png(path, max_px=512):
    """PNG bytes of a downsized preview from the first plane/frame."""
    from io import BytesIO
    from PIL import Image
    movie, arr = _reduce(path)
    plane = arr[0] if movie else arr
    img = Image.fromarray(_to_8bit(plane))
    img.thumbnail((max_px, max_px))
    buf = BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('usage: python czi_convert.py "FILE.czi"')
    movie, arr = _reduce(sys.argv[1])
    print(f"file: {sys.argv[1]}")
    print(f"kind: {'movie' if movie else 'still'}")
    print(f"shape after reduce: {arr.shape}  dtype: {arr.dtype}")
    if not movie:
        out = sys.argv[1].rsplit(".", 1)[0] + "_converted.tif"
        czi_to_tiff(sys.argv[1], out)
        print(f"wrote test TIFF: {out}")
    else:
        print(f"frames: {arr.shape[0]} (would become an MP4)")
