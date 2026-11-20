import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
import urllib.request
import zipfile

PROJ = os.environ.get("AOT_PROJ", "proj")
LOG = os.environ.get("AOT_LOG", "build.log")
STATUS = os.path.abspath("godot-build-status.json")
CACHE = os.path.expanduser(os.environ.get("AOT_GODOT_CACHE", "~/aot-godot"))
RELEASES = "https://github.com/godotengine/godot-builds/releases/download/"
ARCH_KEYS = {"armeabi-v7a": "armeabi-v7a", "arm64-v8a": "arm64-v8a", "x86_64": "x86_64"}
ALL_ARCHS = ["armeabi-v7a", "arm64-v8a", "x86", "x86_64"]


class BuildFail(Exception):
    pass


def log(text):
    print(text, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def read_spec():
    with open(os.path.join(PROJ, "project.json"), encoding="utf-8") as f:
        return json.load(f)


def godot_block(spec):
    return (spec.get("game") or {}).get("godot") or {}


def status(ok, note, extra=None):
    out = {"ok": ok, "note": note}
    if extra:
        out.update(extra)
    with open(STATUS, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)


def cmd_version():
    try:
        print(godot_block(read_spec()).get("version", ""))
    except Exception:
        print("")
    return 0


def run_logged(args, cwd=None, env=None, timeout=3000):
    log("$ " + " ".join(args))
    p = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    try:
        for line in p.stdout:
            line = line.rstrip("\n")
            print(line, flush=True)
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        raise BuildFail("timed out: " + args[0])


def download(url, dest):
    log("download " + url)
    with urllib.request.urlopen(url, timeout=600) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)


def file_hash(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def read_sums(path):
    out = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.strip().split()
            if len(parts) >= 2:
                out[parts[-1].lstrip("*")] = parts[0].lower()
    return out


def fetch_godot(spec):
    g = godot_block(spec)
    ver = g.get("version", "")
    if not re.match(r"^\d+\.\d+(\.\d+)?$", ver):
        raise BuildFail("game.godot.version must look like 4.4.1")
    pin = (g.get("sha256") or "").lower()
    root = os.path.join(CACHE, ver)
    editor_zip = "Godot_v" + ver + "-stable_linux.x86_64.zip"
    tpz = "Godot_v" + ver + "-stable_export_templates.tpz"
    exe = os.path.join(root, "Godot_v" + ver + "-stable_linux.x86_64")
    tdir = os.path.join(root, "templates")
    if os.path.exists(exe) and os.path.isdir(tdir):
        log("godot " + ver + " from cache")
        return exe, tdir
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)
    base = RELEASES + ver + "-stable/"
    sums_path = os.path.join(root, "SHA512-SUMS.txt")
    download(base + "SHA512-SUMS.txt", sums_path)
    sums = read_sums(sums_path)
    for name in (editor_zip, tpz):
        dest = os.path.join(root, name)
        download(base + name, dest)
        want = sums.get(name)
        if not want:
            raise BuildFail(name + " is not listed in the release SHA512-SUMS.txt")
        got = file_hash(dest, "sha512")
        if got != want:
            raise BuildFail("sha512 mismatch for " + name)
        log("verified " + name + " sha512")
    got256 = file_hash(os.path.join(root, editor_zip), "sha256")
    if pin and pin != got256:
        raise BuildFail("game.godot.sha256 mismatch for " + editor_zip + ": expected " + pin + ", got " + got256)
    if not pin:
        if spec.get("play") is True:
            raise BuildFail("unpinned Godot in a play build, sha256 " + got256)
        log("GODOT PIN " + ver + " " + got256)
    with zipfile.ZipFile(os.path.join(root, editor_zip)) as z:
        z.extractall(root)
    os.chmod(exe, 0o755)
    with zipfile.ZipFile(os.path.join(root, tpz)) as z:
        z.extractall(tdir)
    os.remove(os.path.join(root, editor_zip))
    os.remove(os.path.join(root, tpz))
    return exe, tdir


def templates_root(tdir):
    inner = os.path.join(tdir, "templates")
    return inner if os.path.isdir(inner) else tdir


def install_templates(tdir):
    src = templates_root(tdir)
    vfile = os.path.join(src, "version.txt")
    if not os.path.exists(vfile):
        raise BuildFail("export templates have no version.txt")
    verstr = open(vfile, encoding="utf-8").read().strip()
    dest = os.path.expanduser(os.path.join("~/.local/share/godot/export_templates", verstr))
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest)
    log("export templates installed as " + verstr)
    return verstr, src


