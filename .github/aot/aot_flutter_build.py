import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
import zipfile

ABIS = os.environ.get("AOT_ABIS", "android-arm,android-arm64")
ROOT = os.path.abspath("hello")
STATUS = os.path.abspath("flutter-build-status.json")
PKG = "dev.miaot.hello"

MAIN_DART = """import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

void main() => runApp(const HelloApp());

class HelloApp extends StatelessWidget {
  const HelloApp({super.key});

  @override
  Widget build(BuildContext context) {
    return const MaterialApp(home: HelloPage());
  }
}

class HelloPage extends StatefulWidget {
  const HelloPage({super.key});

  @override
  State<HelloPage> createState() => _HelloPageState();
}

class _HelloPageState extends State<HelloPage> {
  static const MethodChannel channel = MethodChannel('aot/bridge');
  String abi = '...';

  @override
  void initState() {
    super.initState();
    channel.invokeMethod<String>('abi').then((v) {
      setState(() {
        abi = v ?? 'unknown';
      });
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(body: Center(child: Text('Hello from Flutter on ' + abi)));
  }
}
"""

MAIN_ACTIVITY = """package %s

import android.os.Build
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "aot/bridge").setMethodCallHandler { call, result ->
            if (call.method == "abi") {
                result.success(Build.SUPPORTED_ABIS.joinToString(","))
            } else {
                result.notImplemented()
            }
        }
    }
}
""" % PKG

def run(args, cwd=None):
    print("$ " + " ".join(args), flush=True)
    r = subprocess.run(args, cwd=cwd)
    return r.returncode


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def status(ok, note, extra=None):
    d = {"ok": ok, "note": note, "abis": ABIS, "flutter": os.environ.get("FLUTTER_VERSION", "")}
    if extra:
        d.update(extra)
    write(STATUS, json.dumps(d, indent=2))


def create():
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    code = run(["flutter", "create", "--org", "dev.miaot", "--project-name", "hello", "--platforms", "android", ROOT])
    if code != 0:
        return False
    write(os.path.join(ROOT, "lib", "main.dart"), MAIN_DART)
    kdir = os.path.join(ROOT, "android", "app", "src", "main", "kotlin", *PKG.split("."))
    for old in [os.path.join(ROOT, "android", "app", "src", "main", "kotlin")]:
        if os.path.isdir(old):
            shutil.rmtree(old)
    write(os.path.join(kdir, "MainActivity.kt"), MAIN_ACTIVITY)
    return True


def check(apk):
    found = set()
    with zipfile.ZipFile(apk) as z:
        for n in z.namelist():
            m = re.match(r"lib/([^/]+)/libflutter\.so$", n)
            if m:
                found.add(m.group(1))
    return sorted(found)


def main():
    if not create():
        status(False, "flutter create failed")
        return 1
    code = run(["flutter", "build", "apk", "--release", "--target-platform", ABIS], cwd=ROOT)
    apk = os.path.join(ROOT, "build", "app", "outputs", "flutter-apk", "app-release.apk")
    if code != 0 or not os.path.exists(apk):
        status(False, "flutter build failed")
        return 1
    abis = check(apk)
    want = {"armeabi-v7a", "arm64-v8a"} if "android-arm," in ABIS + "," and "android-arm64" in ABIS else set(abis)
    ok = want.issubset(set(abis))
    shutil.copy2(apk, os.path.abspath("hello-release.apk"))
    lock = os.path.join(ROOT, "pubspec.lock")
    if os.path.exists(lock):
        shutil.copy2(lock, os.path.abspath("pubspec.lock"))
    status(ok, "built" if ok else "missing abis", {"abis_in_apk": abis, "bytes": os.path.getsize(apk)})
    return 0 if ok else 1


PROJ = os.path.abspath("proj")
HOST = os.path.abspath("flutter-host")
LOG = os.path.abspath("build.log")
FACTS = os.path.abspath("build-facts.txt")
ABI_TARGETS = {"armeabi-v7a": "android-arm", "arm64-v8a": "android-arm64", "x86_64": "android-x64", "x86": "android-x86"}
OVERLAY = ["lib", "packages", "assets", "fonts", "pubspec.yaml", "pubspec.lock"]


