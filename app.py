"""
app.py - local photo gallery for the microscopy catalog.

Browse and filter the catalog visually, view/play and download files, export a
metadata spreadsheet of the current results, and build a selection "cart" to
bulk-download chosen images/movies (optionally with an info spreadsheet).

Runs only on your own machine (127.0.0.1); uses your AWS credentials.

Setup:  pip install flask boto3
Run:    python app.py    ->  http://127.0.0.1:5050   (Ctrl+C to stop)
"""

import csv
import io
import os
import re
import tempfile
import zipfile

from urllib.parse import urlencode

import boto3
from flask import (Flask, request, render_template_string, redirect,
                   url_for, Response, send_file)

REGION = "us-east-1"
TABLE = "Images"
BUCKET = "microscopy-images"
PAGE_SIZE = 120

app = Flask(__name__)
ddb = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
s3 = boto3.client("s3", region_name=REGION)

# every field except the large czi_metadata blob
FIELDS = ["id", "cell_line", "date_of_picture", "picture_label", "passage",
          "acquisition_type", "frame_count", "magnification", "optics_type",
          "objective", "numerical_aperture", "immersion", "microscope", "camera",
          "exposure_ms", "bit_depth", "pixel_size_um", "tp_number", "race_ethnicity",
          "date_specimen_received", "matrigel_type", "growth_medium", "photographer",
          "thumbnail_s3_key", "file_tiff_s3_key", "file_czi_s3_key", "file_mp4_s3_key"]
CSV_COLS = [f for f in FIELDS if f != "thumbnail_s3_key"]


