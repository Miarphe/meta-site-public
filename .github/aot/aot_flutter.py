import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import zipfile
from bisect import bisect_left

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aot_dart_lift as dart_lift
import aot_dart_names as dart_names
import aot_dart_skel as dart_skel
import aot_recover as R

BLUTTER_URL = "https://github.com/worawit/blutter"
BLUTTER_REF = os.environ.get("BLUTTER_REF", "main")
LIBS = os.path.join("in", "lib", "arm64-v8a")
OLDER_LIBS = os.path.join("older_in", "lib", "arm64-v8a")
PLATFORM_LIMIT = 1900 * 1024 * 1024
RUN_TIMEOUT = 7200
VER = re.compile(rb"(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?) \((stable|beta|dev)\)")
FRAMEWORK = ("dart", "flutter", "_")
QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
KEY = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}$")
ROUTE = re.compile(r"^/[A-Za-z0-9_\-/:.]*$")
SQL = re.compile(r"\b(create\s+table|create\s+index|select\s|insert\s+into|update\s+\w+\s+set|delete\s+from|alter\s+table|drop\s+table)", re.I)
CLASS = re.compile(r"^\s*(?:abstract\s+|final\s+|base\s+|sealed\s+)*class\s+(\w+)(?:<[^>{]*>)?(?:\s+extends\s+([\w<>,?. ]+?))?(?:\s+(?:with|implements)\s+[^{]+)?\s*\{?\s*$")
FUNC = re.compile(r"^ {0,2}[^/ \n}].*\)\s*(?:async\*?\s*|sync\*\s*)?\{\s*$")
STR = re.compile(r'^\[pp\+0x([0-9a-f]+)\] String: "(.*)"\s*$')
MAPH = re.compile(r"^\[pp\+0x([0-9a-f]+)\] Map<String, String>\((\d+)\) \{\s*$")
PAIR = re.compile(r'^\s*"((?:[^"\\]|\\.)*)":\s*"((?:[^"\\]|\\.)*)",?\s*$')
PPREF = re.compile(r"\[pp\+0x([0-9a-f]+)\]")
CHEAD = re.compile(r"^// class id: (\d+), size: (\S+?)(?:,|$)")
FADDR = re.compile(r"^\s*// \*\* addr: 0x([0-9a-f]+), size: 0x([0-9a-f]+)")
FIELD = re.compile(r"^\s+(.+?);\s*//\s*offset:\s*(0x[0-9a-f]+)")
CALL = re.compile(r"\bbl\s+#?0x([0-9a-f]+)")
TAIL = re.compile(r"\bb\s+#0x([0-9a-f]+)")
HLCALL = re.compile(r"^\s*//\s+0x[0-9a-f]+:\s+(?:r|x)\d+\s*=\s*([A-Za-z_$][\w$]*)\(")
CLASSL = re.compile(r"^\s*(?:(?:abstract|final|base|sealed|mixin|interface)\s+)*class\s+([^\s<{(]+)(.*)$")
EXTENDS = re.compile(r"\bextends\s+(.+?)(?:\s+(?:with|implements)\b|\s*\{?\s*$)")
SIG = re.compile(r"^ {1,4}(?![/ ])(.*\))\s*(?:async\*?\s*|sync\*\s*)?\{\s*$")
SHORT = re.compile(r"^_{0,2}[A-Za-z]{1,3}[0-9]{0,2}$")
TABLE = re.compile(r"(?:create\s+table(?:\s+if\s+not\s+exists)?|from|into|update|join)\s+[\"`\[]?([A-Za-z_]\w*)", re.I)
HINTS = (("http", "network"), ("sqlite", "database"), ("shared_preferences", "settings"), ("firebase", "firebase"), ("notification", "notifications"), ("purchase", "purchases"), ("login", "login"), ("password", "login"), ("payment", "payments"))
HLINE = re.compile(r"^\s*// 0x([0-9a-f]+): (\S.*)$")
FILLADDR = re.compile(r"#?0x([0-9a-f]{4,})")
FILLPP = re.compile(r"\[pp\+0x([0-9a-f]+)\]")
POOLHEAD = re.compile(r"^\[pp\+0x([0-9a-f]+)\] (.*)$")
OBJHEAD = re.compile(r"^(?:Obj!)?([A-Za-z_$][\w$]*)(?:<[^>]*>)?(?:@[0-9a-f]+)?\s*(?::\s*\{)?\s*$")
OBJHEAD2 = re.compile(r"^(?:Obj!)?([A-Za-z_$][\w$]*)(?:<[^>]*>)?@[0-9a-f]+")
FIELDV = re.compile(r"^\s*([\w$]+):\s*(.+?),?\s*$")
NUM = re.compile(r"^-?(?:0x[0-9a-f]+|\d+(?:\.\d+)?(?:e[+-]?\d+)?)$", re.I)
CONST_CLASSES = ("Duration", "Color", "EdgeInsets", "EdgeInsetsDirectional", "BorderRadius", "BorderRadiusDirectional", "Radius", "Cubic", "Interval", "Threshold", "Curve")
ASMC = (
    ("curve", re.compile(r"\bCurves\.([A-Za-z0-9_]+)")),
    ("duration_ms", re.compile(r"\bDuration\([^)]*?milliseconds:\s*(\d+)")),
    ("duration_us", re.compile(r"\bDuration\([^)]*?microseconds:\s*(\d+)")),
    ("radius", re.compile(r"\b(?:Border)?Radius\.circular\(\s*(-?\d+(?:\.\d+)?)")),
    ("padding", re.compile(r"\bEdgeInsets\.(all|symmetric|only|fromLTRB)\(([^)]*)\)")),
    ("color", re.compile(r"\bColor\(\s*0x([0-9a-fA-F]{8})")),
    ("hero", re.compile(r"\bHero\b")),
)
ASM_QUICK = ("Curves.", "Duration(", "Radius.circular", "EdgeInsets.", "Color(", "Hero")
STOP = {"the", "and", "you", "to", "of", "is", "this", "your", "with", "for", "are", "will"}