class BuildFail(Exception):
    pass


def log(text):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n")
    print(text, flush=True)


def run_logged(args, cwd=None):
    log("$ " + " ".join(args))
    p = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    with open(LOG, "a", encoding="utf-8") as f:
        for raw in p.stdout:
            line = raw.decode("utf-8", errors="replace")
            f.write(line)
            sys.stdout.write(line)
            sys.stdout.flush()
    return p.wait()


def read_spec():
    path = os.path.join(PROJ, "project.json")
    if not os.path.exists(path):
        raise BuildFail("project.json is missing from the uploaded source")
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    if not isinstance(spec.get("applicationId"), str) or not spec["applicationId"]:
        raise BuildFail("project.json has no applicationId")
    if not os.path.exists(os.path.join(PROJ, "pubspec.yaml")):
        raise BuildFail("pubspec.yaml is missing from the uploaded source")
    return spec


def pubspec_name():
    with open(os.path.join(PROJ, "pubspec.yaml"), encoding="utf-8") as f:
        m = re.search(r"^name:\s*([A-Za-z0-9_]+)\s*$", f.read(), re.M)
    if not m:
        raise BuildFail("pubspec.yaml has no name")
    return m.group(1)


def target_platforms(spec):
    abis = [a for a in spec.get("abis", []) if isinstance(a, str)] or ["armeabi-v7a", "arm64-v8a"]
    out = []
    for a in abis:
        t = ABI_TARGETS.get(a)
        if t and t not in out:
            out.append(t)
    if not out:
        raise BuildFail("project.json abis has no known ABI: " + ", ".join(abis))
    return abis, ",".join(out)


def copy_any(src, dst):
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)


def find_files(root, name):
    out = []
    for base, dirs, files in os.walk(root):
        if name in files:
            out.append(os.path.join(base, name))
    return sorted(out)


def create_host(spec):
    if os.path.isdir(HOST):
        shutil.rmtree(HOST)
    app_id = spec["applicationId"]
    org = app_id.rsplit(".", 1)[0] if "." in app_id else "dev.miaot"
    code = run_logged(["flutter", "create", "--org", org, "--project-name", pubspec_name(), "--platforms", "android", HOST])
    if code != 0:
        raise BuildFail("flutter create failed (exit " + str(code) + ")")


def overlay():
    for name in OVERLAY:
        src = os.path.join(PROJ, name)
        if os.path.exists(src):
            copy_any(src, os.path.join(HOST, name))
    main_src = os.path.join(PROJ, "android", "app", "src", "main")
    main_dst = os.path.join(HOST, "android", "app", "src", "main")
    if not os.path.isdir(main_src):
        return
    if find_files(main_src, "MainActivity.kt"):
        for sub in ("kotlin", "java"):
            shutil.rmtree(os.path.join(main_dst, sub), ignore_errors=True)
    copy_any(main_src, main_dst)


def resolve_deps():
    # The overlay keeps the source file times with copy2. An old pubspec.yaml can be older than the
    # package config that flutter create wrote, and then flutter build skips pub get and the host keeps
    # the blank app's packages. Touch it and run pub get here so the log shows the real result.
    pub = os.path.join(HOST, "pubspec.yaml")
    if os.path.exists(pub):
        os.utime(pub, None)
    code = run_logged(["flutter", "pub", "get"], cwd=HOST)
    if code != 0:
        raise BuildFail("flutter pub get failed (exit " + str(code) + ")")


def prune_empty(root):
    for base, dirs, files in os.walk(root, topdown=False):
        if base != root and not os.listdir(base):
            os.rmdir(base)


def relocate_main_activity(namespace):
    kroot = os.path.join(HOST, "android", "app", "src", "main", "kotlin")
    found = find_files(kroot, "MainActivity.kt")
    if not found:
        raise BuildFail("no MainActivity.kt in android/app/src/main/kotlin")
    src = found[0]
    with open(src, encoding="utf-8") as f:
        text = f.read()
    if re.search(r"^package\s+\S+", text, re.M):
        text = re.sub(r"^package\s+\S+", "package " + namespace, text, count=1, flags=re.M)
    else:
        text = "package " + namespace + "\n\n" + text
    dst = os.path.join(kroot, *namespace.split("."), "MainActivity.kt")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(text)
    if os.path.abspath(src) != os.path.abspath(dst):
        os.remove(src)
    for extra in found[1:]:
        if os.path.abspath(extra) != os.path.abspath(dst):
            os.remove(extra)
    prune_empty(kroot)


