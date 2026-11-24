import glob
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RELEASES = "https://storage.googleapis.com/flutter_infra_release/releases/releases_linux.json"
FALLBACK = "3.35.7"
LIBS = os.path.join("ref_in", "lib", "arm64-v8a")


def info():
    try:
        with open("info.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def out(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(name + "=" + value + "\n")


def pick_version(releases, dart):
    best = ""
    for r in releases:
        if r.get("channel") != "stable":
            continue
        d = str(r.get("dart_sdk_version", "")).split()
        if d and d[0] == dart:
            best = r.get("version", "")
            break
    return best


def cmd_pick():
    dart = info().get("dart", "")
    version = ""
    try:
        with urllib.request.urlopen(RELEASES, timeout=60) as r:
            data = json.load(r)
        version = pick_version(data.get("releases", []), dart)
    except Exception as e:
        print("release list failed: " + str(e))
    print("dart " + dart + " flutter " + (version or "not found, using " + FALLBACK))
    out("flutter", version or FALLBACK)
    out("found", "true" if version else "false")


def cmd_build():
    version = info().get("dart", "")
    shutil.rmtree("ref", ignore_errors=True)
    shutil.rmtree("ref_in", ignore_errors=True)
    shutil.rmtree("ref_out", ignore_errors=True)
    subprocess.run(["flutter", "create", "--platforms=android", "--project-name", "aot_ref", "ref"], check=True, timeout=900)
    subprocess.run(["flutter", "build", "apk", "--release", "--target-platform", "android-arm64"], cwd="ref", check=True, timeout=3000)
    apks = glob.glob(os.path.join("ref", "build", "app", "outputs", "flutter-apk", "app-release.apk"))
    if not apks:
        raise SystemExit("the reference build made no apk")
    os.makedirs(LIBS, exist_ok=True)
    with zipfile.ZipFile(apks[0]) as z:
        for n in ("libapp.so", "libflutter.so"):
            with z.open("lib/arm64-v8a/" + n) as src, open(os.path.join(LIBS, n), "wb") as dst:
                shutil.copyfileobj(src, dst)
    subprocess.run(["python3", "blutter/blutter.py", LIBS, "ref_out"], check=True, timeout=7200)
    import aot_dart_names as names
    import aot_flutter as F
    root = os.path.join("ref_out", "asm")
    libs, files = F.walk_asm(root)
    classes, table = F.scan_deep(files, root, {})
    with open("ref_shapes.json", "w", encoding="utf-8") as f:
        json.dump({"dart": version, "shapes": names.shapes(classes)}, f)
    print("reference classes " + str(len(classes)))


def cmd_older():
    shutil.rmtree("older_out", ignore_errors=True)
    libs = os.path.join("older_in", "lib", "arm64-v8a")
    if not (os.path.exists(os.path.join(libs, "libapp.so")) and os.path.exists(os.path.join(libs, "libflutter.so"))):
        print("no older build was given")
        return
    subprocess.run(["python3", "blutter/blutter.py", libs, "older_out"], check=True, timeout=7200)
    import aot_dart_names as names
    import aot_flutter as F
    root = os.path.join("older_out", "asm")
    found, files = F.walk_asm(root)
    classes, table = F.scan_deep(files, root, {})
    with open("older_shapes.json", "w", encoding="utf-8") as f:
        json.dump({"shapes": names.shapes(classes)}, f)
    print("older classes " + str(len(classes)))


if __name__ == "__main__":
    c = sys.argv[1]
    if c == "pick":
        cmd_pick()
    elif c == "build":
        cmd_build()
    elif c == "older":
        cmd_older()
    else:
        raise SystemExit("unknown command " + c)
