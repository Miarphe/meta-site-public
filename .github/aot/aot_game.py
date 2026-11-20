import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
import zipfile
import struct
import glob

PROJ = os.environ.get("AOT_PROJ", "proj")
LOG = os.environ.get("AOT_LOG", "build.log")
HOSTS = {"cdn.jsdelivr.net", "registry.npmjs.org", "github.com", "objects.githubusercontent.com"}
MAX_BYTES = 300 * 1024 * 1024


class GameFail(Exception):
    pass


def log(text):
    print(text, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def read_spec():
    path = os.path.join(PROJ, "project.json")
    if not os.path.exists(path):
        raise GameFail("project.json is missing from the source")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def host_of(url):
    return (urllib.parse.urlparse(url).hostname or "").lower()


def initial_ok(url):
    return url.startswith("https://") and host_of(url) in HOSTS


def final_ok(url):
    h = host_of(url)
    return url.startswith("https://") and (h in HOSTS or h.endswith(".githubusercontent.com"))


def safe_path(rel):
    n = os.path.normpath(rel).replace("\\", "/")
    return bool(rel) and not (n.startswith("..") or os.path.isabs(n) or n.startswith(".github"))


def download(url):
    with urllib.request.urlopen(url, timeout=180) as r:
        if not final_ok(r.geturl()):
            raise GameFail("redirected to a host that is not allowed: " + host_of(r.geturl()))
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise GameFail("file is larger than " + str(MAX_BYTES // (1024 * 1024)) + " MB: " + url)
    return data


def vendor(spec, fetch=download):
    game = spec.get("game") or {}
    items = game.get("vendor") or []
    play = spec.get("play") is True
    done = []
    for v in items:
        path = v.get("path", "")
        url = v.get("url", "")
        pin = (v.get("sha256") or "").lower()
        if not safe_path(path):
            raise GameFail("vendor path is not allowed: " + path)
        if not initial_ok(url):
            raise GameFail("vendor url is not allowed: " + url)
        data = fetch(url)
        got = hashlib.sha256(data).hexdigest()
        if pin and pin != got:
            raise GameFail("sha256 mismatch for " + path + ": expected " + pin + ", got " + got)
        if not pin:
            if play:
                raise GameFail("unpinned vendor file in a play build: " + path + " sha256 " + got)
            log("VENDOR PIN " + path + " " + got)
        dest = os.path.join(PROJ, path)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        log("vendor " + path + " " + str(len(data)) + " bytes " + got[:12])
        done.append({"path": path, "sha256": got, "bytes": len(data)})
    return done


def write_flags(spec):
    game = spec.get("game") or {}
    d = os.path.join(PROJ, "src", "main", "assets", "game")
    if not game or not os.path.isdir(d):
        return
    with open(os.path.join(d, "aot_flags.js"), "w", encoding="utf-8") as f:
        f.write("window.AOT_FLAGS = { debugOverlay: " + ("true" if game.get("debugOverlay") is True else "false") + " };\n")


def cmd_vendor():
    spec = read_spec()
    write_flags(spec)
    if not (spec.get("game") or {}).get("vendor"):
        print("no vendor files")
        return 0
    try:
        vendor(spec)
    except GameFail as e:
        log("FAILED: " + str(e))
        return 1
    except Exception as e:
        log("FAILED: " + type(e).__name__ + ": " + str(e))
        return 1
    return 0


SMOKE_SHOTS = (1.0, 3.0, 6.0)
INIT_JS = """
window.__aot = {frames: [], errors: []};
(function () {
  var last = 0;
  function tick(t) {
    if (last) window.__aot.frames.push(t - last);
    last = t;
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
})();
"""


def pct(vals, q):
    if not vals:
        return 0.0
    v = sorted(vals)
    return round(v[min(len(v) - 1, int(len(v) * q))], 2)


def cmd_smoke():
    root = os.path.join(PROJ, "src", "main", "assets", "game")
    out = {"note": "runner, not phone: software rendering on a CI machine", "ok": False}
    if not os.path.exists(os.path.join(root, "index.html")):
        print("no web game to smoke test")
        return 0
    try:
        import functools
        import http.server
        import subprocess
        import threading
        import time
        subprocess.call([sys.executable, "-m", "pip", "install", "--quiet", "playwright"])
        subprocess.call([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"])
        from playwright.sync_api import sync_playwright

        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=root)
        handler.log_message = lambda *a, **k: None
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        console, page_errors, remote, failed = [], [], [], []
        with sync_playwright() as pw:
            br = pw.chromium.launch(args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"])
            ctx = br.new_context(viewport={"width": 412, "height": 915}, device_scale_factor=1, has_touch=True)
            page = ctx.new_page()
            page.add_init_script(INIT_JS)
            page.on("console", lambda m: console.append({"type": m.type, "text": m.text[:300]}) if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: page_errors.append(str(e)[:300]))
            page.on("requestfailed", lambda r: failed.append(r.url[:200]))
            page.on("request", lambda r: remote.append(r.url[:200]) if not r.url.startswith("http://127.0.0.1:" + str(port)) and not r.url.startswith("data:") and not r.url.startswith("blob:") else None)
            page.goto("http://127.0.0.1:%d/index.html" % port, wait_until="load")
            t0 = time.time()
            shots = []
            for i, at in enumerate(SMOKE_SHOTS):
                wait = at - (time.time() - t0)
                if wait > 0:
                    time.sleep(wait)
                if i == 0:
                    page.mouse.click(206, 457)
                name = "preview-%02d.png" % (i + 1)
                page.screenshot(path=name)
                shots.append(name)
            data = page.evaluate("window.__aot.frames")
            br.close()
        srv.shutdown()
        out.update({
            "ok": not page_errors,
            "screenshots": shots,
            "consoleErrors": console[:20],
            "pageErrors": page_errors[:20],
            "failedRequests": failed[:20],
            "remoteRequests": remote[:20],
            "frames": {"count": len(data), "p50": pct(data, 0.5), "p95": pct(data, 0.95), "p99": pct(data, 0.99), "longOver50ms": len([x for x in data if x > 50])},
        })
        log("smoke: " + ("ok" if out["ok"] else "page errors") + ", " + str(len(shots)) + " screenshots, frame p50 " + str(out["frames"]["p50"]) + " ms (runner, not phone)")
    except Exception as e:
        out["error"] = type(e).__name__ + ": " + str(e)[:300]
        log("smoke skipped: " + out["error"])
    with open("game-smoke.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    return 0


PAGE = 16384
STATUS_FILES = ("flutter-build-status.json", "godot-build-status.json")


def elf_aligned(data):
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return True
    phoff = struct.unpack_from("<Q", data, 32)[0]
    phsize = struct.unpack_from("<H", data, 54)[0]
    phnum = struct.unpack_from("<H", data, 56)[0]
    for i in range(phnum):
        off = phoff + i * phsize
        if off + 56 > len(data):
            return True
        if struct.unpack_from("<I", data, off)[0] == 1 and struct.unpack_from("<Q", data, off + 48)[0] < PAGE:
            return False
    return True


def check_apk(path):
    checked = 0
    bad = []
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            parts = info.filename.split("/")
            if len(parts) != 3 or parts[0] != "lib" or parts[1] not in ("arm64-v8a", "x86_64") or not parts[2].endswith(".so"):
                continue
            checked += 1
            if not elf_aligned(z.read(info)):
                bad.append(info.filename)
    return checked, bad


def apk_files():
    out = []
    for variant in ("debug", "release"):
        out.extend(sorted(glob.glob(os.path.join(PROJ, "build", "outputs", "apk", variant, "*.apk"))))
    return out


def cmd_apkcheck():
    try:
        spec = read_spec()
    except Exception:
        spec = {}
    play = bool(spec.get("play"))
    checked = 0
    bad = []
    files = apk_files()
    for apk in files:
        try:
            n, b = check_apk(apk)
        except Exception as e:
            log("APK16K could not read " + apk + ": " + type(e).__name__)
            continue
        checked += n
        rel = os.path.basename(os.path.dirname(apk)) + "/" + os.path.basename(apk)
        for x in b:
            bad.append(rel + ":" + x)
        log("APK16K " + rel + ": " + str(n) + " native librar" + ("y" if n == 1 else "ies") + ", " + str(len(b)) + " not 16 KB aligned")
    result = {"checked": checked, "bad": bad, "apks": len(files), "play": play}
    for name in STATUS_FILES:
        if os.path.exists(name):
            try:
                with open(name, encoding="utf-8") as f:
                    d = json.load(f)
                d["apk16k"] = result
                if bad and play:
                    d["ok"] = False
                    d["note"] = "native libraries are not 16 KB aligned"
                with open(name, "w", encoding="utf-8") as f:
                    json.dump(d, f, indent=2)
            except Exception as e:
                log("APK16K status not updated: " + type(e).__name__)
    if bad and play:
        log("FAILED: " + str(len(bad)) + " native librar" + ("y is" if len(bad) == 1 else "ies are") + " not 16 KB aligned and play is true")
        return 1
    if bad:
        log("WARN " + str(len(bad)) + " native library(ies) not 16 KB aligned. Set play true to make this fail the build")
    return 0


COMMANDS = {"vendor": cmd_vendor, "smoke": cmd_smoke, "apkcheck": cmd_apkcheck}


def main(argv):
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print("usage: aot_game.py " + "|".join(sorted(COMMANDS)))
        return 2
    return COMMANDS[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