COMPOSE_BOM = "2024.11.00"
COMPILE_SDK = 36
BASE_DEPS = [
    'platform("androidx.compose:compose-bom:' + COMPOSE_BOM + '")',
    '"androidx.compose.ui:ui"',
    '"androidx.compose.ui:ui-graphics"',
    '"androidx.compose.foundation:foundation"',
    '"androidx.compose.material3:material3"',
    '"androidx.compose.material:material-icons-core"',
    '"androidx.activity:activity-compose:1.9.3"',
    '"androidx.core:core-ktx:1.13.1"',
]
KEEP_RULES = "-dontwarn **\n-keepattributes *Annotation*,Signature,InnerClasses,EnclosingMethod,Exceptions,SourceFile,LineNumberTable\n-keep class kotlin.Metadata { *; }\n-keep class aot.** { *; }\n"


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


def need(text, pattern, what, flags=re.M):
    m = re.search(pattern, text, flags)
    if not m:
        raise BuildFail("expected " + what + " was not found in the generated Flutter host")
    return m


def set_line(text, key, value, what):
    pat = r"^([ \t]*)" + key + r"\s*=.*$"
    need(text, pat, what + " line")
    return re.sub(pat, lambda m: m.group(1) + key + " = " + value, text, count=1, flags=re.M)


def patch_settings(path):
    text = read_text(path)
    m = need(text, r'^([ \t]*)id\("org\.jetbrains\.kotlin\.android"\)\s*version\s*"([^"]+)".*$', "kotlin plugin version in settings.gradle.kts")
    kotlin = m.group(2)
    if int(kotlin.split(".")[0]) < 2:
        raise BuildFail("the Flutter host uses Kotlin " + kotlin + ", Compose needs Kotlin 2.0 or newer")
    if "org.jetbrains.kotlin.plugin.compose" not in text:
        line = m.group(1) + 'id("org.jetbrains.kotlin.plugin.compose") version "' + kotlin + '" apply false'
        text = text[:m.end()] + "\n" + line + text[m.end():]
        write_text(path, text)
    return kotlin


def patch_root(path, spec):
    repos = [r for r in spec.get("repos", []) if isinstance(r, dict)]
    if not repos:
        return 0
    text = read_text(path)
    block = ""
    for r in repos:
        u = r.get("url")
        gs = [g for g in r.get("groups", []) if isinstance(g, str) and re.fullmatch(r"[A-Za-z0-9_.\-]+", g)]
        if not (isinstance(u, str) and re.fullmatch(r"https://[A-Za-z0-9._~:/?#@!$&*+,;=%-]+", u) and gs):
            continue
        if u in text or u in block:
            continue
        block += '        maven {\n            url = uri("' + u + '")\n            content {\n' + "".join('                includeGroup("' + g + '")\n' for g in gs) + "            }\n        }\n"
    if not block:
        return 0
    m = need(text, r"^allprojects\s*\{\s*\n\s*repositories\s*\{[ \t]*\n", "allprojects repositories block in android/build.gradle.kts")
    write_text(path, text[:m.end()] + block + text[m.end():])
    return 1


def dep_key(item):
    m = re.search(r'"([^":]+:[^":]+)', item)
    return m.group(1) if m else item


def wanted_deps(spec):
    out = list(BASE_DEPS)
    have = {dep_key(d) for d in out}
    for l in spec.get("libs", []):
        if not isinstance(l, str) or not l.strip():
            continue
        key = ":".join(l.split(":")[:2])
        if key in have:
            continue
        have.add(key)
        out.append('"' + esc(l) + '"')
    for a in spec.get("aars", []):
        if isinstance(a, str) and re.fullmatch(r"libs/[0-9a-f]{64}\.aar", a):
            out.append('files("' + a + '")')
    return out


