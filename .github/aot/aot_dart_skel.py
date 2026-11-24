import json
import os
import re
import shutil
import subprocess

import aot_dart_lift as lift

IDENT = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
FIELD_TEXT = re.compile(r"^((?:(?:late|final|static|const|covariant)\s+)*)(.+?)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s+@(0x[0-9a-f]+)$")
RESERVED = {"abstract", "as", "assert", "async", "await", "break", "case", "catch", "class", "const", "continue", "default", "do", "else", "enum", "export", "extends", "extension", "external", "factory", "false", "final", "finally", "for", "get", "if", "implements", "import", "in", "is", "late", "library", "mixin", "new", "null", "on", "operator", "part", "required", "rethrow", "return", "set", "static", "super", "switch", "sync", "this", "throw", "true", "try", "typedef", "var", "void", "while", "with", "yield", "dynamic"}
PACKAGE_LINE = re.compile(r"^[a-z][a-z0-9_]{0,60}$")
MACHINE = re.compile(r"^(ERROR|WARNING|INFO)\|([A-Z_]+)\|([A-Z_]+)\|(.*?)\|(\d+)\|(\d+)\|(\d+)\|(.*)$")


def file_for(rel):
    parts = [re.sub(r"[^A-Za-z0-9_.]", "_", p) for p in rel.split("/") if p and p != ".."]
    if len(parts) > 1:
        parts = parts[1:]
    name = "/".join(parts) or "unknown.dart"
    if not name.endswith(".dart"):
        name += ".dart"
    return "lib/recovered/" + name


def class_ok(name):
    return bool(IDENT.match(name)) and name not in RESERVED


def field_line(text, used):
    m = FIELD_TEXT.match(text.strip())
    if not m:
        return "  // " + text.strip()[:120]
    mods, typ, name, off = m.group(1).split(), m.group(2), m.group(3), m.group(4)
    if name in used or name in RESERVED:
        return "  // " + text.strip()[:120]
    if "static" in mods or "const" in mods:
        return "  // " + text.strip()[:120]
    used.add(name)
    kind = "final " if "final" in mods else "late "
    return "  " + kind + typ + " " + name + "; // " + off


def func_line(cls, fn, used, lifted=None, fields=None):
    addr, name, size = fn[0], fn[1], fn[2]
    info = "0x" + addr + ", " + str(size) + " bytes"
    tag = " // " + info
    if name == cls:
        if "" in used:
            return "  // " + info + " (constructor)"
        used.add("")
        return "  " + cls + "();" + tag
    it = (lifted or {}).get(addr)
    if it is not None and name != cls:
        code = lift.dart_line(cls, name, it, fields or set(), used)
        if code:
            pseudo = ["  //   " + p[:140] for p in it["pseudo"][:12]]
            return "\n".join(["  // lifted (" + it["confidence"] + ", " + it["kind"] + "), " + info, "  // pseudo-code:"] + pseudo + ["  " + code])
    if name.startswith("fn_") or not IDENT.match(name) or name in RESERVED or name in used:
        return "  // " + info + " " + name
    used.add(name)
    return "  dynamic " + name + "() => throw UnimplementedError();" + tag


def field_names(c):
    names = set()
    for f in c.get("fields", []):
        m = FIELD_TEXT.match(f.strip())
        if m:
            names.add(m.group(3))
    return names


def render_class(name, c, lifted=None):
    ext = c.get("ext", "")
    head = "class " + name
    if ext and "&" not in ext and "$" not in ext and re.match(r"^[A-Za-z_][A-Za-z0-9_<>,?. ]*$", ext):
        head += " extends " + ext
    lines = [head + " {"]
    used = set()
    for f in c.get("fields", []):
        lines.append(field_line(f, used))
    for fn in c.get("funcs", []):
        lines.append(func_line(name, fn, used, lifted, field_names(c)))
    lines.append("}")
    return "\n".join(lines) + "\n"


def render(classes, lifted=None):
    files = {}
    names = {}
    skipped = 0
    for n in sorted(classes):
        c = classes[n]
        if not class_ok(n):
            skipped += 1
            continue
        path = file_for(c.get("file", ""))
        files.setdefault(path, []).append(render_class(n, c, (lifted or {}).get(n)))
        names[n] = path
    out = {}
    for path, blocks in files.items():
        out["skeleton/" + path] = "// Recovered shape only: names, fields and signatures. Bodies are not recovered.\n\n" + "\n".join(blocks)
    rows = ["class\tfile"] + [n + "\t" + p for n, p in sorted(names.items())]
    out["skeleton/classes.tsv"] = "\n".join(rows) + "\n"
    count = sum(len(v) for v in (lifted or {}).values())
    if count:
        out["skeleton/LIFTED.tsv"] = lift.tsv(lifted)
    return out, {"lifted_functions": count, "skeleton_files": len(files), "skeleton_classes": len(names), "skeleton_skipped": skipped}