def set_ini(path, section, key, value):
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    lines = text.splitlines()
    head = "[" + section + "]"
    out = []
    in_sec = False
    done = False
    seen = False
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if in_sec and not done:
                out.append(key + "=" + value)
                done = True
            in_sec = s == head
            seen = seen or in_sec
        elif in_sec and s.startswith(key + "="):
            line = key + "=" + value
            done = True
        out.append(line)
    if not seen:
        out += ["", head, "", key + "=" + value]
    elif in_sec and not done:
        out.append(key + "=" + value)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def prepare_project(spec):
    pg = os.path.join(PROJ, "project.godot")
    if not os.path.exists(pg):
        raise BuildFail("project.godot is missing from the source")
    g = godot_block(spec)
    renderer = g.get("renderer", "mobile")
    set_ini(pg, "rendering", "renderer/rendering_method", '"' + renderer + '"')
    set_ini(pg, "rendering", "textures/vram_compression/import_etc2_astc", "true")
    for stale in ("export_presets.cfg",):
        p = os.path.join(PROJ, stale)
        if os.path.exists(p):
            os.remove(p)
            log("removed " + stale + " from the source; it is generated")


def install_build_template(src, verstr):
    zpath = os.path.join(src, "android_source.zip")
    if not os.path.exists(zpath):
        raise BuildFail("android_source.zip is missing from the export templates")
    build = os.path.join(PROJ, "android", "build")
    os.makedirs(build, exist_ok=True)
    kept = 0
    added = 0
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            name = info.filename
            if ".." in name.split("/") or name.startswith("/"):
                raise BuildFail("unsafe path in android_source.zip: " + name)
            dest = os.path.join(build, name)
            if info.is_dir():
                os.makedirs(dest, exist_ok=True)
                continue
            if os.path.exists(dest):
                kept += 1
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(info) as s, open(dest, "wb") as f:
                shutil.copyfileobj(s, f)
            if name.endswith("gradlew"):
                os.chmod(dest, 0o755)
            added += 1
    with open(os.path.join(PROJ, "android", ".build_version"), "w", encoding="utf-8") as f:
        f.write(verstr)
    open(os.path.join(PROJ, "android", ".gdignore"), "a").close()
    log("android build template: " + str(added) + " files added, " + str(kept) + " kept from the project")


