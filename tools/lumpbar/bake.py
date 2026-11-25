"""Bake Lump's menu bar frames for the Mac app (tools/lumpbar).

The Mac app gets its own drawings of every mood: design/lump.html's menu bar style
(render(..., {mini}): heavier steadier line, face zoomed up, fussy prop detail dropped, effects
kept close), in a slot 1.5x wider than tall so flat poses and props don't shrink him, with a thin
keyline so he stands out on any wallpaper. Each size is drawn at its exact pixels, the outline
a set number of screen pixels wide, rather than shrinking one big frame.

Renders every mood loop in headless Chrome and writes tools/lumpbar/frames/:

  manifest.json   {"fps": 24, "sizes": {"wing": [w, h], "item": [w, h]},
                   "moods": {mood: {"frames": n, "ticks": [frame per tick]}}, "events": {name: (same)}}
  (and tools/lumpbar/icon.png, the 1024 px app icon: his full-size drawing)
  <name>-<size>-<ink>-<k|w>.png   one horizontal strip per mood or event, size (wing = beside the notch,
                                  item = a menu bar item), ink (glow = cream line for dark
                                  backgrounds, paper = dark line for light ones) and body colour:
                                  k = body drawn black, w = body drawn white.

The two strips let the app paint him any colour: every pixel is linear in the body colour,
so  pixel = k + (w - k) * colour / 255  (alpha is the same in both).

Usage:  python3 tools/lumpbar/bake.py
"""
import http.server, json, os, shutil, socket, subprocess, sys, tempfile, threading
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DESIGN = os.path.join(ROOT, "design")
OUT = os.path.join(ROOT, "tools", "lumpbar", "frames")
ICON = os.path.join(ROOT, "tools", "lumpbar", "icon.png")      # 1024 px app icon
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ASPECT = 1.5
# size: [frame height in px (Retina), body outline in px]. The wing sits beside the notch
# (30-36 pt tall depending on the MacBook); a menu bar item is 20 pt.
SIZES = {"wing": [64, 3.1], "item": [40, 2.0]}
KEYLINE = 1.2       # px of keyline round him at wing size (black on glow, white on paper)
PAD = 7             # room round his loop, in lump.html's 240 px units
# One-off animations the bridge can ask for (GET /status "event"), played once over his mood:
# name -> [lump.html clip, the mood whose framing it borrows]. The plane flies off to the edges, so it
# keeps the standing moods' framing and gets clipped there (like on the glass) rather than shrinking him.
EVENTS = {"tap_plane": ["tap_plane_awake", "awake"]}