def commit():
    r = subprocess.run(["git", "-C", "blutter", "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip()


def info():
    try:
        with open("info.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def put_status(state, error="", **kw):
    s = info()
    s["state"] = state
    s["error"] = error
    s.update(kw)
    with open("flutter-status.json", "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)
    R.upload("flutter-status.json", "flutter-status.json")


def cmd_tool():
    shutil.rmtree("blutter", ignore_errors=True)
    subprocess.run(["git", "clone", BLUTTER_URL, "blutter"], check=True)
    subprocess.run(["git", "-C", "blutter", "checkout", BLUTTER_REF], check=True)
    print("blutter " + commit())


def cmd_fetch():
    os.makedirs(LIBS, exist_ok=True)
    got = 0
    for a in R.assets():
        n = a["name"]
        if n in ("libapp.so.xz", "libflutter.so.xz"):
            tmp = os.path.join("in", n)
            R.fetch_asset(a, tmp)
            with lzma.open(tmp, "rb") as src, open(os.path.join(LIBS, n[:-3]), "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.remove(tmp)
            got += 1
        elif n in ("older-libapp.so.xz", "older-libflutter.so.xz"):
            os.makedirs(OLDER_LIBS, exist_ok=True)
            tmp = os.path.join("in", n)
            R.fetch_asset(a, tmp)
            with lzma.open(tmp, "rb") as src, open(os.path.join(OLDER_LIBS, n[len("older-"):-3]), "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.remove(tmp)
        elif n == "flutter-packages.txt":
            R.fetch_asset(a, "packages.txt")
    if got != 2:
        raise SystemExit("the Dart libraries were not uploaded")
    with open(os.path.join(LIBS, "libflutter.so"), "rb") as f:
        m = VER.search(f.read())
    dart = m.group(1).decode() if m else "unknown"
    meta = {"dart": dart, "blutter": commit()}
    with open("info.json", "w", encoding="utf-8") as f:
        json.dump(meta, f)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("dart=" + dart + "\nblutter=" + meta["blutter"] + "\n")
    print("dart " + dart)


def cmd_run():
    shutil.rmtree("out", ignore_errors=True)
    if os.path.exists("blutter.log"):
        os.remove("blutter.log")
    try:
        with open("blutter.log", "ab") as log:
            code = subprocess.run(["python3", "blutter/blutter.py", LIBS, "out"], stdout=log, stderr=subprocess.STDOUT, timeout=RUN_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        raise SystemExit("blutter did not finish in " + str(RUN_TIMEOUT // 60) + " minutes")
    print("blutter exit " + str(code))
    if code != 0:
        raise SystemExit("blutter exit " + str(code))
    if not os.path.isdir(os.path.join("out", "asm")):
        raise SystemExit("blutter produced no asm folder")


def packages():
    try:
        with open("packages.txt", encoding="utf-8") as f:
            return {l.strip() for l in f if l.strip()}
    except Exception:
        return set()


def is_framework(seg, known):
    low = seg.lower()
    return seg in known or low.startswith(FRAMEWORK)


def walk_asm(root):
    libs = {}
    files = []
    for d, _, names in os.walk(root):
        rel = os.path.relpath(d, root)
        seg = "(root)" if rel == "." else rel.split(os.sep)[0]
        for n in names:
            p = os.path.join(d, n)
            size = os.path.getsize(p)
            row = libs.setdefault(seg, [0, 0])
            row[0] += 1
            row[1] += size
            files.append((seg, p, size))
    return libs, files


def head(path, lines):
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= lines:
                    out.append("# cut after " + str(lines) + " lines\n")
                    break
                out.append(line)
    except Exception:
        return ""
    return "".join(out)


def tsv(v):
    return str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def quoted(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                for m in QUOTED.finditer(line):
                    yield m.group(1)
    except Exception:
        return


def read_pool(path):
    strings = []
    maps = []
    cur = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if cur is not None:
                    if line.startswith("}"):
                        maps.append(cur)
                        cur = None
                        continue
                    m = PAIR.match(line)
                    if m:
                        cur[2][m.group(1)] = m.group(2)
                    continue
                m = STR.match(line)
                if m:
                    strings.append((m.group(1), m.group(2)))
                    continue
                h = MAPH.match(line)
                if h:
                    cur = (h.group(1), int(h.group(2)), {})
    except Exception:
        pass
    return strings, maps


def english(values):
    words = 0
    hits = 0
    for v in values:
        for w in re.findall(r"[a-z]+", v.lower()):
            words += 1
            if w in STOP:
                hits += 1
    return hits / words if words else 0.0


def i18n_cand(maps):
    cand = [m for m in maps if m[1] >= 20 and sum(1 for k in m[2] if KEY.match(k)) * 2 >= len(m[2])]
    cand.sort(key=lambda m: -english(m[2].values()))
    return cand


def extract_i18n(maps):
    cand = i18n_cand(maps)
    if not cand:
        return "", "", 0, 0
    cand.sort(key=lambda m: -english(m[2].values()))
    best = cand[0]
    rows = [k + "\t" + tsv(v) for k, v in sorted(best[2].items())]
    use = cand
    keys = sorted(set().union(*[m[2].keys() for m in use]))
    head = "key\t" + "\t".join("pp+0x" + m[0] for m in use)
    full = [head] + [k + "\t" + "\t".join(tsv(m[2].get(k, "")) for m in use) for k in keys]
    return "\n".join(rows) + "\n", "\n".join(full) + "\n", len(rows), len(cand)


def is_text(t):
    if len(t) < 3 or KEY.match(t) or ROUTE.match(t) or SQL.search(t):
        return False
    return bool(re.search(r"[A-Za-z]{3}", t)) and (" " in t or t[:1].isupper())


def stem(w):
    return w[:5]


def pair_keys(classes, strings, maps):
    best = {}
    for m in i18n_cand(maps)[:1]:
        best = m[2]
    byoff = {}
    for off, t in strings:
        byoff.setdefault(off, t)
    keyoffs = {}
    for off, t in byoff.items():
        if KEY.match(t):
            keyoffs.setdefault(t, []).append(off)
    order = sorted(byoff, key=lambda o: int(o, 16))
    nums = [int(o, 16) for o in order]
    words = {}
    for off, t in byoff.items():
        if is_text(t):
            for w in set(re.findall(r"[a-z]{3,}", t.lower())):
                words.setdefault(stem(w), set()).add(off)
    fn_strs = {}
    fn_of = {}
    for cname, c in classes.items():
        for fa, offs in c["fstr"].items():
            fid = cname + "." + fa
            fn_strs[fid] = set(offs)
            for o in offs:
                fn_of.setdefault(o, set()).add(fid)
    rows = []
    stats = {"exact": 0, "high": 0, "medium": 0, "low": 0, "unpaired": 0}
    for key in sorted(best):
        rows.append((key, best[key], "exact", 99, "translation map", ""))
        stats["exact"] += 1
    for key in sorted(keyoffs):
        if key in best:
            continue
        score = {}
        basis = {}
        shared = {}
        kw = {stem(w) for w in key.split("_") if len(w) >= 3}
        for ko in keyoffs[key]:
            for fid in fn_of.get(ko, ()):
                for o2 in fn_strs[fid]:
                    if o2 in byoff and is_text(byoff[o2]):
                        shared.setdefault(o2, set()).add(fid)
            i = bisect_left(nums, int(ko, 16))
            for j in range(max(0, i - 6), min(len(order), i + 7)):
                o2 = order[j]
                if o2 == ko or not is_text(byoff[o2]):
                    continue
                d = abs(nums[j] - int(ko, 16))
                add = 2 if d <= 0x20 else 1
                if add > score.get(("p", o2), 0):
                    score[("p", o2)] = add
        cands = set(shared) | {k[1] for k in score}
        for w in kw:
            for o2 in words.get(w, ()):
                if len(words[w]) <= 3000:
                    cands.add(o2)
        top = None
        for o2 in cands:
            t = byoff[o2]
            sc = 0
            why = []
            if o2 in shared:
                sc += 3
                why.append("same function x" + str(len(shared[o2])))
            pp = score.get(("p", o2), 0)
            if pp:
                sc += pp
                why.append("pool neighbour" if pp == 2 else "pool nearby")
            tw = {stem(w) for w in re.findall(r"[a-z]{3,}", t.lower())}
            ov = len(kw & tw)
            if kw and ov:
                sc += 2 if ov * 2 >= len(kw) else 1
                why.append("words " + str(ov) + "/" + str(len(kw)))
            if sc < 3:
                continue
            cur = (sc, ov, -len(t))
            if top is None or cur > top[0]:
                top = (cur, o2, sc, why, len(shared.get(o2, ())))
        if top is None:
            stats["unpaired"] += 1
            rows.append((key, "", "none", 0, "", ""))
            continue
        sc = top[2]
        conf = "high" if sc >= 6 else ("medium" if sc >= 4 else "low")
        stats[conf] += 1
        rows.append((key, byoff[top[1]], conf, sc, ", ".join(top[3]), "pp+0x" + top[1]))
    return rows, stats


def pairs_tsv(rows):
    head = "key\ttext\tconfidence\tscore\tbasis\ttext_pool"
    return head + "\n" + "\n".join(tsv(r[0]) + "\t" + tsv(r[1]) + "\t" + r[2] + "\t" + str(r[3]) + "\t" + tsv(r[4]) + "\t" + r[5] for r in rows) + "\n"


def extract_routes(strings):
    seen = set()
    for off, t in strings:
        if 1 < len(t) < 120 and ROUTE.match(t):
            seen.add(t)
    rows = sorted(seen)
    return "\n".join(rows) + "\n" if rows else "", len(rows)


def strings_table(strings):
    seen = set()
    rows = []
    for off, t in strings:
        if len(t) < 2 or t in seen:
            continue
        seen.add(t)
        rows.append(off + "\t" + t)
    return "\n".join(rows) + "\n" if rows else "", len(rows)


def scan_asm(files, root, wanted):
    classes = []
    where = {}
    for seg, p, size in files:
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        func = ""
        cur = None
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = parse_class(line)
                    if m:
                        cur = [m[0], m[1], rel, 0]
                        classes.append(cur)
                        continue
                    if "// ** addr:" in line and cur is not None:
                        cur[3] += 1
                    if FUNC.match(line):
                        func = line[:line.index("(")].split()[-1][-60:] if line[:line.index("(")].split() else ""
                    if "[pp+0x" in line:
                        for r in PPREF.findall(line):
                            if r in wanted:
                                lst = where.setdefault(r, [])
                                item = (cur[0] if cur else "", func, rel)
                                if item not in lst:
                                    lst.append(item)
        except Exception:
            continue
    c = ["class\textends\tfile\tmethods"] + [tsv(x[0]) + "\t" + tsv(x[1]) + "\t" + x[2] + "\t" + str(x[3]) for x in classes]
    return "\n".join(c) + "\n", len(classes), where


def sql_table(strings, where):
    rows = []
    for off, t in strings:
        if len(t) >= 12 and SQL.search(t):
            refs = where.get(off) or [("", "", "")]
            for cls, fn, rel in refs:
                rows.append("pp+0x" + off + "\t" + cls + "\t" + tsv(fn) + "\t" + rel + "\t" + tsv(t))
    return "\n".join(rows) + "\n" if rows else "", len(rows)


def parse_class(line):
    if line.lstrip().startswith("//"):
        return None
    m = CLASSL.match(line)
    if not m:
        return None
    e = EXTENDS.search(m.group(2))
    return m.group(1), (e.group(1).strip() if e else "")


def sig_name(sig):
    head = sig.split("(")[0].strip()
    if "<anonymous closure>" in sig or "<anonymous closure>" in head:
        return "<anonymous closure>"
    parts = head.split()
    return parts[-1][-60:] if parts else ""


def verdict(classes):
    names = [n for n in classes if "&" not in n and not n.startswith("_$")]
    if not names:
        return "unknown", 0, 0
    short = sum(1 for n in names if SHORT.match(n))
    ratio = short / len(names)
    v = "obfuscated" if ratio > 0.6 else ("partly obfuscated" if ratio > 0.25 else "readable")
    return v, short, len(names)


def safe_name(n):
    return re.sub(r"[^A-Za-z0-9_]", "_", n)[:80] or "anon"


def scan_deep(files, root, strmap):
    classes = {}
    table = {}
    cur = None
    cur_n = ""
    for seg, p, size in files:
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        func = None
        want_name = None
        pending = None
        sig = None
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    h = CHEAD.match(line)
                    if h:
                        pending = (h.group(1), h.group(2))
                        continue
                    m = parse_class(line)
                    if m:
                        name = m[0]
                        cur_n = name
                        cur = classes.setdefault(name, {"ext": "", "file": rel, "id": "", "size": "", "fields": [], "funcs": [], "strs": [], "sql": [], "calls": {}, "named": {}, "tails": {}, "fstr": {}, "pp": set(), "hits": []})
                        if m[1]:
                            cur["ext"] = m[1]
                        if pending:
                            cur["id"], cur["size"] = pending
                        pending = None
                        func = None
                        want_name = None
                        sig = None
                        continue
                    if cur is None:
                        continue
                    a = FADDR.match(line)
                    if a:
                        func = [a.group(1), sig or "fn_" + a.group(1), int(a.group(2), 16)]
                        cur["funcs"].append(func)
                        table[a.group(1)] = (cur_n, func)
                        want_name = None if sig else func
                        sig = None
                        continue
                    if want_name is None and func is None or want_name is None:
                        sm = SIG.match(line)
                        if sm:
                            sig = sig_name(sm.group(1))
                            func = None
                            continue
                    if want_name is not None:
                        t = line.strip()
                        if t and not t.startswith("//"):
                            head_part = t.split("(")[0].split()
                            if "(" in t and head_part:
                                want_name[1] = head_part[-1][-60:]
                            want_name = None
                    if func is None:
                        fm = FIELD.match(line)
                        if fm:
                            cur["fields"].append(tsv(fm.group(1)) + " @" + fm.group(2))
                        continue
                    if "[pp+0x" in line:
                        for r in PPREF.findall(line):
                            cur["pp"].add(r)
                            t = strmap.get(r)
                            if t is not None:
                                cur["fstr"].setdefault(func[0], []).append(r)
                                if SQL.search(t) and len(t) >= 12:
                                    if t not in cur["sql"]:
                                        cur["sql"].append(t)
                                elif t not in cur["strs"]:
                                    cur["strs"].append(t)
                    if any(q in line for q in ASM_QUICK):
                        for kind, rx in ASMC:
                            for hm in rx.finditer(line):
                                cur["hits"].append((kind, hm.group(0).strip()[:160], hm.groups()))
                    for c in CALL.findall(line):
                        cur["calls"].setdefault(func[0], set()).add(c)
                    for c in TAIL.findall(line):
                        cur["tails"].setdefault(func[0], set()).add(c)
                    hl = HLCALL.match(line)
                    if hl:
                        cur["named"].setdefault(func[0], set()).add(hl.group(1))
        except Exception:
            continue
    return classes, table


def resolve_calls(classes, table):
    rows = []
    seen = set()
    per = {}
    byname = {}
    for name, c in classes.items():
        for fn in c["funcs"]:
            if not fn[1].startswith("fn_") and fn[1] != "<anonymous closure>":
                byname.setdefault(fn[1], []).append((name, fn[1]))
    stats = {"direct": 0, "tail": 0, "by_name": 0}
    for name, c in classes.items():
        names = {fn[0]: fn[1] for fn in c["funcs"]}
        out = []
        for addr in set(c["calls"]) | set(c["tails"]) | set(c["named"]):
            caller = name + "." + names.get(addr, "fn_" + addr)
            found = [(t, "direct") for t in sorted(c["calls"].get(addr, ()))] + [(t, "tail") for t in sorted(c["tails"].get(addr, ()))]
            for t, how in found:
                hit = table.get(t)
                if not hit:
                    continue
                callee = hit[0] + "." + hit[1][1]
                if callee == caller:
                    continue
                key = (caller, callee)
                if key in seen:
                    continue
                seen.add(key)
                out.append(callee)
                stats["direct" if how == "direct" else "tail"] += 1
                rows.append(caller + "\t" + callee + "\t" + how)
            for nm in sorted(c["named"].get(addr, ())):
                hits = byname.get(nm, [])
                if len(hits) != 1:
                    continue
                callee = hits[0][0] + "." + hits[0][1]
                key = (caller, callee)
                if callee == caller or key in seen:
                    continue
                seen.add(key)
                out.append(callee)
                stats["by_name"] += 1
                rows.append(caller + "\t" + callee + "\tname")
        per[name] = sorted(set(out))
    return rows, per, stats


def guess_names(classes, keys, routes):
    out = []
    for name, c in sorted(classes.items()):
        votes = []
        for sq in c["sql"]:
            for t in TABLE.findall(sq):
                if t.lower() not in ("select", "set", "values", "where"):
                    votes.append((t.lower(), "sql table " + t, "Dao"))
        for t in c["strs"]:
            if t in routes:
                votes.append((t.strip("/").split("/")[0].lower() or "home", "route " + t, "Screen"))
            elif t in keys:
                votes.append((t.split("_")[0], "i18n key " + t, "Screen"))
            else:
                low = t.lower()
                for needle, label in HINTS:
                    if needle in low:
                        votes.append((label, "string " + tsv(t)[:40], "Client"))
                        break
        if not votes:
            continue
        counts = {}
        for feat, why, suffix in votes:
            counts.setdefault(feat, [0, why, suffix])[0] += 1
        feat, (n, why, suffix) = sorted(counts.items(), key=lambda kv: (-kv[1][0], kv[0]))[0]
        out.append((name, feat, feat.capitalize() + suffix, why, n))
    return out


def names_tsv(guesses):
    rows = ["class\tfeature\tguess\tbasis\tsignals\tkind"] + [tsv(n) + "\t" + tsv(f) + "\t" + tsv(g) + "\t" + tsv(w) + "\t" + str(k) + "\tguess" for n, f, g, w, k in guesses]
    return "\n".join(rows) + "\n"


def features_md(guesses):
    groups = {}
    for n, f, g, w, k in guesses:
        groups.setdefault(f, []).append((n, g, w))
    lines = ["# Features (guessed)", "", "Every name below is a guess from SQL tables, routes, translation keys and strings. The real class names are obfuscated.", ""]
    for f in sorted(groups, key=lambda x: (-len(groups[x]), x)):
        lines.append("## " + f)
        for n, g, w in groups[f]:
            lines.append("- " + n + " (maybe " + g + "; " + w + ")")
        lines.append("")
    return "\n".join(lines) + "\n"


def class_md(name, c, calls, guess):
    lines = ["# " + name, ""]
    if c["ext"]:
        lines.append("extends " + c["ext"])
    lines.append("file asm/" + c["file"])
    if c["id"]:
        lines.append("class id " + c["id"] + ", size " + c["size"])
    if guess:
        lines.append("guess: " + guess[2] + " (" + guess[3] + ")")
    for title, items in (("Fields", c["fields"]), ("Functions", ["0x" + a + " " + n + " (" + str(sz) + " bytes)" for a, n, sz in c["funcs"]]), ("Strings", ['"' + tsv(t) + '"' for t in c["strs"]]), ("SQL", [tsv(t) for t in c["sql"]]), ("Calls", calls)):
        if items:
            lines += ["", "## " + title] + ["- " + i for i in items]
    return "\n".join(lines) + "\n"


def num(v):
    v = v.strip()
    if not NUM.match(v):
        return None
    try:
        return int(v, 16) if v.lower().startswith("0x") else (int(v) if re.match(r"^-?\d+$", v) else float(v))
    except Exception:
        return None


def fmt_num(n):
    return str(int(n)) if float(n) == int(n) else ("%.4f" % n).rstrip("0").rstrip(".")


def read_consts(path):
    entries = []
    cur = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                h = POOLHEAD.match(line)
                if h:
                    cur = {"off": h.group(1), "head": h.group(2).strip(), "fields": [], "cls": ""}
                    m = OBJHEAD2.match(cur["head"]) or OBJHEAD.match(cur["head"])
                    if m:
                        cur["cls"] = m.group(1)
                    entries.append(cur)
                    continue
                if cur is not None:
                    fm = FIELDV.match(line)
                    if fm:
                        cur["fields"].append((fm.group(1), fm.group(2)))
    except Exception:
        pass
    return entries


def interpret(e):
    cls = e["cls"]
    vals = [num(v) for k, v in e["fields"]]
    vals = [v for v in vals if v is not None]
    if cls == "Duration" and vals:
        return "duration_ms", fmt_num(vals[0] / 1000.0)
    if cls == "Color" and vals and isinstance(vals[0], int):
        return "color", "#%08x" % (vals[0] & 0xffffffff)
    if cls.startswith("EdgeInsets") and vals:
        return "padding", ",".join(fmt_num(v) for v in vals)
    if (cls.startswith("BorderRadius") or cls == "Radius") and vals:
        return "radius", ",".join(fmt_num(v) for v in vals)
    if cls == "Cubic" and len(vals) >= 4:
        return "curve", "cubic(" + ",".join(fmt_num(v) for v in vals[:4]) + ")"
    if cls in ("Interval", "Threshold") or "Curve" in cls:
        return "curve", cls + ("(" + ",".join(fmt_num(v) for v in vals) + ")" if vals else "")
    return None


def mine_constants(classes, entries):
    rows = []
    probe = {}
    by_off = {}
    for e in entries:
        c = e["cls"] or e["head"].split(":")[0].split(" ")[0]
        slot = probe.setdefault(c, [0, []])
        slot[0] += 1
        if len(slot[1]) < 3:
            slot[1].append("[pp+0x" + e["off"] + "] " + e["head"] + " " + "; ".join(k + "=" + v for k, v in e["fields"]))
        got = interpret(e)
        if got:
            by_off[e["off"]] = (got[0], got[1], e["head"] + " " + "; ".join(k + "=" + v for k, v in e["fields"]))
    used = set()
    for name, c in sorted(classes.items()):
        for off in sorted(c["pp"]):
            hit = by_off.get(off)
            if hit:
                used.add(off)
                rows.append((name, hit[0], "", hit[1], hit[2], "pool", "pp+0x" + off))
        for kind, raw, groups in c["hits"]:
            if kind == "duration_us":
                rows.append((name, "duration_ms", "", fmt_num(int(groups[0]) / 1000.0), raw, "asm", ""))
            elif kind == "padding":
                rows.append((name, "padding", groups[0], groups[1].strip(), raw, "asm", ""))
            elif kind == "color":
                rows.append((name, "color", "", "#" + groups[0].lower(), raw, "asm", ""))
            elif kind == "hero":
                rows.append((name, "hero", "", "", raw, "asm", ""))
            else:
                rows.append((name, kind, "", groups[0] if groups else "", raw, "asm", ""))
    for off, hit in sorted(by_off.items()):
        if off not in used:
            rows.append(("", hit[0], "", hit[1], hit[2], "pool", "pp+0x" + off))
    return rows, probe


def constants_tsv(rows):
    head = "class\tkind\tname\tvalue\traw\tsource\tlabel\tpool"
    return head + "\n" + "\n".join("\t".join([tsv(r[0]), r[1], tsv(r[2]), tsv(r[3]), tsv(r[4]), r[5], "exact", r[6]]) for r in rows) + "\n"


def probe_consts_text(probe, total):
    lines = ["# How Blutter printed the pool objects", "", str(total) + " pool entries. Constants are read from the first sample of each class below, so check this file first if design_constants.tsv looks empty.", ""]
    for c, (n, ex) in sorted(probe.items(), key=lambda kv: (-kv[1][0], kv[0])):
        lines.append("## " + c + " (" + str(n) + ")")
        lines += ["    " + tsv(x) for x in ex]
        lines.append("")
    return "\n".join(lines) + "\n"


def dossier_md(feat, members, classes, per, pairs, routes):
    lines = ["# " + feat, "", "Guessed feature. Every class below was grouped here from SQL tables, routes, translation keys or strings.", ""]
    keys = []
    sqls = []
    rts = []
    outs = {}
    for n, g, w in members:
        c = classes[n]
        lines.append("## " + n)
        lines.append("- guess " + g + " (" + w + ")")
        lines.append("- file asm/" + c["file"] + (", extends " + c["ext"] if c["ext"] else ""))
        names = [fn[1] for fn in c["funcs"] if not fn[1].startswith("fn_") and fn[1] != "<anonymous closure>"]
        lines.append("- " + str(len(c["funcs"])) + " functions" + (": " + ", ".join(names) if names else ""))
        for t in c["strs"]:
            if t in pairs:
                keys.append(t)
            if t in routes:
                rts.append(t)
        sqls += [q for q in c["sql"] if q not in sqls]
        for callee in per.get(n, []):
            owner = callee.split(".")[0]
            if owner != n:
                outs.setdefault(owner, set()).add(callee)
        lines.append("")
    if keys:
        lines += ["## Text keys used", ""]
        for k in sorted(set(keys)):
            r = pairs[k]
            lines.append("- `" + k + "`: " + (tsv(r[1]) if r[1] else "(no text paired)") + " [" + r[2] + (", " + r[4] if r[4] else "") + "]")
        lines.append("")
    if rts:
        lines += ["## Routes", ""] + ["- " + t for t in sorted(set(rts))] + [""]
    if sqls:
        lines += ["## SQL", ""] + ["- " + tsv(q) for q in sqls] + [""]
    if outs:
        lines += ["## Calls into other classes", ""] + ["- " + o + ": " + ", ".join(sorted(v)) for o, v in sorted(outs.items())] + [""]
    return "\n".join(lines) + "\n"


def dossiers(classes, per, guesses, pair_rows, routes):
    pairs = {r[0]: r for r in pair_rows}
    groups = {}
    for n, f, g, w, k in guesses:
        groups.setdefault(f, []).append((n, g, w))
    out = {}
    index = ["# Feature dossiers", "", "One file per guessed feature. Open the dossier first, then the class notes in classes/.", ""]
    used = set()
    for f in sorted(groups, key=lambda x: (-len(groups[x]), x)):
        fn = safe_name(f)
        if fn in used:
            fn += "_" + str(len(used))
        used.add(fn)
        out["dossiers/" + fn + ".md"] = dossier_md(f, groups[f], classes, per, pairs, routes)
        index.append("- [" + f + "](" + fn + ".md): " + str(len(groups[f])) + " classes")
    out["dossiers/INDEX.md"] = "\n".join(index) + "\n"
    return out


def fill_names(text, table, strmap):
    def addr(m):
        t = table.get(m.group(1))
        if t is None:
            return m.group(0)
        return t[0] + "." + t[1][1]

    def pool(m):
        v = strmap.get(m.group(1))
        if v is None:
            return m.group(0)
        return m.group(0) + "=" + json.dumps(v, ensure_ascii=False)

    text = FILLADDR.sub(addr, text)
    return FILLPP.sub(pool, text)


def condense(files, root, table, strmap):
    out = {}
    cur = None
    for seg, p, size in files:
        block = None
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = parse_class(line)
                    if m:
                        cur = out.setdefault(m[0], [])
                        block = None
                        continue
                    if cur is None:
                        continue
                    a = FADDR.match(line)
                    if a:
                        t = table.get(a.group(1))
                        name = t[1][1] if t else "fn_" + a.group(1)
                        block = ["fn " + name + " @0x" + a.group(1)]
                        cur.append(block)
                        continue
                    if block is None:
                        continue
                    h = HLINE.match(line)
                    if h:
                        block.append("  " + fill_names(h.group(2).rstrip(), table, strmap))
        except Exception:
            continue
    texts = {}
    for n, blocks in out.items():
        kept = [b for b in blocks if len(b) > 1]
        if kept:
            texts[n] = "\n\n".join("\n".join(b) for b in kept) + "\n"
    return texts


def condensed_dossier(feat, members, texts):
    parts = ["# " + feat + " (condensed)", "", "Only Blutter's higher-level lines, with function and string names filled in. Raw asm is in asm/.", ""]
    found = False
    for n, g, w in members:
        t = texts.get(n)
        if not t:
            continue
        found = True
        parts += ["## " + n, "", "```", t.rstrip("\n"), "```", ""]
    if not found:
        return ""
    return "\n".join(parts) + "\n"


def digests(files, root, strings, maps, everything=None):
    strmap = {off: t for off, t in strings}
    keys = {t for off, t in strings if KEY.match(t)}
    routes = {t for off, t in strings if 1 < len(t) < 120 and ROUTE.match(t)}
    classes, table = scan_deep(files, root, strmap)
    rows, per, cstats = resolve_calls(classes, table)
    guesses = guess_names(classes, keys, routes)
    gmap = {g[0]: g for g in guesses}
    ranked = sorted(classes, key=lambda n: -(len(classes[n]["strs"]) + len(classes[n]["sql"]) + len(per.get(n, [])) + len(classes[n]["funcs"])))
    out = {}
    used = set()
    for n in ranked:
        fn = safe_name(n)
        if fn in used:
            fn = fn + "_" + (classes[n]["id"] or str(len(used)))
        used.add(fn)
        out["classes/" + fn + ".md"] = class_md(n, classes[n], per.get(n, []), gmap.get(n))
    if rows:
        out["calls.tsv"] = "caller\tcallee\thow\n" + "\n".join(rows) + "\n"
    if guesses:
        out["names.tsv"] = names_tsv(guesses)
        out["features.md"] = features_md(guesses)
    entries = read_consts(os.path.join("out", "pp.txt")) if os.path.exists(os.path.join("out", "pp.txt")) else []
    crow, probe = mine_constants(classes, entries)
    out["design_constants.tsv"] = constants_tsv(crow)
    out["constants_probe.txt"] = probe_consts_text(probe, len(entries))
    cstat = {}
    for r in crow:
        cstat[r[1]] = cstat.get(r[1], 0) + 1
    pair_rows, pstats = pair_keys(classes, strings, maps)
    if pair_rows:
        out["key_pairs.tsv"] = pairs_tsv(pair_rows)
    out.update(dossiers(classes, per, guesses, pair_rows, routes))
    texts = condense(files, root, table, strmap)
    cused = set()
    cindex = ["# Condensed functions", "", "One file per class: only the higher-level lines, names filled in.", ""]
    for n in sorted(texts):
        cn = safe_name(n)
        if cn in cused:
            cn = cn + "_" + (classes[n]["id"] if n in classes and classes[n]["id"] else str(len(cused)))
        cused.add(cn)
        out["condensed/" + cn + ".txt"] = texts[n]
        cindex.append("- " + cn + ".txt: " + str(texts[n].count("\n")) + " lines")
    if texts:
        out["condensed/INDEX.md"] = "\n".join(cindex) + "\n"
    groups = {}
    for gn, gf, gg, gw, gk in guesses:
        groups.setdefault(gf, []).append((gn, gg, gw))
    dused = set()
    for gf in sorted(groups):
        dn = safe_name(gf)
        if dn in dused:
            dn += "_" + str(len(dused))
        dused.add(dn)
        cd = condensed_dossier(gf, groups[gf], texts)
        if cd:
            out["dossiers/" + dn + ".condensed.md"] = cd
    ref_rows = []
    rstats = {}
    if os.path.exists("ref_shapes.json") and everything is not None:
        try:
            with open("ref_shapes.json", encoding="utf-8") as f:
                ref = json.load(f)
            kept = {p for seg, p, size in files}
            rest = [f for f in everything if f[1] not in kept]
            more, mtable = scan_deep(rest, root, strmap) if rest else ({}, {})
            every = dict(more)
            every.update(classes)
            ref_rows = dart_names.match(dart_names.shapes(every), ref["shapes"])
            rstats = dart_names.stats(ref_rows)
            rstats["names_ref_dart"] = ref.get("dart", "")
            if ref_rows:
                out["names_ref.tsv"] = dart_names.tsv(ref_rows)
        except Exception as e:
            rstats = {"names_ref_error": str(e)[:80]}
    older_rows = []
    if os.path.exists("older_shapes.json") and everything is not None:
        try:
            with open("older_shapes.json", encoding="utf-8") as f:
                older = json.load(f)
            kept = {p for seg, p, size in files}
            rest = [f for f in everything if f[1] not in kept]
            more, mtable = scan_deep(rest, root, strmap) if rest else ({}, {})
            every = dict(more)
            every.update(classes)
            older_rows = dart_names.match(dart_names.shapes(every), older["shapes"])
            rstats["names_older_rows"] = len(older_rows)
            if older_rows:
                out["names_older.tsv"] = dart_names.tsv(older_rows)
        except Exception as e:
            rstats["names_older_error"] = str(e)[:80]
    merged = dart_names.merge_map(ref_rows, older_rows)
    if merged:
        out["names.map"] = dart_names.map_tsv(merged)
        rstats["names_map_rows"] = len(merged)
        rstats["names_map_on"] = sum(1 for r in merged if r[4] == "yes")
    skel, sstats = dart_skel.render(classes, dart_lift.lift_all(texts))
    out.update(skel)
    out["skeleton/pubspec.draft"] = dart_skel.pubspec("recovered", dart_skel.package_names(sorted(packages())))
    v, short, total = verdict(classes)
    fns = [fn for c in classes.values() for fn in c["funcs"]]
    named = sum(1 for fn in fns if not fn[1].startswith("fn_"))
    stats = {"digests": len(ranked), "calls": len(rows), "names": len(guesses), "functions": len(fns), "named_functions": named, "unnamed_functions": len(fns) - named, "call_direct": cstats["direct"], "call_tail": cstats["tail"], "call_by_name": cstats["by_name"], "short_class_names": short, "class_names": total, "obfuscation_verdict": v, "constants": len(crow), "constant_kinds": cstat, "pairs_exact": pstats["exact"], "pairs_high": pstats["high"], "pairs_medium": pstats["medium"], "pairs_low": pstats["low"], "pairs_unpaired": pstats["unpaired"], "dossiers": len(guesses and {g[1] for g in guesses}), "condensed_classes": len(texts), "condensed_lines": sum(t.count("\n") for t in texts.values())}
    stats.update(sstats)
    stats.update(rstats)
    return out, stats


def probe_text(files, root, paths):
    parts = []
    for seg, p, size in sorted(files, key=lambda f: -f[2])[:3]:
        parts.append("## " + os.path.relpath(p, root).replace(os.sep, "/") + "\n" + head(p, 120))
    for path in paths:
        parts.append("## " + os.path.basename(path) + " around the first key\n" + around_key(path))
    return "\n".join(parts)


def around_key(path, before=3, after=6):
    prev = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if any(KEY.match(t) for t in QUOTED.findall(line)):
                    rest = [line]
                    for j, nxt in enumerate(f):
                        if j >= after:
                            break
                        rest.append(nxt)
                    return "".join(prev + rest)
                prev.append(line)
                if len(prev) > before:
                    prev.pop(0)
                if i > 3000000:
                    break
    except Exception:
        pass
    return ""


def extras(keep, root, everything=None):
    out = {}
    counts = {}
    paths = [os.path.join("out", n) for n in ("pp.txt", "objs.txt") if os.path.exists(os.path.join("out", n))]
    strings = []
    maps = []
    if os.path.exists(os.path.join("out", "pp.txt")):
        strings, maps = read_pool(os.path.join("out", "pp.txt"))
    counts["pool_strings"] = len(strings)
    counts["pool_maps"] = len(maps)
    try:
        text, n = strings_table(strings)
        counts["strings"] = n
        if text:
            out["strings.tsv"] = text
    except Exception as e:
        counts["strings"] = "error " + str(e)[:80]
    try:
        en, allm, n, nm = extract_i18n(maps)
        counts["i18n"] = n
        counts["i18n_maps"] = nm
        if en:
            out["i18n.tsv"] = en
            out["i18n_all.tsv"] = allm
    except Exception as e:
        counts["i18n"] = "error " + str(e)[:80]
    try:
        text, n = extract_routes(strings)
        counts["routes"] = n
        if text:
            out["routes.txt"] = text
    except Exception as e:
        counts["routes"] = "error " + str(e)[:80]
    try:
        wanted = {off for off, t in strings if len(t) >= 12 and SQL.search(t)}
        cls, nc, where = scan_asm(keep, root, wanted)
        counts["classes"] = nc
        if nc:
            out["classes.tsv"] = cls
        text, ns = sql_table(strings, where)
        counts["sql"] = ns
        if text:
            out["sql.tsv"] = text
    except Exception as e:
        counts["sql"] = counts["classes"] = "error " + str(e)[:80]
    try:
        d, dc = digests(keep, root, strings, maps, everything)
        out.update(d)
        counts.update(dc)
    except Exception as e:
        counts["digests"] = "error " + str(e)[:80]
    try:
        out["probe.txt"] = probe_text(keep, root, paths)
    except Exception:
        pass
    return out, counts


def build_zip(libs, mine, keep, ex, with_asm):
    kept = 0
    spent = 0
    with zipfile.ZipFile("flutter.zip", "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        rows = ["lib\tfiles\tbytes\tkept"] + [k + "\t" + str(v[0]) + "\t" + str(v[1]) + "\t" + ("yes" if k in mine else "no") for k, v in sorted(libs.items())]
        z.writestr("flutter/blutter/libs.tsv", "\n".join(rows) + "\n")
        for name, text in ex.items():
            z.writestr("flutter/blutter/" + name, text)
            spent += len(text)
        for fn in ("pp.txt", "objs.txt"):
            path = os.path.join("out", fn)
            if os.path.exists(path):
                z.write(path, "flutter/blutter/" + fn)
                spent += os.path.getsize(path)
        if with_asm:
            root = os.path.join("out", "asm")
            for seg, p, size in keep:
                rel = os.path.relpath(p, root).replace(os.sep, "/")
                z.write(p, "flutter/blutter/asm/" + rel)
                spent += size
                kept += 1
    return kept, spent


def cmd_pack():
    root = os.path.join("out", "asm")
    known = packages()
    libs, files = walk_asm(root)
    mine = {seg for seg in libs if not is_framework(seg, known)}
    keep = [f for f in files if f[0] in mine]
    if not keep:
        mine = set(libs)
        keep = list(files)
    keep.sort(key=lambda f: f[2])
    ex, counts = extras(keep, root, files)
    try:
        more, mstats = dart_skel.analyze(ex, dart_skel.package_names(sorted(packages())))
        ex.update(more)
        counts.update(mstats)
    except Exception as e:
        counts["analyze_note"] = "error " + str(e)[:80]
    kept, spent = build_zip(libs, mine, keep, ex, True)
    left_out = 0
    if os.path.getsize("flutter.zip") > PLATFORM_LIMIT:
        kept, spent = build_zip(libs, mine, keep, ex, False)
        left_out = len(keep)
    R.upload("flutter.zip", "flutter.zip")
    put_status("done", files=kept, bytes=spent, dropped=left_out, asm_left_out=left_out > 0, libs=len(libs), kept_libs=sorted(mine), found=counts, obfuscation=counts.get("obfuscation_verdict", "unknown"))


def cmd_fail():
    err = ""
    if os.path.exists("blutter.log"):
        with open("blutter.log", errors="replace") as f:
            err = f.read().strip()[-600:]
    put_status("failed", err or "the Flutter step stopped before Blutter ran")


if __name__ == "__main__":
    c = sys.argv[1]
    if c == "tool":
        cmd_tool()
    elif c == "fetch":
        cmd_fetch()
    elif c == "run":
        cmd_run()
    elif c == "pack":
        cmd_pack()
    elif c == "fail":
        cmd_fail()
    else:
        raise SystemExit("unknown command " + c)