def dump_template():
    build = os.path.join(PROJ, "android", "build")
    log("---- android/build tree")
    n = 0
    for root, dirs, files in os.walk(build):
        dirs.sort()
        for f in sorted(files):
            n += 1
            if n <= 300:
                log(os.path.relpath(os.path.join(root, f), build))
    log("---- " + str(n) + " files; gradle files follow")
    for p in sorted(glob.glob(os.path.join(build, "**", "*.gradle*"), recursive=True))[:8]:
        log("---- " + os.path.relpath(p, build))
        with open(p, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= 120:
                    log("...")
                    break
                log(line.rstrip("\n"))
    log("---- end of template dump")


COMPOSE_BOM = "2024.11.00"
HOST_DEPS = [
    ("platform", "androidx.compose:compose-bom:" + COMPOSE_BOM),
    ("", "androidx.compose.ui:ui"),
    ("", "androidx.compose.ui:ui-graphics"),
    ("", "androidx.compose.foundation:foundation"),
    ("", "androidx.compose.material3:material3"),
    ("", "androidx.compose.material:material-icons-core"),
    ("", "androidx.activity:activity-compose:1.9.3"),
    ("", "androidx.core:core-ktx:1.13.1"),
    ("", "androidx.lifecycle:lifecycle-runtime-ktx:2.8.7"),
    ("", "org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.2"),
]
MAVEN_COORD = re.compile(r"[A-Za-z0-9_.\-]+:[A-Za-z0-9_.\-]+(:[A-Za-z0-9_.\-+]+)?")
MARK = "aot-host"
APP_ATTRS = ("allowBackup", "dataExtractionRules", "fullBackupContent", "networkSecurityConfig", "usesCleartextTraffic")


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def host_info():
    p = os.path.join(PROJ, "android", "aot_host.json")
    if os.path.exists(p):
        try:
            return json.loads(read_text(p))
        except Exception:
            return {}
    return {}


def find_gradle(build):
    for name in ("build.gradle", "build.gradle.kts"):
        p = os.path.join(build, name)
        if os.path.exists(p):
            return p
    hits = []
    for root, dirs, files in os.walk(build):
        for f in files:
            if f in ("build.gradle", "build.gradle.kts"):
                hits.append(os.path.join(root, f))
    hits.sort(key=lambda x: (x.count(os.sep), x))
    return hits[0] if hits else None


def find_manifest(build):
    for rel in ("AndroidManifest.xml", os.path.join("src", "main", "AndroidManifest.xml")):
        p = os.path.join(build, rel)
        if os.path.exists(p):
            return p
    hits = []
    for root, dirs, files in os.walk(build):
        for f in files:
            if f == "AndroidManifest.xml":
                hits.append(os.path.join(root, f))
    hits.sort(key=lambda x: (x.count(os.sep), x))
    return hits[0] if hits else None


def kotlin_version(build, gradle_text):
    cfg = os.path.join(build, "config.gradle")
    texts = [read_text(cfg)] if os.path.exists(cfg) else []
    texts.append(gradle_text)
    for tx in texts:
        for pat in (r"kotlinVersion\s*[:=]\s*['\"]([0-9][0-9.]*)['\"]", r"kotlin-gradle-plugin:([0-9][0-9.]*)", r"org\.jetbrains\.kotlin\.android['\"]\)?\s*version\s*['\"]([0-9][0-9.]*)['\"]"):
            m = re.search(pat, tx)
            if m:
                return m.group(1)
    return ""


def merge_manifest(path, stub):
    t = read_text(path)
    if MARK in t:
        log("manifest already merged")
        return
    s = read_text(stub)
    am = re.search(r"<application\b([^>]*?)(/>|>(.*)</application>)", s, re.S)
    attrs = dict(re.findall(r'(android:\w+)="([^"]*)"', am.group(1))) if am else {}
    inner = (am.group(3) or "") if am else ""
    outside = s[:am.start()] + s[am.end():] if am else s
    perms = re.findall(r"<uses-(?:permission|feature)\b[^>]*/>", outside)
    app = re.search(r"<application\b[^>]*>", t)
    if not app:
        raise BuildFail("the template manifest has no <application> element")
    tag = app.group(0)
    for k, v in attrs.items():
        short = k.split(":", 1)[1]
        if short not in APP_ATTRS:
            continue
        if re.search(r"\b" + re.escape(k) + r'="[^"]*"', tag):
            tag = re.sub(r"\b" + re.escape(k) + r'="[^"]*"', k + '="' + v + '"', tag, count=1)
        else:
            tag = tag[:-1].rstrip("/").rstrip() + " " + k + '="' + v + '"' + ("/>" if tag.endswith("/>") else ">")
    t = t[:app.start()] + tag + t[app.end():]
    names = set(re.findall(r'<uses-(?:permission|feature)\b[^>]*android:name="([^"]+)"', t))
    add = []
    for p in perms:
        m = re.search(r'android:name="([^"]+)"', p)
        if m and m.group(1) not in names:
            names.add(m.group(1))
            add.append("    " + p.strip())
    if add:
        a = re.search(r"<application\b", t)
        t = t[:a.start()] + "\n".join(add).lstrip() + "\n    " + t[a.start():]
    body = inner.strip()
    block = "\n        <!-- " + MARK + " -->\n        " + body + "\n    " if body else ""
    if block:
        close = t.rfind("</application>")
        if close >= 0:
            t = t[:close] + block + t[close:]
        else:
            m = re.search(r"<application\b[^>]*?/>", t)
            if not m:
                raise BuildFail("could not place the host elements in the template manifest")
            t = t[:m.end() - 2] + ">" + block + "</application>" + t[m.end():]
    else:
        t = t.replace("<manifest", "<!-- " + MARK + " -->\n<manifest", 1) if "<?xml" not in t else t
    if "tools:" in t and "xmlns:tools" not in t:
        t = re.sub(r"<manifest\b", '<manifest xmlns:tools="http://schemas.android.com/tools"', t, count=1)
    write_text(path, t)
    log("manifest merged: " + str(len(add)) + " permission line(s), " + str(len(body)) + " bytes of application elements")


def groovy_dep(kind, coord):
    q = "'" + coord + "'"
    return "    implementation platform(" + q + ")" if kind == "platform" else "    implementation " + q


def kts_dep(kind, coord):
    q = '"' + coord + '"'
    return "    implementation(platform(" + q + "))" if kind == "platform" else "    implementation(" + q + ")"


def patch_gradle(build, gradle, spec, info):
    text = read_text(gradle)
    if MARK in text:
        log("gradle already patched")
        return
    kts = gradle.endswith(".kts")
    compose = bool(info.get("compose")) or os.path.isdir(os.path.join(build, "src", "main", "kotlin", "aot", "ads")) or os.path.isdir(os.path.join(build, "src", "main", "kotlin", "aot", "pay"))
    if "kotlin" not in text.lower():
        raise BuildFail("the template Gradle file has no Kotlin plugin; the AotNative plugin is Kotlin. See the template dump above")
    if compose:
        kv = kotlin_version(build, text)
        if not kv:
            raise BuildFail("could not read the Kotlin version from the template, needed for the Compose compiler plugin")
        major = int(kv.split(".")[0])
        if major < 2:
            raise BuildFail("the template uses Kotlin " + kv + ", older than 2.0, so the Compose compiler plugin cannot be added")
        plug = "org.jetbrains.kotlin.plugin.compose"
        if re.search(r"^\s*plugins\s*\{", text, re.M):
            m = re.search(r"^[ \t]*(?:id\s*\(?\s*['\"]org\.jetbrains\.kotlin\.android['\"]\s*\)?.*)$", text, re.M)
            line = ('    id("' + plug + '") version "' + kv + '"') if kts else ("    id '" + plug + "' version '" + kv + "'")
            if m:
                text = text[:m.end()] + "\n" + line + text[m.end():]
            else:
                m = re.search(r"^\s*plugins\s*\{[ \t]*$", text, re.M)
                text = text[:m.end()] + "\n" + line + text[m.end():]
        else:
            cp = "org.jetbrains.kotlin:compose-compiler-gradle-plugin:" + kv
            bm = re.search(r"buildscript\s*\{.*?dependencies\s*\{[ \t]*\n", text, re.S)
            if not bm:
                raise BuildFail("no buildscript dependencies block to add the Compose compiler plugin to")
            line = ('        classpath("' + cp + '")\n') if kts else ("        classpath '" + cp + "'\n")
            text = text[:bm.end()] + line + text[bm.end():]
            ap = re.search(r"^[ \t]*apply\s+plugin:\s*['\"]org\.jetbrains\.kotlin\.android['\"].*$", text, re.M)
            if not ap:
                ap = re.search(r"^[ \t]*apply\s+plugin:\s*['\"]kotlin-android['\"].*$", text, re.M)
            if not ap:
                raise BuildFail("could not find where the Kotlin plugin is applied, to add the Compose compiler plugin after it")
            text = text[:ap.end()] + "\napply plugin: '" + plug + "'" + text[ap.end():]
        log("compose compiler plugin " + kv + " added")
    tail = "\n// " + MARK + "\n"
    if kts:
        tail += "android {\n    sourceSets.getByName(\"main\") {\n        java.srcDir(\"src/main/kotlin\")\n        assets.srcDir(\"src/main/assets\")\n        res.srcDir(\"src/main/res\")\n    }\n"
        if compose:
            tail += "    buildFeatures {\n        compose = true\n    }\n"
        tail += "}\n"
    else:
        tail += "android {\n    sourceSets {\n        main {\n            java.srcDirs += ['src/main/kotlin']\n            assets.srcDirs += ['src/main/assets']\n            res.srcDirs += ['src/main/res']\n        }\n    }\n"
        if compose:
            tail += "    buildFeatures {\n        compose true\n    }\n"
        tail += "}\n"
    repos = [r for r in spec.get("repos", []) if isinstance(r, dict)]
    rb = ""
    for r in repos:
        u = r.get("url")
        if isinstance(u, str) and re.fullmatch(r"https://[A-Za-z0-9._~:/?#@!$&*+,;=%-]+", u):
            rb += ('    maven { url = uri("' + u + '") }\n') if kts else ("    maven { url '" + u + "' }\n")
    if rb:
        tail += "repositories {\n" + rb + "}\n"
    fmt = kts_dep if kts else groovy_dep
    deps = []
    if compose:
        deps += [fmt(k, c) for k, c in HOST_DEPS]
    seen = {c.split(":")[0] + ":" + c.split(":")[1] for k, c in HOST_DEPS} if compose else set()
    for l in spec.get("libs", []):
        if isinstance(l, str) and MAVEN_COORD.fullmatch(l.strip()):
            key = ":".join(l.strip().split(":")[:2])
            if key not in seen:
                seen.add(key)
                deps.append(fmt("", l.strip()))
    libs = os.path.join(build, "libs")
    for a in spec.get("aars", []):
        if isinstance(a, str) and re.fullmatch(r"libs/[0-9a-f]{64}\.aar", a) and os.path.exists(os.path.join(build, a)):
            deps.append(("    implementation files('" + a + "')") if not kts else ('    implementation(files("' + a + '"))'))
    if deps:
        tail += "dependencies {\n" + "\n".join(deps) + "\n}\n"
    write_text(gradle, text.rstrip("\n") + "\n" + tail)
    log("gradle patched: " + os.path.relpath(gradle, build) + ", " + str(len(deps)) + " dependency line(s), compose " + str(compose))


def patch_host(spec):
    build = os.path.join(PROJ, "android", "build")
    stub = os.path.join(PROJ, "android", "aot_host_manifest.xml")
    info = host_info()
    kdir = os.path.join(build, "src", "main", "kotlin")
    if not os.path.isdir(kdir) and not os.path.exists(stub):
        log("host: nothing injected, template left as is")
        return {"host": False}
    gradle = find_gradle(build)
    if not gradle:
        raise BuildFail("no build.gradle was found in android/build, so the AotNative plugin cannot be added")
    manifest = find_manifest(build)
    if not manifest:
        raise BuildFail("no AndroidManifest.xml was found in android/build")
    log("host: gradle " + os.path.relpath(gradle, build) + ", manifest " + os.path.relpath(manifest, build))
    if os.path.exists(stub):
        merge_manifest(manifest, stub)
    patch_gradle(build, gradle, spec, info)
    return {"host": True, "host_gradle": os.path.relpath(gradle, build), "host_manifest": os.path.relpath(manifest, build), "compose": bool(info.get("compose"))}


def preset(index, name, export_format, spec, archs):
    g = godot_block(spec)
    opts = [
        ("custom_template/debug", '""'),
        ("custom_template/release", '""'),
        ("gradle_build/use_gradle_build", "true"),
        ("gradle_build/gradle_build_directory", '""'),
        ("gradle_build/android_source_template", '""'),
        ("gradle_build/compress_native_libraries", "false"),
        ("gradle_build/export_format", str(export_format)),
        ("gradle_build/min_sdk", '"' + str(spec.get("minSdk", 26)) + '"'),
        ("gradle_build/target_sdk", '"' + str(spec.get("targetSdk", 36)) + '"'),
    ]
    for a in ALL_ARCHS:
        opts.append(("architectures/" + a, "true" if a in archs else "false"))
    opts += [
        ("version/code", str(spec.get("versionCode", 1))),
        ("version/name", '"' + str(spec.get("versionName", "1.0")) + '"'),
        ("package/unique_name", '"' + spec["applicationId"] + '"'),
        ("package/name", '"' + str(spec.get("name", "Game")).replace('"', "") + '"'),
        ("package/signed", "false"),
        ("package/app_category", "2"),
        ("package/retain_data_on_uninstall", "false"),
        ("package/exclude_from_recents", "false"),
        ("screen/immersive_mode", "true"),
        ("user_data_backup/allow", "false"),
        ("permissions/internet", "true"),
        ("keystore/debug", '""'),
        ("keystore/release", '""'),
    ]
    head = [
        "[preset." + str(index) + "]",
        "",
        'name="' + name + '"',
        'platform="Android"',
        "runnable=" + ("true" if index == 0 else "false"),
        "advanced_options=false",
        "dedicated_server=false",
        'custom_features=""',
        'export_filter="all_resources"',
        'include_filter=""',
        'exclude_filter="assets_src/*, android/*"',
        'export_path=""',
        'encryption_include_filters=""',
        'encryption_exclude_filters=""',
        "encrypt_pck=false",
        "encrypt_directory=false",
        "",
        "[preset." + str(index) + ".options]",
        "",
    ]
    return "\n".join(head + [k + "=" + v for k, v in opts]) + "\n"


def write_presets(spec):
    g = godot_block(spec)
    archs = [a for a in g.get("abis", []) if a in ARCH_KEYS] or ["arm64-v8a"]
    text = preset(0, "Android APK", 0, spec, archs) + "\n" + preset(1, "Android AAB", 1, spec, archs)
    with open(os.path.join(PROJ, "export_presets.cfg"), "w", encoding="utf-8") as f:
        f.write(text)
    return archs


def write_editor_settings(verstr):
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or ""
    java = os.environ.get("JAVA_HOME") or ""
    if not sdk or not java:
        raise BuildFail("ANDROID_HOME and JAVA_HOME must be set on the runner")
    ks = os.path.abspath("aot-debug.keystore")
    if not os.path.exists(ks):
        run_logged(["keytool", "-genkeypair", "-keystore", ks, "-storepass", "android", "-keypass", "android", "-alias", "androiddebugkey", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000", "-dname", "CN=Android Debug,O=Android,C=US"])
    major = ".".join(verstr.split(".")[:2])
    body = "\n".join([
        "[gd_resource type=\"EditorSettings\" format=3]",
        "",
        "[resource]",
        'export/android/android_sdk_path = "' + sdk + '"',
        'export/android/java_sdk_path = "' + java + '"',
        'export/android/debug_keystore = "' + ks + '"',
        'export/android/debug_keystore_user = "androiddebugkey"',
        'export/android/debug_keystore_pass = "android"',
        "",
    ])
    cfg = os.path.expanduser("~/.config/godot")
    os.makedirs(cfg, exist_ok=True)
    for name in ("editor_settings-4.tres", "editor_settings-" + major + ".tres"):
        with open(os.path.join(cfg, name), "w", encoding="utf-8") as f:
            f.write(body)


def collect(src, dest_dir, name):
    if not os.path.exists(src) or os.path.getsize(src) == 0:
        raise BuildFail(os.path.basename(src) + " was not produced")
    os.makedirs(dest_dir, exist_ok=True)
    out = os.path.join(dest_dir, name)
    shutil.copy2(src, out)
    return out


def export(exe, kind, preset_name, out_file):
    if os.path.exists(out_file):
        os.remove(out_file)
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    flag = "--export-debug" if kind == "debug" else "--export-release"
    code = run_logged([exe, "--headless", "--path", os.path.abspath(PROJ), flag, preset_name, out_file])
    return code


def project_main():
    if os.path.exists(LOG):
        os.remove(LOG)
    extra = {}
    try:
        spec = read_spec()
        extra = {"applicationId": spec["applicationId"], "godot": godot_block(spec).get("version", "")}
        exe, tdir = fetch_godot(spec)
        verstr, tsrc = install_templates(tdir)
        prepare_project(spec)
        write_editor_settings(verstr)
        archs = write_presets(spec)
        extra["abis"] = archs
        install_build_template(tsrc, verstr)
        if not os.path.exists(os.path.join(PROJ, "android", ".dumped")):
            dump_template()
        extra.update(patch_host(spec))
        for attempt in (1, 2):
            code = run_logged([exe, "--headless", "--path", os.path.abspath(PROJ), "--import"])
            if os.path.isdir(os.path.join(PROJ, ".godot", "imported")) or code == 0:
                break
            log("import attempt " + str(attempt) + " exited " + str(code))
        out = os.path.abspath("godot-out")
        shutil.rmtree(out, ignore_errors=True)
        debug_apk = os.path.join(out, "debug.apk")
        release_apk = os.path.join(out, "release.apk")
        release_aab = os.path.join(out, "release.aab")
        export(exe, "debug", "Android APK", debug_apk)
        d = collect(debug_apk, os.path.join(PROJ, "build", "outputs", "apk", "debug"), "app-debug.apk")
        export(exe, "release", "Android APK", release_apk)
        r = collect(release_apk, os.path.join(PROJ, "build", "outputs", "apk", "release"), "app-release.apk")
        extra["apk_bytes"] = os.path.getsize(r)
        try:
            export(exe, "release", "Android AAB", release_aab)
            collect(release_aab, os.path.join(PROJ, "build", "outputs", "bundle", "release"), "app-release.aab")
            extra["aab"] = True
            log("aab: built")
        except BuildFail as e:
            extra["aab"] = False
            extra["aab_note"] = str(e)
            log("aab: skipped, " + str(e))
        status(True, "built", extra)
        return 0
    except BuildFail as e:
        log("FAILED: " + str(e))
        status(False, str(e), extra)
        return 1
    except Exception as e:
        log("FAILED: " + type(e).__name__ + ": " + str(e))
        log(traceback.format_exc())
        status(False, type(e).__name__ + ": " + str(e), extra)
        return 1


COMMANDS = {"version": cmd_version, "project": project_main}


def main(argv):
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print("usage: aot_godot_build.py " + "|".join(sorted(COMMANDS)))
        return 2
    return COMMANDS[argv[1]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