BAKE_PAGE = r"""<!doctype html><meta charset="utf-8"><body>
<iframe id="src" src="lump.html" style="width:240px;height:240px;border:0"></iframe>
<script>
const SIZES = __SIZES__, ASPECT = __ASPECT__, KEYLINE = __KEYLINE__, PAD = __PAD__, EVENTS = __EVENTS__;
const INKS = ['glow', 'paper'], TINTS = { k: '#000000', w: '#FFFFFF' };
const off = document.createElement('canvas'); off.width = off.height = 240;
const octx = off.getContext('2d', { willReadFrequently: true });
// render() paints a background and a floor shadow first: skip both so he's on transparency.
// setTransform (render's reset to identity) maps onto our crop and scale instead.
function wrap(ctx, k = 1, ox = 0, oy = 0) {
  let fills = 0;
  return new Proxy(ctx, {
    get(t, p) {
      if (p === 'setTransform') return () => t.setTransform(k, 0, 0, k, ox, oy);
      if (p === 'fillRect') return (x, y, w, h) => { if (x === 0 && y === 0 && w === 240 && h === 240) return; t.fillRect(x, y, w, h); };
      if (p === 'fill') return (...a) => { if (fills++ === 0) return; t.fill(...a); };
      const v = t[p]; return typeof v === 'function' ? v.bind(t) : v;
    },
    set(t, p, v) { t[p] = v; return true; }
  });
}
const loopLen = (L, m) => L.LOOPS[m].reduce((s, [, h]) => s + h, 0);
function fit(L, m) {      // the box everything he does across the loop covers, effects included, at the slot's aspect
  let x0 = 240, y0 = 240, x1 = 0, y1 = 0;
  L.setLine('glow'); L.setTint('#FF7A45');
  for (let t = 0; t < loopLen(L, m); t++) {
    octx.setTransform(1, 0, 0, 1, 0, 0); octx.clearRect(0, 0, 240, 240);
    L.render(wrap(octx), m, L.at(m, t).id, t, { dim: false, mini: { line: 2 } });
    const d = octx.getImageData(0, 0, 240, 240).data;
    for (let y = 0; y < 240; y++) for (let x = 0; x < 240; x++) if (d[(y * 240 + x) * 4 + 3] > 40) { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
  }
  let w = x1 - x0 + PAD * 2, h = y1 - y0 + PAD * 2;
  if (w / h > ASPECT) h = w / ASPECT; else w = h * ASPECT;
  return [(x0 + x1) / 2 - w / 2, (y0 + y1) / 2 - h / 2, w, h];
}
// A keyline: his silhouette grown by r px, laid under him.
function keyline(cv, r, colour) {
  const tmp = document.createElement('canvas'), sil = document.createElement('canvas');
  for (const c of [tmp, sil]) { c.width = cv.width; c.height = cv.height; }
  tmp.getContext('2d').drawImage(cv, 0, 0);
  const sc = sil.getContext('2d'); sc.drawImage(cv, 0, 0); sc.globalCompositeOperation = 'source-in'; sc.fillStyle = colour; sc.fillRect(0, 0, sil.width, sil.height);
  const c = cv.getContext('2d'); c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, cv.width, cv.height);
  for (let i = 0; i < 16; i++) { const a = i / 16 * Math.PI * 2; c.drawImage(sil, Math.cos(a) * r, Math.sin(a) * r); }
  c.drawImage(tmp, 0, 0);
}
// a: which drawing shows this tick (L.at for a mood loop, L.atSeq for a clip); fm: the effects mood
function frame(L, fm, a, t, ink, tint, crop, size) {
  const [H, outline] = SIZES[size], cv = document.createElement('canvas');
  cv.height = H; cv.width = Math.round(H * ASPECT);
  const k = H / crop[3], line = outline / (4 * k);              // lump.html's body outline is 4 units wide
  L.setLine(ink); L.setTint(tint);
  L.render(wrap(cv.getContext('2d', { willReadFrequently: true }), k, -crop[0] * k, -crop[1] * k), fm, a.id, t, { dim: false, mini: { line } });
  keyline(cv, Math.max(1, KEYLINE * H / SIZES.wing[0]), ink === 'paper' ? '#FFFFFF' : '#000000');
  return cv.getContext('2d').getImageData(0, 0, cv.width, cv.height);
}
function hash(a) { let h = 2166136261; for (let i = 0; i < a.length; i++) h = Math.imul(h ^ a[i], 16777619); return h >>> 0; }
// App icon: his plain smiling drawing (N) on black, like the glass, drawn at full size rather than scaled up.
async function icon(L) {
  const N = 1024, cv = document.createElement('canvas'); cv.width = cv.height = N;
  const c = cv.getContext('2d');
  c.fillStyle = '#000'; c.beginPath(); c.roundRect(100, 100, 824, 824, 185); c.fill();
  L.setLine('glow'); L.setTint('#FF7A45');
  octx.setTransform(1, 0, 0, 1, 0, 0); octx.clearRect(0, 0, 240, 240);
  L.render(wrap(octx), L.fxFor('N'), 'N', 0, { still: true, noFX: true });
  const d = octx.getImageData(0, 0, 240, 240).data; let y0 = 240, y1 = 0, x0 = 240, x1 = 0;
  for (let y = 0; y < 240; y++) for (let x = 0; x < 240; x++) if (d[(y * 240 + x) * 4 + 3] > 40) { x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); }
  const k = 560 / (y1 - y0), ox = 512 - (x0 + x1) / 2 * k, oy = 540 - (y0 + y1) / 2 * k;
  c.save(); c.beginPath(); c.roundRect(100, 100, 824, 824, 185); c.clip();
  L.render(wrap(c, k, ox, oy), L.fxFor('N'), 'N', 0, { still: true, noFX: true });
  c.restore();
  return new Promise(r => cv.toBlob(r, 'image/png'));
}
async function post(path, body) { await fetch(path, { method: 'POST', body }); }
// Bake one sequence (a mood loop or a clip): every tick at every size, ink and body colour,
// identical frames shared, one strip per size/ink/colour.
async function bakeSeq(L, name, len, at, fxm, crop) {
  const uniq = new Map(), layers = [], ticks = [];
  for (let t = 0; t < len; t++) {
    const a = at(t), fm = fxm(a), imgs = {};
    for (const size of Object.keys(SIZES)) for (const ink of INKS) for (const [k, tint] of Object.entries(TINTS))
      imgs[`${size}-${ink}-${k}`] = frame(L, fm, a, t, ink, tint, crop, size);
    const key = Object.values(imgs).map(i => hash(i.data)).join('.');
    if (!uniq.has(key)) { uniq.set(key, layers.length); layers.push(imgs); }
    ticks.push(uniq.get(key));
  }
  for (const n of Object.keys(layers[0])) {
    const { width: fw, height: fh } = layers[0][n];
    const strip = document.createElement('canvas'); strip.width = fw * layers.length; strip.height = fh;
    const c = strip.getContext('2d'); layers.forEach((imgs, i) => c.putImageData(imgs[n], i * fw, 0));
    await post(`/png?name=${name}-${n}`, await new Promise(r => strip.toBlob(r, 'image/png')));
  }
  return { frames: layers.length, ticks };
}
async function bake() {
  const L = document.getElementById('src').contentWindow.__lump;
  const manifest = { fps: 24, sizes: {}, moods: {}, events: {} }, crops = {};
  for (const [size, [H]] of Object.entries(SIZES)) manifest.sizes[size] = [Math.round(H * ASPECT), H];
  for (const m of L.MOMENTS) {
    crops[m] = fit(L, m);
    manifest.moods[m] = await bakeSeq(L, m, loopLen(L, m), t => L.at(m, t), () => m, crops[m]);
  }
  for (const [name, [clip, framing]] of Object.entries(EVENTS))
    manifest.events[name] = await bakeSeq(L, name, L.seqLen(clip), t => L.atSeq(clip, t), a => L.fxFor(a.id), crops[framing]);
  await post('/png?name=icon', await icon(L));
  await post('/done', JSON.stringify(manifest));
}
document.getElementById('src').addEventListener('load', () => bake().catch(e => post('/fail', String(e.stack || e))));
</script>"""

