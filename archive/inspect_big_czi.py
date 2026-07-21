"""
inspect_big_czi.py  -  READ-ONLY. Writes nothing, uploads nothing.

Before trying to make a preview clip from a multi-GB czi movie, find out whether
we can read just the first few timepoints instead of loading the whole file.
This reports what is actually inside the czi and times a single-frame read.

    python inspect_big_czi.py "/Volumes/Zimberg Lab/.../BRL-42 flask day 32.czi"
"""

import os
import sys
import time

import numpy as np


def main(path):
    import czifile
    size_gb = os.path.getsize(path) / (1024 ** 3)
    print(f"file : {os.path.basename(path)}")
    print(f"size : {size_gb:.1f} GB\n")

    t0 = time.time()
    with czifile.CziFile(path) as czi:
        print(f"opened in {time.time()-t0:.1f}s (header only - no pixels yet)")
        axes = "".join(czi.axes)
        print(f"axes : {axes}")
        print(f"shape: {czi.shape}")
        sizes = dict(zip(axes, czi.shape))
        n_t = sizes.get("T", 1)
        print(f"timepoints (T): {n_t}")
        print(f"frame size    : {sizes.get('Y')} x {sizes.get('X')}")
        print(f"dtype         : {czi.dtype}")

        subs = czi.subblock_directory
        print(f"subblocks     : {len(subs)}")

        # can we address subblocks by timepoint?
        t_starts = []
        for sb in subs[:2000]:
            d = {e.dimension: e.start for e in sb.dimension_entries}
            if "T" in d:
                t_starts.append(d["T"])
        if t_starts:
            print(f"T values found on subblocks: min {min(t_starts)}, max {max(t_starts)} "
                  f"({len(set(t_starts))} distinct in the first {len(subs[:2000])})")
            print("=> subblocks ARE addressable by timepoint: a partial read is possible")
        else:
            print("=> no T dimension on subblocks: cannot select timepoints this way")
            return

        # time reading ONE timepoint
        first_t = min(t_starts)
        t0 = time.time()
        got = []
        for sb in subs:
            d = {e.dimension: e.start for e in sb.dimension_entries}
            if d.get("T", 0) == first_t:
                got.append(np.squeeze(sb.data_segment().data()))
                if len(got) >= 4:
                    break
        dt = time.time() - t0
        if got:
            a = got[0]
            print(f"\nread 1 timepoint in {dt:.2f}s -> array {a.shape} {a.dtype}, "
                  f"min {a.min()} max {a.max()}")
            print(f"estimated time for 450 frames (30s of video): {dt*450:.0f}s")
        else:
            print("\ncould not read a timepoint from the subblocks")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('usage: python inspect_big_czi.py "FILE.czi"')
    main(sys.argv[1])
