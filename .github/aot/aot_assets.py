import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request

PROJ = os.environ.get("AOT_PROJ", "proj")
LOG = os.environ.get("AOT_LOG", "build.log")
CACHE = os.path.expanduser(os.environ.get("AOT_CACHE", "~/aot-cache"))
HERE = os.path.dirname(os.path.abspath(__file__))
BLEND_SCRIPT = os.path.join(HERE, "blender", "aot_blend.py")
BLENDER_HOST = "download.blender.org"
GLTF_CLI = "@gltf-transform/cli@4.1.0"
OUT_DIRS = {
    "babylon": ("src", "main", "assets", "game", "models"),
    "flame": ("assets", "sprites"),
    "godot": ("assets", "models"),
}
LIBS = ["libxi6", "libxxf86vm1", "libxfixes3", "libxrender1", "libgl1", "libxkbcommon0", "libsm6", "libegl1", "libxext6"]


class AssetFail(Exception):
    pass


def log(text):
    print(text, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def read_spec():
    path = os.path.join(PROJ, "project.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def jobs_file():
    return os.path.join(PROJ, "assets_src", "assets.json")


def sha_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def safe_rel(p):
    if not isinstance(p, str) or not p or p.startswith("/") or "\\" in p:
        return False
    return ".." not in p.split("/")


def blender_url(version):
    parts = version.split(".")
    major = parts[0] + "." + parts[1] if len(parts) > 1 else version
    return "https://" + BLENDER_HOST + "/release/Blender" + major + "/blender-" + version + "-linux-x64.tar.xz"


def cmd_version():
    spec = read_spec()
    b = (spec.get("game") or {}).get("blender") or {}
    if b.get("version") and os.path.exists(jobs_file()):
        print(b["version"])
    return 0


def install_libs():
    cmd = ["apt-get", "install", "-y", "-qq"] + LIBS
    if os.geteuid() != 0:
        cmd = ["sudo"] + cmd
    subprocess.call(["sudo", "apt-get", "update", "-qq"] if os.geteuid() != 0 else ["apt-get", "update", "-qq"])
    code = subprocess.call(cmd)
    if code != 0:
        log("warning: could not install blender system libraries (exit " + str(code) + ")")


def install_blender(spec):
    b = (spec.get("game") or {}).get("blender") or {}
    version = b.get("version", "")
    pin = (b.get("sha256") or "").lower()
    if not version:
        raise AssetFail("game.blender.version is required for assets_src jobs")
    root = os.path.join(CACHE, "blender-" + version)
    exe = os.path.join(root, "blender")
    if os.path.exists(exe):
        log("blender " + version + " from cache")
        return exe
    os.makedirs(CACHE, exist_ok=True)
    tar = os.path.join(CACHE, "blender-" + version + ".tar.xz")
    url = blender_url(version)
    log("downloading " + url)
    with urllib.request.urlopen(url, timeout=600) as r, open(tar, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    got = sha_file(tar)
    if pin and pin != got:
        os.remove(tar)
        raise AssetFail("blender sha256 mismatch: expected " + pin + ", got " + got)
    if not pin:
        if spec.get("play") is True:
            raise AssetFail("unpinned blender in a play build, sha256 " + got)
        log("BLENDER PIN " + version + " " + got)
    tmp = os.path.join(CACHE, "blender-extract")
    shutil.rmtree(tmp, ignore_errors=True)
    with tarfile.open(tar) as t:
        t.extractall(tmp)
    inner = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
    if not inner:
        raise AssetFail("blender archive was empty")
    shutil.rmtree(root, ignore_errors=True)
    shutil.move(os.path.join(tmp, inner[0]), root)
    os.remove(tar)
    install_libs()
    return exe


def job_key(job, root):
    h = hashlib.sha256()
    h.update(json.dumps(job, sort_keys=True).encode())
    h.update(sha_file(BLEND_SCRIPT).encode())
    for f in [job["in"]] + [a for a in step_files(job)]:
        p = os.path.join(root, f)
        if os.path.exists(p):
            h.update(sha_file(p).encode())
    return h.hexdigest()


def step_files(job):
    out = []
    for s in job.get("steps", []):
        if s.startswith("retarget(") and s.endswith(")"):
            out.append(s[len("retarget("):-1].strip().strip("\"'"))
    return out


def run_job(exe, job, root, engine, dest):
    for k in ("in", "out"):
        if not safe_rel(job.get(k, "")):
            raise AssetFail("job " + k + " is not a safe relative path: " + str(job.get(k)))
    if not os.path.exists(os.path.join(root, job["in"])):
        raise AssetFail("job input is missing: " + job["in"])
    key = job_key(job, root)
    store = os.path.join(CACHE, "results", key)
    if os.path.isdir(store):
        log("job " + job["out"] + " from cache " + key[:12])
        return restore(store, dest)
    work = os.path.join(CACHE, "work", key)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    spec = {
        "input": os.path.join(root, job["in"]),
        "out": job["out"],
        "root": root,
        "steps": job.get("steps", []),
        "glb": os.path.join(work, "out", job["out"]),
        "sprites": os.path.join(work, "sprites"),
        "previews": os.path.join(work, "previews"),
    }
    os.makedirs(spec["previews"], exist_ok=True)
    jf = os.path.join(work, "job.json")
    with open(jf, "w", encoding="utf-8") as f:
        json.dump(spec, f)
    code = subprocess.call([exe, "-b", "--python", BLEND_SCRIPT, "--", jf])
    if code != 0:
        raise AssetFail("blender failed on " + job["in"] + " (exit " + str(code) + ")")
    if engine == "babylon" and os.path.exists(spec["glb"]):
        optimize(spec["glb"])
    os.makedirs(store, exist_ok=True)
    for sub in ("out", "sprites", "previews"):
        p = os.path.join(work, sub)
        if os.path.isdir(p):
            shutil.copytree(p, os.path.join(store, sub), dirs_exist_ok=True)
    sheets(os.path.join(store, "sprites"))
    return restore(store, dest)


def restore(store, dest):
    n = 0
    for sub, target in (("out", dest), ("sprites", dest)):
        p = os.path.join(store, sub)
        if os.path.isdir(p):
            shutil.copytree(p, target, dirs_exist_ok=True)
            n += 1
    pv = os.path.join(store, "previews")
    if os.path.isdir(pv):
        for f in glob.glob(os.path.join(pv, "preview-turn-*.png")):
            shutil.copy2(f, os.path.basename(f))
    return n


def optimize(glb):
    if shutil.which("npx") is None:
        log("warning: npx missing, glb not optimized")
        return
    before = os.path.getsize(glb)
    steps = [["dedup"], ["prune"], ["resize", "--width", "1024", "--height", "1024"], ["webp"]]
    for s in steps:
        code = subprocess.call(["npx", "--yes", GLTF_CLI] + s + [glb, glb])
        if code != 0:
            log("warning: gltf-transform " + s[0] + " failed (exit " + str(code) + "), kept the previous file")
    log("optimize " + os.path.basename(glb) + " " + str(before) + " -> " + str(os.path.getsize(glb)) + " bytes")


def sheets(sprites_dir):
    if not os.path.isdir(sprites_dir):
        return
    try:
        subprocess.call([sys.executable, "-m", "pip", "install", "--quiet", "pillow"])
        from PIL import Image
    except Exception as e:
        log("warning: no pillow, sprite frames stay separate: " + str(e))
        return
    for d in sorted(os.listdir(sprites_dir)):
        p = os.path.join(sprites_dir, d)
        if not os.path.isdir(p):
            continue
        frames = sorted(glob.glob(os.path.join(p, "*.png")))
        if not frames:
            continue
        imgs = [Image.open(f).convert("RGBA") for f in frames]
        w, h = imgs[0].size
        sheet = Image.new("RGBA", (w * len(imgs), h))
        for i, im in enumerate(imgs):
            sheet.paste(im, (i * w, 0))
        sheet.save(os.path.join(sprites_dir, d + ".png"))
        with open(os.path.join(sprites_dir, d + ".json"), "w", encoding="utf-8") as f:
            json.dump({"frames": len(imgs), "frameWidth": w, "frameHeight": h}, f)
        shutil.rmtree(p)


def cmd_run():
    spec = read_spec()
    path = jobs_file()
    if not os.path.exists(path):
        print("no assets_src jobs")
        return 0
    try:
        engine = (spec.get("game") or {}).get("engine", "")
        if engine not in OUT_DIRS:
            raise AssetFail("assets_src jobs need game.engine babylon, flame or godot")
        with open(path, encoding="utf-8") as f:
            jobs = json.load(f).get("jobs", [])
        if not jobs:
            print("assets.json has no jobs")
            return 0
        dest = os.path.join(PROJ, *OUT_DIRS[engine])
        os.makedirs(dest, exist_ok=True)
        exe = install_blender(spec)
        root = os.path.join(PROJ, "assets_src")
        total = 0
        for job in jobs:
            total += run_job(exe, job, root, engine, dest)
        log("assets: " + str(len(jobs)) + " job(s) done, output in " + "/".join(OUT_DIRS[engine]))
    except AssetFail as e:
        log("FAILED: " + str(e))
        return 1
    except Exception as e:
        log("FAILED: " + type(e).__name__ + ": " + str(e))
        return 1
    return 0


COMMANDS = {"version": cmd_version, "run": cmd_run}


def main(argv):
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print("usage: aot_assets.py " + "|".join(sorted(COMMANDS)))
        return 2
    return COMMANDS[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
