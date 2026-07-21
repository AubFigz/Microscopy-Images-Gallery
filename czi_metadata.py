"""
czi_metadata.py
Extract metadata from a Zeiss .czi file: the full metadata blob (which becomes
the schema's czi_metadata field / a DynamoDB Map), plus the handful of promoted
fields you actually search on (magnification, optics_type, acquisition date).

Only BRL 47 has .czi files right now, so this runs on those.

Verified against current libraries (July 2026):
    pip install czifile xmltodict
czifile.CziFile(path).metadata() returns the raw ZEN metadata XML; we parse it
to a nested dict with xmltodict and pull fields by tag name, tolerant to where
ZEISS places them in the tree.

Library use:
    from czi_metadata import extract_czi_metadata
    promoted, full = extract_czi_metadata("file.czi")
    # promoted -> {"magnification": "20x", "optics_type": "Fluorescence", ...}
    # full     -> nested dict of the ENTIRE czi metadata (store as czi_metadata)

Test on one file from the shell (prints what it found):
    python czi_metadata.py "BRL 47 ... Change Scaling-234.czi"
"""

import json
import sys


def _iter_nodes(node, target):
    """Yield every value whose key == target, anywhere in the nested structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == target:
                yield v
            yield from _iter_nodes(v, target)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_nodes(item, target)


def _first(node, target):
    """First non-empty value for a tag, handling elements that carry attributes."""
    for v in _iter_nodes(node, target):
        if isinstance(v, dict):
            t = v.get("text") or v.get("#text")
            if t:
                return str(t)
        elif v not in (None, "", [], {}):
            return str(v)
    return ""


def _sanitize(node):
    """Make the parsed metadata safe for DynamoDB: strip @/# prefixes, drop None."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            k = str(k).lstrip("@#")   # xmltodict marks attributes @ and text #text
            sv = _sanitize(v)
            if sv not in (None, "", {}, []):
                out[k] = sv
        return out
    if isinstance(node, list):
        return [s for s in (_sanitize(x) for x in node) if s not in (None, "", {}, [])]
    if node is None:
        return ""
    return str(node)


def extract_czi_metadata(path):
    import czifile
    import xmltodict
    import xml.etree.ElementTree as ET

    czi = czifile.CziFile(path)
    try:
        md = czi.metadata()           # newer czifile: str; older: bytes or Element
    finally:
        czi.close()

    if isinstance(md, bytes):
        md = md.decode("utf-8", "replace")
    elif not isinstance(md, str):     # older czifile returns an ElementTree Element
        md = ET.tostring(md, encoding="unicode")

    full = _sanitize(xmltodict.parse(md))
    return _promote(full), full


def _text(node):
    """Value of a tag whether it's a bare string or an element with attributes."""
    if isinstance(node, dict):
        return node.get("text") or node.get("#text") or ""
    return "" if node is None else str(node)


def _as_list(node):
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def _by_id(items, id_val):
    for it in _as_list(items):
        if isinstance(it, dict) and _text(it.get("Id")) == id_val:
            return it
    return {}


def _fmt_mag(value):
    try:
        m = float(value)
        return f"{int(m)}x" if m.is_integer() else f"{m}x"
    except (TypeError, ValueError):
        return value or ""


def _promote(full):
    """Pull the handful of searchable fields, reading the SELECTED objective and
    condenser so values reflect the acquisition that was actually used, not just
    the first hardware slot listed. Every field is best-effort."""
    promoted = {}

    def safe(key, fn):
        try:
            v = fn()
            if v not in (None, ""):
                promoted[key] = v
        except Exception:
            pass

    hw = full.get("ImageDocument", {}).get("Metadata", {}).get("HardwareSetting", {})
    cols = _as_list(hw.get("ParameterCollection"))
    devices = _as_list(hw.get("Configuration", {}).get("Device"))

    # which objective/condenser positions were selected
    obj_pos = _text(_by_id(cols, "MTBObjectiveChanger").get("Position"))
    objectives = _as_list(
        _by_id(devices, "MTBObjectiveChanger").get("ChangerElements", {}).get("Objective")
    )
    selected = next((o for o in objectives if _text(o.get("Position")) == obj_pos), {})

    # total magnification if present, else the selected objective's magnification
    def _mag():
        for c in cols:
            if isinstance(c, dict) and _text(c.get("TotalMagnification")):
                return _fmt_mag(_text(c.get("TotalMagnification")))
        return _fmt_mag(_text(selected.get("Magnification")))

    safe("magnification", _mag)
    safe("objective", lambda: _text(selected.get("Name")).strip(" -"))
    safe("numerical_aperture", lambda: _text(selected.get("NumericalAperture")))
    safe("immersion", lambda: _text(selected.get("Immersions")))
    # real optics setup: condenser contrast position (e.g. Contrast.PH2 -> PH2)
    safe("optics_type",
         lambda: _text(_by_id(cols, "MTBCondenserContrastChanger").get("PositionName"))
                 .rsplit(".", 1)[-1])
    safe("microscope", lambda: _text(_by_id(devices, "Microscope").get("Name")))
    safe("camera",
         lambda: next((_text(c.get("CameraDisplayName")) for c in cols
                       if isinstance(c, dict) and _text(c.get("CameraDisplayName"))), ""))
    safe("exposure_ms",
         lambda: next((_text(c.get("ExposureTime")) for c in cols
                       if isinstance(c, dict) and "HDCam" in _text(c.get("Id"))), ""))
    safe("bit_depth", lambda: _first(full, "ComponentBitCount") or _first(full, "BitCountRange"))
    safe("acquisition_date", lambda: _first(full, "AcquisitionDateAndTime"))

    # pixel size at the sample = camera pixel size / total magnification (um)
    def _pixel_um():
        cpd = next((_text(c.get("CameraPixelDistances")) for c in cols
                    if isinstance(c, dict) and _text(c.get("CameraPixelDistances"))), "")
        mag = promoted.get("magnification", "").rstrip("x")
        if cpd and mag:
            return str(round(float(cpd.split(",")[0]) / float(mag), 4))
        return ""

    safe("pixel_size_um", _pixel_um)
    return promoted


def flatten(node, prefix=""):
    """Turn the nested metadata into flat 'path = value' rows."""
    rows = []
    if isinstance(node, dict):
        for k, v in node.items():
            rows += flatten(v, f"{prefix}/{k}" if prefix else k)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            rows += flatten(item, f"{prefix}[{i}]")
    else:
        rows.append((prefix, str(node)))
    return rows


if __name__ == "__main__":
    args = sys.argv[1:]

    if args and args[0] == "--dump":
        # list every field in the czi so you can see what's available
        if len(args) != 2:
            sys.exit('usage: python czi_metadata.py --dump "FILE.czi"')
        _, full = extract_czi_metadata(args[1])
        for path, value in flatten(full):
            value = value if len(value) <= 80 else value[:77] + "..."
            print(f"{path} = {value}")
        sys.exit(0)

    if len(args) != 1:
        sys.exit('usage: python czi_metadata.py "FILE.czi"   (or --dump to list all fields)')

    promoted, full = extract_czi_metadata(args[0])
    print("PROMOTED (top-level, searchable):")
    print(json.dumps(promoted, indent=2))
    size = len(json.dumps(full))
    print(f"\nFULL czi_metadata blob: {size} chars"
          f"  (DynamoDB item limit is 400 KB, so fine unless it's huge)")
