# app.py — EduTap Shorts (Online-ready, single file)
# Clean dark UI, multi-item, Upload/Link, Full/Mid layouts, fixed logo, CPU-friendly.
# Caption rendering uses Pillow (no Playwright/Chromium). Auto-cleans outputs after TTL.

from __future__ import annotations
from pathlib import Path
from flask import Flask, request, render_template_string, send_from_directory, flash
from werkzeug.utils import secure_filename
from PIL import Image, ImageDraw, ImageFont
import subprocess, json, re, requests, tempfile, os, shutil, time, threading
from urllib.parse import urlparse, parse_qs, urlencode

# -------------------- Paths & constants --------------------
APP_DIR = Path(__file__).parent.resolve()
ASSETS_DIR = APP_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
LOGO_PATH = str((ASSETS_DIR / "logo.png").resolve())           # fixed logo (hidden from UI)
OUTPUTS_DIR = APP_DIR / "outputs"                              # public downloads
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

# online-friendly defaults
OUT_W, OUT_H = 1080, 1920
ACCENT = "#00BCD5"

# auto-clean (seconds); Railway can override via env
TTL_SECONDS = int(os.environ.get("TTL_SECONDS", "3600"))   # 1 hour
CLEAN_INTERVAL = int(os.environ.get("CLEAN_INTERVAL", "600"))  # every 10 min

# -------------------- Flask --------------------
app = Flask(__name__)
app.secret_key = "edutap-online-local-only"

# -------------------- Small helpers --------------------
def is_url(s: str) -> bool:
    return isinstance(s, str) and s.lower().startswith(("http://", "https://"))

def _stream_to_file(resp: requests.Response, out_path: str, chunk=1<<20):
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        for b in resp.iter_content(chunk_size=chunk):
            if b:
                f.write(b)

def _gdrive_file_id(url: str):
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None

def _extract_onedrive_tokens(url: str, html: str):
    def pick_from(u: str):
        p = urlparse(u)
        q = parse_qs(p.query); frag = parse_qs(p.fragment)
        cid = (q.get("cid") or frag.get("cid") or [""])[0]
        resid = (q.get("resid") or frag.get("resid") or [""])[0]
        authkey = (q.get("authkey") or frag.get("authkey") or [""])[0]
        return cid, resid, authkey
    cid, resid, authkey = pick_from(url)
    if (not cid or not resid) and html:
        m = re.search(r'property=["\']og:url["\']\s+content=["\']([^"\']+)', html, re.I)
        if m:
            c2, r2, a2 = pick_from(m.group(1))
            cid = cid or c2; resid = resid or r2; authkey = authkey or a2
        if not resid:
            m = re.search(r'https://onedrive\.live\.com/[^"\']*resid=([^"&\']+)', html, re.I)
            if m: resid = m.group(1)
        if not authkey:
            m = re.search(r'authkey=(![A-Za-z0-9-_%]+)', html, re.I)
            if m: authkey = m.group(1)
        if not cid and resid and "!" in resid: cid = resid.split("!")[0]
    return cid, resid, authkey

def _resolve_onedrive_download(url: str, session: requests.Session) -> requests.Response:
    r = session.get(url, allow_redirects=True)
    final_url = r.url
    if "text/html" not in r.headers.get("Content-Type", "") and r.status_code == 200:
        return r
    cid, resid, authkey = _extract_onedrive_tokens(final_url, r.text)
    params = {"cid": cid, "resid": resid}
    if authkey: params["authkey"] = authkey
    dl_url = "https://onedrive.live.com/download?" + urlencode({k:v for k,v in params.items() if v})
    r2 = session.get(dl_url, stream=True, allow_redirects=True, headers={"Referer": final_url})
    if r2.status_code == 403:
        r2 = session.get(dl_url, stream=True, allow_redirects=True)
    r2.raise_for_status()
    return r2