def patch_app(path, spec, kotlin):
    text = read_text(path)
    ns = spec.get("namespace") or spec["applicationId"]
    text = set_line(text, "namespace", '"' + esc(ns) + '"', "namespace")
    text = set_line(text, "applicationId", '"' + esc(spec["applicationId"]) + '"', "applicationId")
    text = set_line(text, "minSdk", str(int(spec.get("minSdk", 24))), "minSdk")
    text = set_line(text, "targetSdk", str(min(int(spec.get("targetSdk", COMPILE_SDK)), COMPILE_SDK)), "targetSdk")
    text = set_line(text, "versionCode", str(int(spec.get("versionCode", 1))), "versionCode")
    text = set_line(text, "versionName", '"' + esc(str(spec.get("versionName", "1.0"))) + '"', "versionName")
    if "kotlin.plugin.compose" not in text:
        m = need(text, r'^([ \t]*)id\("(?:kotlin-android|org\.jetbrains\.kotlin\.android)"\).*$', "kotlin-android plugin in the plugins block")
        text = text[:m.end()] + "\n" + m.group(1) + 'id("org.jetbrains.kotlin.plugin.compose")' + text[m.end():]
    if not re.search(r"^\s*compose\s*=\s*true", text, re.M):
        m = need(text, r'^([ \t]*)namespace\s*=.*$', "namespace line")
        text = text[:m.end()] + "\n\n    buildFeatures {\n        compose = true\n        buildConfig = true\n    }\n" + text[m.end():]
    if "abiFilters" not in text:
        abis, _ = target_platforms(spec)
        m = need(text, r'^([ \t]*)versionName\s*=.*$', "versionName line")
        lst = ", ".join('"' + a + '"' for a in abis)
        text = text[:m.end()] + "\n" + m.group(1) + "ndk {\n" + m.group(1) + "    abiFilters.addAll(listOf(" + lst + "))\n" + m.group(1) + "}" + text[m.end():]
    if bool(spec.get("debugSuffix", True)) and "applicationIdSuffix" not in text:
        m = need(text, r"^([ \t]*)buildTypes\s*\{[ \t]*$", "buildTypes block")
        ind = m.group(1)
        text = text[:m.end()] + "\n" + ind + "    debug {\n" + ind + '        applicationIdSuffix = ".debug"\n' + ind + "    }" + text[m.end():]
    if "aot-rules.pro" not in text:
        m = need(text, r"^([ \t]*)release\s*\{[ \t]*$", "release build type")
        text = text[:m.end()] + "\n" + m.group(1) + '    proguardFiles("aot-rules.pro")' + text[m.end():]
    deps = wanted_deps(spec)
    m = re.search(r"^dependencies\s*\{[ \t]*$", text, re.M)
    if m:
        end = text.index("\n}", m.end()) if "\n}" in text[m.end():] else len(text)
        body = text[m.end():end]
        add = [d for d in deps if (dep_key(d) not in body)]
        if add:
            text = text[:m.end()] + "".join("\n    implementation(" + d + ")" for d in add) + text[m.end():]
    else:
        text = text.rstrip("\n") + "\n\ndependencies {\n" + "".join("    implementation(" + d + ")\n" for d in deps) + "}\n"
    write_text(path, text)


def patch_host(spec):
    android = os.path.join(HOST, "android")
    app = os.path.join(android, "app", "build.gradle.kts")
    settings = os.path.join(android, "settings.gradle.kts")
    root = os.path.join(android, "build.gradle.kts")
    for p in (app, settings, root):
        if not os.path.exists(p):
            raise BuildFail("android/" + os.path.relpath(p, android) + " is missing from the generated host")
    kotlin = patch_settings(settings)
    patch_root(root, spec)
    patch_app(app, spec, kotlin)
    write_text(os.path.join(android, "app", "aot-rules.pro"), KEEP_RULES)
    libs = os.path.join(PROJ, "libs")
    if os.path.isdir(libs):
        copy_any(libs, os.path.join(android, "app", "libs"))


def collect(variant, flutter_apk):
    dest = os.path.join(PROJ, "build", "outputs", "apk", variant)
    os.makedirs(dest, exist_ok=True)
    src = os.path.join(HOST, "build", "app", "outputs", "flutter-apk", flutter_apk)
    if not os.path.exists(src):
        raise BuildFail(flutter_apk + " was not produced")
    out = os.path.join(dest, "app-" + variant + ".apk")
    shutil.copy2(src, out)
    return out