def scan_all():
    names = {f"#a{i}": a for i, a in enumerate(FIELDS)}
    items, kw = [], {"ProjectionExpression": ",".join(names), "ExpressionAttributeNames": names}
    while True:
        r = ddb.scan(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            return items
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def dl_name(item, ext):
    base = (f"{item.get('cell_line','')}_{item.get('date_of_picture','')}_"
            f"{item.get('picture_label','')}")
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-") or "image"
    return f"{base}.{ext}"


# Search synonym groups: any term in a group matches any other in that group.
# The label keeps its own wording; only the search comparison treats these as
# equivalent. Add more groups as needed.
SYNONYMS = {
    "sphere": ["mammosphere", "mammosph", "mamosphere", "mammo", "episphere", "epi",
               "spheroid", "sphere"],
    "duct": ["milk", "duct"],
    "structure": ["structure", "struct"],
    "spillover": ["spill over", "spillover", "spill", "so"],
    "matrigel": ["matrigel", "matigel", "mgel", "mg"],
    "gelatin": ["gelatin", "gel"],
}


def canon(text):
    """Fold synonym terms (whole words only) to a shared root so grouped terms
    match each other, without matching them inside unrelated words."""
    t = str(text).lower()
    for root, alts in SYNONYMS.items():
        for a in sorted(alts, key=len, reverse=True):        # longest first
            t = re.sub(r"\b" + re.escape(a) + r"\b", root, t)
    return t


def presign(key, expires=3600, filename=None, inline=False):
    if not key:
        return ""
    params = {"Bucket": BUCKET, "Key": key}
    if filename:
        params["ResponseContentDisposition"] = f'{"inline" if inline else "attachment"}; filename="{filename}"'
    return s3.generate_presigned_url("get_object", Params=params, ExpiresIn=expires)


def get_filters():
    return {k: request.args.get(k, "").strip() for k in
            ("cell_line", "q", "mag", "optics", "type", "passage", "photographer", "date_from", "date_to")}


def apply_filters(items, f):
    def keep(i):
        if f["cell_line"] and i.get("cell_line", "") != f["cell_line"]:
            return False
        if f["type"] and i.get("acquisition_type", "") != f["type"]:
            return False
        if f["mag"] and i.get("magnification", "") != f["mag"]:
            return False
        if f["optics"] and i.get("optics_type", "") != f["optics"]:
            return False
        if f["passage"] and i.get("passage", "") != f["passage"]:
            return False
        if f["photographer"] and i.get("photographer", "") != f["photographer"]:
            return False
        if f["q"]:
            ql = canon(f["q"])
            if ql and not re.search(r"\b" + re.escape(ql), canon(i.get("picture_label", ""))):
                return False
        d = i.get("date_of_picture", "")
        if f["date_from"] and d and d < f["date_from"]:
            return False
        if f["date_to"] and d and d > f["date_to"]:
            return False
        return True

    out = [i for i in items if keep(i)]
    out.sort(key=lambda x: (x.get("date_of_picture", ""), str(x.get("picture_label", ""))))
    return out


def build_csv(rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_COLS)
    for r in rows:
        w.writerow([r.get(c, "") for c in CSV_COLS])
    return out.getvalue()


@app.route("/")
def index():
    f = get_filters()
    items = scan_all()
    cell_lines = sorted({i.get("cell_line", "") for i in items if i.get("cell_line")})
    mags = sorted({i.get("magnification", "") for i in items if i.get("magnification")})
    optics = sorted({i.get("optics_type", "") for i in items if i.get("optics_type")})
    passages = sorted({i.get("passage", "") for i in items if i.get("passage")})
    types = sorted({i.get("acquisition_type", "") for i in items if i.get("acquisition_type")})
    photographers = sorted({i.get("photographer", "") for i in items if i.get("photographer")})

    matched = apply_filters(items, f)
    total = len(matched)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE
    shown = matched[start:start + PAGE_SIZE]
    for i in shown:
        i["_thumb"] = presign(i.get("thumbnail_s3_key", ""))

    # query string without 'page', so page links can append their own
    base = {k: v for k, v in request.args.items() if k != "page"}
    base_qs = urlencode({k: v for k, v in base.items() if v})

    return render_template_string(
        INDEX, items=shown, total=total, count=len(items), shown=len(shown),
        f=f, cell_lines=cell_lines, mags=mags, optics=optics, passages=passages, types=types, photographers=photographers,
        page_size=PAGE_SIZE, qs=request.query_string.decode(),
        page=page, pages=pages, start=start, base_qs=base_qs)


@app.route("/export.csv")
def export_csv():
    rows = apply_filters(scan_all(), get_filters())
    return Response(build_csv(rows), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=microscopy_catalog.csv"})


@app.route("/download", methods=["POST"])
def download():
    ids = request.form.getlist("ids")
    include_info = request.form.get("info") == "1"
    if not ids:
        return redirect(url_for("index"))

    tmpzip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    rows, used = [], set()
    with zipfile.ZipFile(tmpzip, "w", zipfile.ZIP_DEFLATED) as z:
        for image_id in ids:
            item = ddb.get_item(Key={"id": image_id}).get("Item")
            if not item:
                continue
            rows.append(item)
            is_movie = item.get("acquisition_type") == "movie"
            key = item.get("file_mp4_s3_key") if is_movie else item.get("file_tiff_s3_key")
            ext = "mp4" if is_movie else "tif"
            if not key:                      # fall back to whatever file exists
                key = item.get("file_tiff_s3_key") or item.get("file_czi_s3_key")
                ext = key.rsplit(".", 1)[-1] if key else ""
            if not key:
                continue
            arc = dl_name(item, ext)
            n = 2
            while arc in used:               # avoid duplicate names in the zip
                stem, _, e = arc.rpartition(".")
                arc = f"{stem}-{n}.{e}"
                n += 1
            used.add(arc)
            with tempfile.NamedTemporaryFile(delete=False) as tf:
                s3.download_file(BUCKET, key, tf.name)
            z.write(tf.name, arcname=arc)
            os.remove(tf.name)
        if include_info:
            z.writestr("image_info.csv", build_csv(rows))

    return send_file(tmpzip.name, as_attachment=True,
                     download_name="microscopy_selection.zip", mimetype="application/zip")


@app.route("/image/<image_id>/info.csv")
def image_csv(image_id):
    item = ddb.get_item(Key={"id": image_id}).get("Item")
    if not item:
        return redirect(url_for("index"))
    item.pop("czi_metadata", None)
    return Response(build_csv([item]), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{dl_name(item, "csv")}"'})


@app.route("/image/<image_id>")
def detail(image_id):
    item = ddb.get_item(Key={"id": image_id}).get("Item")
    if not item:
        return redirect(url_for("index"))
    links = {
        "thumbnail": presign(item.get("thumbnail_s3_key", "")),
        "tiff": presign(item.get("file_tiff_s3_key", ""), filename=dl_name(item, "tif")),
        # a czi skipped for size was never uploaded - do not offer a broken link
        "czi": ("" if str(item.get("czi_upload_pending", "")).strip()
                else presign(item.get("file_czi_s3_key", ""), filename=dl_name(item, "czi"))),
        "mp4": presign(item.get("file_mp4_s3_key", ""), inline=True),
        "mp4_dl": presign(item.get("file_mp4_s3_key", ""), filename=dl_name(item, "mp4")),
    }
    has_czi_meta = "czi_metadata" in item
    hide = {"czi_metadata", "thumbnail_s3_key", "file_tiff_s3_key",
            "file_czi_s3_key", "file_mp4_s3_key"}
    fields = {k: v for k, v in sorted(item.items()) if k not in hide and not k.startswith("_")}
    return render_template_string(DETAIL, item=item, fields=fields, links=links,
                                  has_czi_meta=has_czi_meta)


BASE_CSS = """
:root{--ink:#14202b;--muted:#5a6b78;--paper:#f6f8f9;--card:#fff;--line:#e3e9ed;
  --teal:#0e7c86;--teal-ink:#0a5960;--chip:#eef4f5}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.mono{font-family:"SF Mono",SFMono-Regular,Menlo,Consolas,monospace}
a{color:var(--teal-ink);text-decoration:none}
header{position:sticky;top:0;z-index:5;background:rgba(246,248,249,.92);
  backdrop-filter:blur(6px);border-bottom:1px solid var(--line);padding:14px 22px}
.brand{display:flex;align-items:baseline;gap:10px}
.brand b{font-size:16px;letter-spacing:.2px}
.brand span{color:var(--muted);font-size:12.5px;letter-spacing:.4px;text-transform:uppercase}
form.filters{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;margin-top:12px}
.field{display:flex;flex-direction:column;gap:3px}
.field label{font-size:11px;letter-spacing:.5px;text-transform:uppercase;color:var(--muted)}
.field input,.field select{border:1px solid var(--line);background:var(--card);
  border-radius:7px;padding:7px 9px;font-size:13.5px;color:var(--ink);min-width:120px}
.field input:focus,.field select:focus{outline:2px solid var(--teal);border-color:var(--teal)}
.btn{background:var(--teal);color:#fff;border:none;border-radius:7px;
  padding:8px 16px;font-size:13.5px;font-weight:600;cursor:pointer}
.btn:hover{background:var(--teal-ink)}
.link{align-self:center;font-size:13px;color:var(--muted)}
.count{padding:16px 22px 4px;color:var(--muted);font-size:13px}
.count b{color:var(--ink)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));
  gap:16px;padding:14px 22px 90px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
  overflow:hidden;transition:transform .12s,box-shadow .12s;position:relative}
.card:hover{transform:translateY(-2px);box-shadow:0 6px 20px rgba(20,32,43,.10)}
.pick{position:absolute;top:8px;left:8px;z-index:2;width:22px;height:22px;cursor:pointer;
  accent-color:var(--teal)}
.card a{display:block;color:inherit}
.thumb{aspect-ratio:1/1;background:#0e161d;display:flex;align-items:center;
  justify-content:center;overflow:hidden}
.thumb img{width:100%;height:100%;object-fit:cover}
.thumb .none{color:#54636e;font-size:12px}
.label{padding:9px 11px 11px;border-top:3px solid var(--teal)}
.label .t{font-weight:600;font-size:13.5px;line-height:1.3;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:35px}
.label .m{margin-top:5px;color:var(--muted);font-size:11.5px;display:flex;
  justify-content:space-between;gap:6px}
.chips{margin-top:7px;display:flex;flex-wrap:wrap;gap:5px}
.chip{background:var(--chip);color:var(--teal-ink);border-radius:20px;
  padding:2px 8px;font-size:10.5px;font-weight:600;letter-spacing:.3px}
.empty{padding:60px 22px;text-align:center;color:var(--muted)}
.pager{display:flex;align-items:center;justify-content:center;gap:18px;
  padding:6px 22px 80px;font-size:13.5px}
.pager a{border:1px solid var(--line);background:var(--card);border-radius:7px;
  padding:8px 16px;font-weight:600}
.pager a:hover{border-color:var(--teal)}
.pager .off{color:#b7c2c9;padding:8px 16px}
.pager .pg{color:var(--muted)}
#cart{position:fixed;left:0;right:0;bottom:0;z-index:9;display:none;
  background:var(--ink);color:#fff;padding:12px 22px;align-items:center;gap:14px;
  box-shadow:0 -4px 18px rgba(20,32,43,.18)}
#cart.show{display:flex}
#cart .n{font-weight:600}
#cart .sp{flex:1}
#cart button{border:1px solid rgba(255,255,255,.35);background:transparent;color:#fff;
  border-radius:7px;padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer}
#cart button.solid{background:var(--teal);border-color:var(--teal)}
#cart button:hover{border-color:#fff}
"""

INDEX = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Microscopy Catalog</title><style>""" + BASE_CSS + """</style></head><body>
<header>
  <div class=brand><b>Latimer Lab</b><span>Microscopy Catalog</span></div>
  <form class=filters method=get>
    <div class=field><label>Cell line</label>
      <select name=cell_line><option value="">All</option>
      {% for c in cell_lines %}<option {{'selected' if c==f.cell_line}}>{{c}}</option>{% endfor %}</select></div>
    <div class=field><label>Label contains</label>
      <input name=q value="{{f.q}}" placeholder="duct, primary, episphere..."></div>
    <div class=field><label>Magnification</label>
      <select name=mag><option value="">Any</option>
      {% for m in mags %}<option {{'selected' if m==f.mag}}>{{m}}</option>{% endfor %}</select></div>
    <div class=field><label>Optics</label>
      <select name=optics><option value="">Any</option>
      {% for o in optics %}<option {{'selected' if o==f.optics}}>{{o}}</option>{% endfor %}</select></div>
    <div class=field><label>Passage</label>
      <select name=passage><option value="">Any</option>
      {% for p in passages %}<option {{'selected' if p==f.passage}}>{{p}}</option>{% endfor %}</select></div>
    <div class=field><label>Photographer</label>
      <select name=photographer><option value="">Any</option>
      {% for p in photographers %}<option {{'selected' if p==f.photographer}}>{{p}}</option>{% endfor %}</select></div>
    <div class=field><label>Type</label>
      <select name=type><option value="">Any</option>
      {% for t in types %}<option {{'selected' if t==f.type}}>{{t}}</option>{% endfor %}</select></div>
    <div class=field><label>From</label><input type=date name=date_from value="{{f.date_from}}"></div>
    <div class=field><label>To</label><input type=date name=date_to value="{{f.date_to}}"></div>
    <button class=btn type=submit>Search</button>
    <a class=link href="/">Clear</a>
    <a class=link href="/export.csv?{{qs}}">Download CSV</a>
  </form>
</header>
<div class=count>Showing <b>{{start+1 if total else 0}}-{{start+shown}}</b> of <b>{{total}}</b>
  matching image{{'' if total==1 else 's'}}{% if pages>1 %} &middot; page {{page}} of {{pages}}{% endif %}
  &middot; {{count}} in catalog &middot; tick the boxes to build a download</div>
{% if items %}
<div class=grid>
  {% for i in items %}
  <div class=card>
    <input type=checkbox class=pick data-id="{{i.id}}" title="Select for download">
    <a href="/image/{{i.id}}">
      <div class=thumb>
        {% if i._thumb %}<img src="{{i._thumb}}" loading=lazy alt="">
        {% else %}<span class=none>no preview</span>{% endif %}
      </div>
      <div class=label>
        <div class=t>{{i.picture_label or '(no label)'}}</div>
        <div class=m><span class=mono>{{i.cell_line}}</span><span class=mono>{{i.date_of_picture or '-'}}</span></div>
        <div class=chips>
          {% if i.magnification %}<span class=chip>{{i.magnification}}</span>{% endif %}
          {% if i.optics_type %}<span class=chip>{{i.optics_type}}</span>{% endif %}
          {% if i.passage %}<span class=chip>{{i.passage}}</span>{% endif %}
          {% if i.acquisition_type=='movie' %}<span class=chip>movie</span>{% endif %}
        </div>
      </div>
    </a>
  </div>
  {% endfor %}
</div>
{% else %}
<div class=empty>No images match these filters. Widen them or <a href="/">clear all filters</a>.</div>
{% endif %}
{% if pages>1 %}
<div class=pager>
  {% if page>1 %}<a href="?{{base_qs}}{{'&' if base_qs}}page={{page-1}}">&larr; Previous</a>
  {% else %}<span class=off>&larr; Previous</span>{% endif %}
  <span class=pg>page {{page}} of {{pages}}</span>
  {% if page<pages %}<a href="?{{base_qs}}{{'&' if base_qs}}page={{page+1}}">Next &rarr;</a>
  {% else %}<span class=off>Next &rarr;</span>{% endif %}
</div>
{% endif %}
<div id=cart>
  <span class=n><span id=cn>0</span> selected</span>
  <span class=sp></span>
  <button class=solid onclick="dl(false)">Download images (zip)</button>
  <button onclick="dl(true)">Download images + info (zip)</button>
  <button onclick="clearCart()">Clear</button>
</div>
<script>
function getCart(){try{return JSON.parse(localStorage.getItem('cart')||'[]')}catch(e){return[]}}
function setCart(a){localStorage.setItem('cart',JSON.stringify(a));render()}
function render(){
  var c=getCart();document.getElementById('cn').textContent=c.length;
  document.getElementById('cart').className=c.length?'show':'';
  document.querySelectorAll('.pick').forEach(function(cb){cb.checked=c.indexOf(cb.dataset.id)>=0});
}
document.querySelectorAll('.pick').forEach(function(cb){
  cb.addEventListener('click',function(e){e.stopPropagation();
    var c=getCart(),id=cb.dataset.id,x=c.indexOf(id);
    if(x>=0){c.splice(x,1)}else{c.push(id)}setCart(c);});
});
function clearCart(){setCart([])}
function dl(info){
  var ids=getCart();if(!ids.length)return;
  var f=document.createElement('form');f.method='POST';f.action='/download';
  ids.forEach(function(id){var i=document.createElement('input');i.type='hidden';i.name='ids';i.value=id;f.appendChild(i)});
  var inf=document.createElement('input');inf.type='hidden';inf.name='info';inf.value=info?'1':'0';f.appendChild(inf);
  document.body.appendChild(f);f.submit();document.body.removeChild(f);
}
render();
</script>
</body></html>"""

DETAIL = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{{item.picture_label or item.id}}</title><style>""" + BASE_CSS + """
.wrap{max-width:1000px;margin:0 auto;padding:22px}
.back{font-size:13px;color:var(--muted)}
.detail{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:26px;margin-top:14px}
@media(max-width:720px){.detail{grid-template-columns:1fr}}
.preview{background:#0e161d;border-radius:12px;aspect-ratio:1/1;display:flex;
  align-items:center;justify-content:center;overflow:hidden}
.preview img{width:100%;height:100%;object-fit:contain}
.preview video{width:100%;height:100%;object-fit:contain;background:#0e161d}
.preview .none{color:#54636e;font-size:13px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:16px}
table{border-collapse:collapse;width:100%;font-size:13px}
td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
td.k{color:var(--muted);width:42%;text-transform:uppercase;font-size:11px;letter-spacing:.4px}
td.v{font-family:"SF Mono",Menlo,Consolas,monospace;font-size:12.5px;word-break:break-word}
.dl{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}
.dl a{border:1px solid var(--teal);color:var(--teal-ink);border-radius:8px;
  padding:8px 14px;font-size:13px;font-weight:600}
.dl a.solid{background:var(--teal);color:#fff;border-color:var(--teal)}
.note{color:var(--muted);font-size:12px;margin-top:10px}
</style></head><body>
<div class=wrap>
  <a class=back href="javascript:history.back()">&larr; Back to results</a>
  <div class=detail>
    <div>
      <div class=preview>
        {% if links.mp4 %}<video controls preload=metadata poster="{{links.thumbnail}}" src="{{links.mp4}}"></video>
        {% elif links.thumbnail %}<img src="{{links.thumbnail}}" alt="">
        {% else %}<span class=none>no preview</span>{% endif %}
      </div>
      <div class=dl>
        {% if links.mp4_dl %}<a class=solid href="{{links.mp4_dl}}">Download MP4</a>{% endif %}
        {% if links.tiff %}<a class="{{'solid' if not links.mp4_dl else ''}}" href="{{links.tiff}}">Download TIFF</a>{% endif %}
        {% if links.czi %}<a href="{{links.czi}}">Download CZI</a>{% endif %}
        {% if links.thumbnail and not links.mp4 %}<a href="{{links.thumbnail}}">Open thumbnail</a>{% endif %}
        <a href="/image/{{item.id}}/info.csv">Download info (CSV)</a>
      </div>
      {% if item.czi_upload_pending %}<div class=note>The .czi for this
        acquisition was too large to store in the catalog and lives on the lab
        drive. Everything else here (metadata, movie, image) came from it.</div>{% endif %}
      <div class=note>Links are temporary and expire after one hour.
        {% if has_czi_meta %}Full czi metadata is stored on this record.{% endif %}</div>
    </div>
    <div>
      <h1>{{item.picture_label or '(no label)'}}</h1>
      <div class=sub>{{item.cell_line}} &middot; {{item.date_of_picture or 'undated'}}
        &middot; {{item.acquisition_type}}</div>
      <table>
        {% for k,v in fields.items() %}
        <tr><td class=k>{{k}}</td><td class=v>{{v}}</td></tr>
        {% endfor %}
      </table>
    </div>
  </div>
</div>
</body></html>"""

if __name__ == "__main__":
    print("Gallery running at http://127.0.0.1:5050  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=5050, debug=False)