def download_video_to_temp(source: str) -> tuple[str, str]:
    """
    Returns (local_path, tmp_dir_to_cleanup_or_empty).
    If downloaded, tmp_dir is a new temp folder to delete later.
    """
    if not is_url(source):
        return source, ""
    tmpdir = tempfile.mkdtemp(prefix="viddl_")
    out_file = os.path.join(tmpdir, "input.mp4")
    sess = requests.Session(); sess.headers.update({"User-Agent": "Mozilla/5.0"})
    url = source

    if "drive.google.com" in url:
        fid = _gdrive_file_id(url)
        dl_url = f"https://drive.google.com/uc?export=download&id={fid}" if fid else url
        r = sess.get(dl_url, stream=True)
        if "text/html" in r.headers.get("Content-Type", ""):
            m = re.search(r'confirm=([0-9A-Za-z_]+)', r.text)
            if m and fid:
                dl_url = f"https://drive.google.com/uc?export=download&confirm={m.group(1)}&id={fid}"
                r = sess.get(dl_url, stream=True)
        _stream_to_file(r, out_file)
        return out_file, tmpdir

    if "1drv.ms" in url or "onedrive.live.com" in url:
        r = _resolve_onedrive_download(url, sess)
        _stream_to_file(r, out_file)
        return out_file, tmpdir

    r = sess.get(url, stream=True, allow_redirects=True)
    _stream_to_file(r, out_file)
    return out_file, tmpdir

def ffprobe_json(path: str):
    out = subprocess.check_output(
        ["ffprobe","-v","error","-print_format","json","-show_streams","-show_format",path],
        text=True
    )
    return json.loads(out)

def derive_fps(meta) -> int:
    try:
        vstream = next(s for s in meta["streams"] if s.get("codec_type")=="video")
        rfr = vstream.get("r_frame_rate") or "25/1"
        num, den = rfr.split("/")
        fps = max(1, int(round(float(num)/float(den)))) if float(den)!=0 else 25
        return min(max(fps, 10), 60)
    except Exception:
        return 25

def has_nvenc() -> bool:
    try:
        encs = subprocess.check_output(["ffmpeg","-hide_banner","-encoders"], text=True, stderr=subprocess.STDOUT)
        return ("h264_nvenc" in encs)
    except Exception:
        return False

# -------------------- Caption rendering (Pillow) --------------------
def _load_font(size: int) -> ImageFont.FreeTypeFont|ImageFont.ImageFont:
    # Try bundled Poppins SemiBold first; fallback to default PIL font
    candidates = [
        FONTS_DIR / "Poppins-SemiBold.ttf",
        FONTS_DIR / "Poppins-SemiBold.otf",
    ]
    for p in candidates:
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size=size)
            except Exception:
                pass
    return ImageFont.load_default()

def _wrap_text(text: str, font: ImageFont.ImageFont, max_w: int, draw: ImageDraw.ImageDraw):
    # simple word-wrap using font.getlength if available
    words = (text or "").replace("\r","").split()
    lines, curr = [], ""
    for w in words:
        test = (curr + " " + w).strip()
        width = draw.textlength(test, font=font) if hasattr(draw, "textlength") else font.getsize(test)[0]
        if width <= max_w or not curr:
            curr = test
        else:
            lines.append(curr)
            curr = w
    if curr:
        lines.append(curr)
    if not lines:
        lines = [""]
    return lines

def render_caption_png_pillow(text: str, out_path: Path, *,
                              max_width: int = 1000,
                              font_size: int = 52,
                              line_height: float = 1.35,
                              pad_x: int = 18,
                              pad_y: int = 10,
                              radius: int = 8):
    """Render text as a white rounded box with black text onto a transparent PNG."""
    font = _load_font(font_size)
    dummy = Image.new("RGBA", (10,10), (0,0,0,0))
    dr = ImageDraw.Draw(dummy)
    lines = _wrap_text(text, font, max_width, dr)

    # measure
    ascent, descent = font.getmetrics() if hasattr(font, "getmetrics") else (font.size, 0)
    line_px = int((ascent+descent) * line_height) if ascent else int(font.size*line_height)
    text_w = 0
    for ln in lines:
        w = dr.textlength(ln, font=font) if hasattr(dr,"textlength") else font.getsize(ln)[0]
        text_w = max(text_w, int(w))
    text_h = line_px * len(lines)

    box_w = text_w + pad_x*2
    box_h = text_h + pad_y*2

    img = Image.new("RGBA", (box_w, box_h), (0,0,0,0))
    dr2 = ImageDraw.Draw(img)

    # rounded rect (white)
    def rr(draw, xy, r, fill):
        x0,y0,x1,y1 = xy
        draw.rounded_rectangle(xy, r, fill=fill)

    rr(dr2, (0,0,box_w,box_h), radius, (255,255,255,255))

    # draw lines centered horizontally
    y = pad_y
    for ln in lines:
        if hasattr(dr2, "textlength"):
            w = dr2.textlength(ln, font=font)
        else:
            w = font.getsize(ln)[0]
        x = (box_w - int(w))//2
        dr2.text((x,y), ln, font=font, fill=(0,0,0,255))
        y += line_px

    img.save(out_path, "PNG")