done = threading.Event()
result = {}


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DESIGN, **k)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/__lumpbar_bake.html":
            body = (BAKE_PAGE.replace("__SIZES__", json.dumps(SIZES)).replace("__ASPECT__", str(ASPECT))
                    .replace("__KEYLINE__", str(KEYLINE)).replace("__PAD__", str(PAD))
                    .replace("__EVENTS__", json.dumps(EVENTS))).encode()
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)
        else:
            super().do_GET()

    def do_POST(self):
        u = urlparse(self.path); body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if u.path == "/png":
            name = parse_qs(u.query)["name"][0]
            with open(ICON if name == "icon" else os.path.join(OUT, name + ".png"), "wb") as f:
                f.write(body)
        elif u.path == "/done":
            with open(os.path.join(OUT, "manifest.json"), "w") as f:
                json.dump(json.loads(body), f, separators=(",", ":"))
            result["ok"] = json.loads(body); done.set()
        elif u.path == "/fail":
            result["error"] = body.decode(); done.set()
        self.send_response(200); self.end_headers()


def main():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix="lumpbar-chrome-")
    chrome = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                               f"--user-data-dir={profile}", f"http://127.0.0.1:{port}/__lumpbar_bake.html"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not done.wait(300):
            sys.exit("timed out waiting for the bake page")
    finally:
        chrome.terminate(); chrome.wait(10); srv.shutdown(); shutil.rmtree(profile, ignore_errors=True)
    if "error" in result:
        sys.exit("bake failed:\n" + result["error"])
    moods = result["ok"]["moods"]
    events = result["ok"].get("events") or {}
    print(f"baked {sum(m['frames'] for m in moods.values())} frames for {len(moods)} moods"
          f" and {sum(m['frames'] for m in events.values())} for {len(events)} event(s) into {OUT}")


if __name__ == "__main__":
    main()