def tool_versions():
    try:
        p = subprocess.run(["flutter", "--version", "--machine"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
        d = json.loads(p.stdout.decode("utf-8", errors="replace"))
        return str(d.get("frameworkVersion", "")), str(d.get("dartSdkVersion", ""))
    except Exception:
        return os.environ.get("FLUTTER_VERSION", ""), ""


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def write_facts(extra, ok, note):
    try:
        fv, dv = tool_versions()
        built = extra.get("abis_in_apk")
        abis = built or extra.get("abis") or []
        lock = os.path.join(HOST, "pubspec.lock")
        lines = [
            "flutter: kind flutter, Flutter " + (fv or "unknown") + ", Dart " + (dv or "unknown"),
            "flutter abis " + ("built: " if built else "requested: ") + (", ".join(abis) if abis else "none"),
            "pubspec.lock sha256: " + (file_sha(lock) if os.path.exists(lock) else "none"),
            "flutter status: " + ("ok" if ok else "failed") + ", " + note,
        ]
        with open(FACTS, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        log("facts not written: " + str(e))


def collect_aab(targets):
    try:
        code = run_logged(["flutter", "build", "appbundle", "--release", "--target-platform", targets], cwd=HOST)
        if code != 0:
            return {"aab": False, "aab_note": "flutter build appbundle failed (exit " + str(code) + ")"}
        src = os.path.join(HOST, "build", "app", "outputs", "bundle", "release", "app-release.aab")
        if not os.path.exists(src):
            return {"aab": False, "aab_note": "app-release.aab was not produced"}
        dest = os.path.join(PROJ, "build", "outputs", "bundle", "release")
        os.makedirs(dest, exist_ok=True)
        out = os.path.join(dest, "app-release.aab")
        shutil.copy2(src, out)
        return {"aab": True, "aab_bytes": os.path.getsize(out)}
    except Exception as e:
        return {"aab": False, "aab_note": type(e).__name__ + ": " + str(e)[:200]}


def project_main():
    if os.path.exists(LOG):
        os.remove(LOG)
    extra = {}
    try:
        spec = read_spec()
        abis, targets = target_platforms(spec)
        extra = {"abis": abis, "applicationId": spec["applicationId"]}
        create_host(spec)
        overlay()
        resolve_deps()
        relocate_main_activity(spec.get("namespace") or spec["applicationId"])
        patch_host(spec)
        for mode in ("debug", "release"):
            code = run_logged(["flutter", "build", "apk", "--" + mode, "--target-platform", targets], cwd=HOST)
            if code != 0:
                raise BuildFail("flutter build apk --" + mode + " failed (exit " + str(code) + ")")
        debug = collect("debug", "app-debug.apk")
        release = collect("release", "app-release.apk")
        found = check(release)
        want = [a for a in abis if a in ABI_TARGETS]
        missing = [a for a in want if a not in found]
        extra.update({"abis_in_apk": found, "bytes": os.path.getsize(release)})
        lock = os.path.join(HOST, "pubspec.lock")
        if os.path.exists(lock):
            shutil.copy2(lock, os.path.abspath("pubspec.lock"))
        extra.update(collect_aab(targets))
        if extra.get("aab"):
            log("aab: built " + str(extra.get("aab_bytes")) + " bytes")
        else:
            log("aab: skipped, " + str(extra.get("aab_note")))
        if missing:
            raise BuildFail("release APK is missing ABIs: " + ", ".join(missing))
        write_facts(extra, True, "built")
        status(True, "built", extra)
        return 0
    except BuildFail as e:
        log("FAILED: " + str(e))
        write_facts(extra, False, str(e))
        status(False, str(e), extra)
        return 1
    except Exception as e:
        log("FAILED: " + type(e).__name__ + ": " + str(e))
        log(traceback.format_exc())
        write_facts(extra, False, type(e).__name__ + ": " + str(e))
        status(False, type(e).__name__ + ": " + str(e), extra)
        return 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "project":
        sys.exit(project_main())
    sys.exit(main())