# -------------------- Compose (FFmpeg) --------------------
def compose_full(local_video_path: str, caption_png: Path, output_path: Path, logo_path: str):
    meta = ffprobe_json(local_video_path)
    fps = derive_fps(meta)

    # measure caption & logo
    with Image.open(caption_png) as im: cap_w, cap_h = im.size
    with Image.open(logo_path) as lg:
        lw, lh = lg.size
        logo_target = 120
        logo_h = int(round(logo_target * (lh / lw))) if lw else 0

    caption_top_min = 80
    caption_clearance = 20
    gap_below_caption = 40
    logo_margin_x = 40
    logo_margin_y = 40

    caption_y = max(caption_top_min, logo_margin_y + logo_h + caption_clearance)
    video_top = caption_y + cap_h + gap_below_caption
    available_h = OUT_H - video_top
    if available_h < 10:
        raise RuntimeError("Not enough space for video in FULL mode.")

    filter_graph = (
        f"[1:v]scale={OUT_W}:{available_h}:force_original_aspect_ratio=decrease[sv];"
        f"[0:v][sv]overlay=x=(W-w)/2:y={video_top}[bgv];"
        f"[bgv][2:v]overlay=x=(W-w)/2:y={caption_y}:format=auto[bgvcap];"
        f"[3:v]scale=120:-1[logo];"
        f"[bgvcap][logo]overlay=x={logo_margin_x}:y={logo_margin_y}:format=auto"
    )

    vcodec = (["-c:v","h264_nvenc","-preset","p4","-rc","vbr","-cq","19","-b:v","0","-pix_fmt","yuv420p"]
              if has_nvenc() else
              ["-c:v","libx264","-preset","veryfast","-crf","18","-pix_fmt","yuv420p"])

    cmd = [
        "ffmpeg","-y",
        "-f","lavfi","-i", f"color=0x00BCD5:size={OUT_W}x{OUT_H}:rate={fps}",
        "-i", local_video_path,
        "-loop","1","-i", str(caption_png),
        "-i", logo_path,
        "-filter_complex", filter_graph,
        "-shortest",
        *vcodec,
        "-threads","0","-filter_complex_threads","4",
        "-c:a","aac","-b:a","160k",
        "-movflags","+faststart",
        str(output_path)
    ]
    subprocess.check_call(cmd)

def compose_mid(local_video_path: str, caption_png: Path, output_path: Path, logo_path: str):
    meta = ffprobe_json(local_video_path)
    fps = derive_fps(meta)
    vstream = next(s for s in meta["streams"] if s["codec_type"]=="video")
    src_w = int(vstream.get("width")); src_h = int(vstream.get("height"))

    with Image.open(caption_png) as im: cap_w, cap_h = im.size

    vid_x = (OUT_W - src_w) // 2
    vid_y = (OUT_H - src_h) // 2

    mid_logo_offset_y = 60
    mid_text_offset_y = 235

    logo_x = vid_x + (src_w - 120) // 2
    logo_y = vid_y + mid_logo_offset_y

    cap_x = vid_x + (src_w - cap_w) // 2
    cap_y = vid_y + mid_text_offset_y

    filter_graph = (
        f"[0:v][1:v]overlay=x={vid_x}:y={vid_y}[bgv];"
        f"[3:v]scale=120:-1[logo];"
        f"[bgv][logo]overlay=x={logo_x}:y={logo_y}[bgvlogo];"
        f"[bgvlogo][2:v]overlay=x={cap_x}:y={cap_y}:format=auto"
    )

    vcodec = (["-c:v","h264_nvenc","-preset","p3","-rc","vbr","-cq","19","-b:v","0","-pix_fmt","yuv420p"]
              if has_nvenc() else
              ["-c:v","libx264","-preset","veryfast","-crf","18","-pix_fmt","yuv420p"])

    cmd = [
        "ffmpeg","-y",
        "-f","lavfi","-i", f"color=0x00BCD5:size={OUT_W}x{OUT_H}:rate={fps}",
        "-i", local_video_path,
        "-loop","1","-i", str(caption_png),
        "-i", logo_path,
        "-filter_complex", filter_graph,
        "-shortest",
        *vcodec,
        "-threads","0","-filter_complex_threads","4",
        "-c:a","aac","-b:a","160k",
        "-movflags","+faststart",
        str(output_path)
    ]
    subprocess.check_call(cmd)