def package_names(lines):
    seen = []
    for l in lines:
        t = l.strip()
        if PACKAGE_LINE.match(t) and t not in seen and t not in ("flutter", "dart", "flutter_test"):
            seen.append(t)
    return seen


def pubspec(name, pkgs):
    out = ["name: " + name, "description: Draft from a recovered app. Versions are unknown; every package is any.", "publish_to: none", "version: 1.0.0+1", "", "environment:", "  sdk: \">=3.5.0 <4.0.0\"", "", "dependencies:", "  flutter:", "    sdk: flutter"]
    for p in pkgs:
        out.append("  " + p + ": any")
    out += ["", "flutter:", "  uses-material-design: true", ""]
    return "\n".join(out)


def parse_machine(text):
    rows = []
    for line in text.splitlines():
        m = MACHINE.match(line.strip())
        if not m:
            continue
        rows.append({"severity": m.group(1), "type": m.group(2), "code": m.group(3).lower(), "file": m.group(4), "line": int(m.group(5)), "message": m.group(8)})
    return rows


def summarize(rows, total_files):
    by_code = {}
    by_file = {}
    for r in rows:
        if r["severity"] != "ERROR":
            continue
        by_code[r["code"]] = by_code.get(r["code"], 0) + 1
        by_file[r["file"]] = by_file.get(r["file"], 0) + 1
    clean = max(total_files - len(by_file), 0)
    return {"errors": sum(by_code.values()), "files": total_files, "files_clean": clean, "files_with_errors": len(by_file), "codes": dict(sorted(by_code.items(), key=lambda kv: -kv[1]))}, by_file


def analysis_files(rows, summary, by_file, resolved, note):
    out = {}
    md = ["# Dart analysis of the skeleton", "", "Run with `dart analyze` on the runner. An error here means a name or type did not resolve, which is expected for a shape-only skeleton.", ""]
    md.append("- Packages resolved: " + ("yes" if resolved else "no, analysed against Flutter only"))
    if note:
        md.append("- Note: " + note)
    md.append("- Files: " + str(summary["files"]) + ", clean: " + str(summary["files_clean"]) + ", with errors: " + str(summary["files_with_errors"]))
    md.append("- Errors: " + str(summary["errors"]))
    md.append("")
    md.append("| code | count |")
    md.append("| --- | --- |")
    for k, v in summary["codes"].items():
        md.append("| " + k + " | " + str(v) + " |")
    out["skeleton/ANALYZE.md"] = "\n".join(md) + "\n"
    out["skeleton/analyze.tsv"] = "file\terrors\n" + "\n".join(f + "\t" + str(n) for f, n in sorted(by_file.items())) + "\n"
    out["skeleton/analyze.json"] = json.dumps(dict(summary, packages_resolved=resolved, note=note), indent=2) + "\n"
    return out


def run(cmd, cwd, limit):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=limit)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, str(e)


def analyze(files, pkgs, work="skel_work"):
    tool = shutil.which("flutter")
    if not tool:
        return {}, {"analyzed": False, "analyze_note": "no Flutter SDK on the runner"}
    shutil.rmtree(work, ignore_errors=True)
    for rel, text in files.items():
        if not rel.startswith("skeleton/lib/"):
            continue
        dest = os.path.join(work, rel[len("skeleton/"):])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(text)
    total = sum(1 for r in files if r.startswith("skeleton/lib/"))
    if total == 0:
        return {}, {"analyzed": False, "analyze_note": "no skeleton files"}
    resolved = False
    note = ""
    with open(os.path.join(work, "pubspec.yaml"), "w", encoding="utf-8") as f:
        f.write(pubspec("recovered", pkgs))
    code, log = run([tool, "pub", "get"], work, 900)
    if code == 0:
        resolved = True
    else:
        note = "pub get failed with the draft packages: " + log.strip().splitlines()[-1][:160] if log.strip() else "pub get failed"
        with open(os.path.join(work, "pubspec.yaml"), "w", encoding="utf-8") as f:
            f.write(pubspec("recovered", []))
        code, log = run([tool, "pub", "get"], work, 900)
        if code != 0:
            return {"skeleton/ANALYZE.md": "# Dart analysis of the skeleton\n\nNot run: " + note + "\n"}, {"analyzed": False, "analyze_note": note}
    code, log = run(["dart", "analyze", "--format=machine", "lib"], work, 1200)
    rows = parse_machine(log)
    summary, by_file = summarize(rows, total)
    out = analysis_files(rows, summary, by_file, resolved, note)
    return out, {"analyzed": True, "analyze_errors": summary["errors"], "analyze_files_clean": summary["files_clean"], "analyze_packages_resolved": resolved}