# -------------------- filenames & cleanup --------------------
def safe_filename_from_text(text: str) -> str:
    if not text: return "video.mp4"
    s = re.sub(r"\s+", " ", text.strip())
    s = re.sub(r'[^A-Za-z0-9 _.-]', '', s)
    s = s[:116].rstrip()
    if not s: s = "video"
    return f"{s}.mp4"

def cleanup_outputs():
    while True:
        try:
            now = time.time()
            for p in OUTPUTS_DIR.glob("*.mp4"):
                if now - p.stat().st_mtime > TTL_SECONDS:
                    p.unlink(missing_ok=True)
            for p in OUTPUTS_DIR.glob("*.png"):
                if now - p.stat().st_mtime > TTL_SECONDS:
                    p.unlink(missing_ok=True)
        except Exception:
            pass
        time.sleep(CLEAN_INTERVAL)

threading.Thread(target=cleanup_outputs, daemon=True).start()

# -------------------- UI --------------------
HTML = f"""
<!doctype html>
<title>EduTap Shorts</title>
<meta name="viewport" content="width=device-width,initial-scale=1" />
<style>
:root {{
  --bg:#0b1118; --panel:#0f1621; --ring:{ACCENT}; --txt:#e8eef6; --mut:#8aa0b6; --card:#0c141e;
}}
*{{box-sizing:border-box}}
body{{
  margin:0;background:radial-gradient(1200px 600px at 15% -10%,#132031 0%,rgba(19,32,49,0)60%),var(--bg);
  font:15px/1.45 system-ui,Segoe UI,Arial;color:var(--txt);
}}
.wrap{{max-width:1200px;margin:38px auto;padding:0 16px}}
.grid{{display:grid;grid-template-columns:1.2fr .9fr;gap:22px}}
@media (max-width:980px){{.grid{{grid-template-columns:1fr;}}}}

.panel{{background:linear-gradient(180deg,rgba(255,255,255,.03),rgba(255,255,255,.02));border:1px solid #1f2b3a;border-radius:18px;box-shadow:0 0 40px rgba(0,188,213,.22);}}
.left{{padding:28px;min-height:520px}}
.right{{padding:18px}}

h1{{margin:0 0 18px;font-weight:900;font-size:44px;letter-spacing:.2px}}
h1 .a{{color:{ACCENT}}}
p.lead{{color:#cde6f7}}

label{{display:block;margin:10px 0 6px;font-weight:700;color:#d8e7f8}}
small{{color:var(--mut)}}
input[type=text], textarea, select{{
  width:100%;padding:14px;border:1px solid #223246;background:#0a1119;color:var(--txt);
  border-radius:12px;outline:none;font-size:15px
}}
textarea{{height:140px;resize:vertical}}

.btn{{display:inline-flex;align-items:center;justify-content:center;gap:10px;background:var(--ring);
  border:0;color:#00323a;padding:14px 18px;border-radius:12px;font-weight:900;cursor:pointer;font-size:16px}}
.btn.subtle{{background:#122232;color:#cfe8f2;border:1px solid #1e3347}}
.controls{{display:flex;gap:12px;align-items:center;margin:10px 0}}
.row{{display:grid;grid-template-columns:1fr;gap:12px}}

.cards{{display:flex;gap:14px;overflow-x:auto;padding-bottom:8px}}
.card{{min-width:460px;max-width:460px;background:var(--card);border:1px solid #173047;border-radius:16px;padding:14px;position:relative}}
@media(max-width:540px){{.card{{min-width:100%;max-width:100%}}}}
.card h4{{margin:0 0 10px}}
.rm{{position:absolute;right:10px;top:10px;width:28px;height:28px;border-radius:8px;border:1px solid #244;cursor:pointer;background:#101b26;color:#9bc}}
.mode{{display:flex;gap:12px;margin:8px 0}}
.hidden{{display:none}}

#overlay{{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;align-items:center;justify-content:center;z-index:999}}
.spinner{{width:64px;height:64px;border:6px solid #fff;border-top-color:{ACCENT};border-radius:50%;animation:spin 1s linear infinite}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
#overlay p{{color:#fff;margin-top:12px;text-align:center;font-weight:700}}
</style>

<div id="overlay"><div style="display:flex;flex-direction:column;align-items:center">
  <div class="spinner"></div><p>Rendering... please wait</p>
</div></div>

<div class="wrap">
  <div class="grid panel">
    <div class="left">
      <h1>Edu<span class="a">Tap</span> Shorts</h1>
      <p class="lead">Generate vertical shorts with a branded caption box. Upload a video or paste a link. Choose <b>Full</b> or <b>Mid</b> layout. Clean, fast, and simple.</p>
    </div>

    <div class="right">
      <form id="renderForm" method="post" action="{{{{ url_for('render') }}}}" enctype="multipart/form-data">
        {{% with messages = get_flashed_messages() %}}
          {{% if messages %}}<div style="margin:10px 0 14px;color:#9ef">{{{{ messages[0] }}}}</div>{{% endif %}}
        {{% endwith %}}

        <div class="controls">
          <label><input type="radio" name="design" value="full" {{% if design=='full' %}}checked{{% endif %}}> Full</label>
          <label><input type="radio" name="design" value="mid"  {{% if design=='mid' %}}checked{{% endif %}}> Mid</label>
          <button type="button" class="btn subtle" onclick="addItem()">+ Add Video</button>
        </div>

        <input type="hidden" name="total_items" id="total_items" value="1">

        <div id="cards" class="cards">
          <!-- Card 0 -->
          <div class="card" data-idx="0">
            <button type="button" class="rm" onclick="removeItem(this)" title="Remove">×</button>
            <h4>Video 1</h4>

            <div class="mode">
              <label><input type="radio" name="mode_0" value="upload" checked onchange="toggleMode(this)"> Upload</label>
              <label><input type="radio" name="mode_0" value="link" onchange="toggleMode(this)"> Link</label>
            </div>

            <div class="up">
              <input type="file" name="file_0" accept="video/mp4,video/quicktime,video/*">
              <small>MP4 / MOV recommended</small>
            </div>
            <div class="lk hidden">
              <input type="text" name="link_0" placeholder="https://... (Google Drive / OneDrive / direct)">
            </div>

            <label>Caption text</label>
            <textarea name="text_0" placeholder="Write your lines here…"></textarea>
          </div>
        </div>

        <div class="row" style="margin-top:14px">
          <button class="btn" id="renderBtn" type="submit">Render</button>
        </div>
      </form>
    </div>
  </div>

  {{% if last_files %}}
    <div style="max-width:1200px;margin:16px auto 30px">
      {{% for name in last_files %}}
        <div style="margin:8px 0">✅ <a style="color:#9ef" href="{{{{ url_for('download', filename=name) }}}}">{{{{ name }}}}</a></div>
      {{% endfor %}}
      <div style="color:#8aa0b6;margin-top:10px">Files auto-delete after about {{ttl}} seconds.</div>
    </div>
  {{% endif %}}
</div>

<script>
  const overlay = document.getElementById('overlay');
  const btn = document.getElementById('renderBtn');
  const form = document.getElementById('renderForm');
  form.addEventListener('submit', () => { btn.disabled = true; overlay.style.display = 'flex'; });

  function toggleMode(radio){
    const card = radio.closest('.card');
    const isUpload = radio.value === 'upload';
    card.querySelector('.up').classList.toggle('hidden', !isUpload);
    card.querySelector('.lk').classList.toggle('hidden', isUpload);
  }

  function removeItem(el){
    const card = el.closest('.card');
    const cards = document.getElementById('cards');
    if(cards.children.length === 1){ alert('At least one video is required.'); return; }
    card.remove(); renumber();
  }

  function addItem(){
    const cards = document.getElementById('cards');
    const idx = cards.children.length;
    const base = cards.children[0].cloneNode(true);
    base.setAttribute('data-idx', idx);
    base.querySelector('h4').textContent = 'Video ' + (idx+1);

    // clear inputs & rename
    base.querySelectorAll('input,textarea').forEach(inp=>{
      if(inp.type==='radio'){
        inp.name = 'mode_'+idx;
        if(inp.value==='upload') inp.checked = true;
        else inp.checked = false;
      }else if(inp.type==='file'){
        inp.name = 'file_'+idx; inp.value='';
      }else if(inp.type==='text'){
        inp.name = 'link_'+idx; inp.value='';
      }else if(inp.tagName.toLowerCase()==='textarea'){
        inp.name = 'text_'+idx; inp.value='';
      }
    });
    base.querySelector('.up').classList.remove('hidden');
    base.querySelector('.lk').classList.add('hidden');

    cards.appendChild(base);
    renumber();
  }

  function renumber(){
    const cards = document.getElementById('cards');
    [...cards.children].forEach((card,i)=>{
      card.dataset.idx = i;
      card.querySelector('h4').textContent = 'Video ' + (i+1);
      card.querySelectorAll('input,textarea').forEach(inp=>{
        if(inp.type==='radio'){ inp.name = 'mode_'+i; }
        else if(inp.type==='file'){ inp.name = 'file_'+i; }
        else if(inp.type==='text'){ inp.name = 'link_'+i; }
        else if(inp.tagName.toLowerCase()==='textarea'){ inp.name = 'text_'+i; }
      });
    });
    document.getElementById('total_items').value = cards.children.length;
  }
</script>
"""

# -------------------- Flask routes --------------------
@app.get("/")
def index():
    return render_template_string(HTML, last_files=None, design="full", ttl=TTL_SECONDS)

def _save_upload(file_storage, tmp_root) -> str:
    if not file_storage or file_storage.filename == "":
        return ""
    fn = secure_filename(file_storage.filename)
    ext = Path(fn).suffix.lower()
    if ext not in [".mp4",".mov",".m4v",".qt"]:
        ext = ".mp4"
    out = Path(tmp_root) / ("input" + ext)
    file_storage.save(out)
    return str(out)

@app.post("/render")
def render():
    design = (request.form.get("design","full") or "full").lower()

    try:
        n = int(request.form.get("total_items","1"))
    except Exception:
        n = 1
    n = max(1, n)

    produced = []
    temp_batch = tempfile.mkdtemp(prefix="batch_")
    try:
        for i in range(n):
            mode = request.form.get(f"mode_{i}","upload")
            text = request.form.get(f"text_{i}","").strip()
            link = request.form.get(f"link_{i}","").strip()
            file_storage = request.files.get(f"file_{i}")

            if mode == "link":
                if not link:
                    continue
                local_video, tmpdir = download_video_to_temp(link)
                tmp_cleanup = tmpdir
            else:
                local_video = _save_upload(file_storage, temp_batch)
                if not local_video:
                    continue
                tmp_cleanup = ""

            # Caption -> PNG (Pillow)
            cap_png = OUTPUTS_DIR / f"caption_{os.urandom(6).hex()}.png"
            render_caption_png_pillow(
                text, cap_png,
                max_width=1000, font_size=52, line_height=1.35,
                pad_x=18, pad_y=10, radius=8
            )

            # Output filename (unique)
            base_name = safe_filename_from_text(text)
            candidate = OUTPUTS_DIR / base_name
            if candidate.exists():
                ts = time.strftime("%Y%m%d_%H%M%S")
                candidate = OUTPUTS_DIR / f"{Path(base_name).stem}_{ts}.mp4"

            # Compose
            if design == "mid":
                compose_mid(local_video, cap_png, candidate, LOGO_PATH)
            else:
                compose_full(local_video, cap_png, candidate, LOGO_PATH)

            produced.append(candidate.name)

            # cleanup
            if tmp_cleanup and os.path.isdir(tmp_cleanup):
                shutil.rmtree(tmp_cleanup, ignore_errors=True)
            if cap_png.exists():
                cap_png.unlink(missing_ok=True)

        if not produced:
            flash("Please add at least one valid item (upload a file or provide a link).")
            return render_template_string(HTML, last_files=None, design=design, ttl=TTL_SECONDS)

        flash("Render complete!")
        return render_template_string(HTML, last_files=produced, design=design, ttl=TTL_SECONDS)
    finally:
        shutil.rmtree(temp_batch, ignore_errors=True)

@app.get("/download/<path:filename>")
def download(filename):
    return send_from_directory(OUTPUTS_DIR, filename, as_attachment=True)

if __name__ == "__main__":
    # local dev
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
