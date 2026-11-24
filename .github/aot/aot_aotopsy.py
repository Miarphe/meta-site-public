import json
import lzma
import os
import re
import shutil
import subprocess
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aot_recover as R

URL = "https://github.com/BroNils/aotopsy"
REF = os.environ.get("AOTOPSY_REF", "")
BIN = os.path.abspath("aotopsy-bin")
LIB = os.path.abspath(os.path.join("in", "libapp.so"))
OUT = os.path.abspath("aotopsy-out")
TRY_TIMEOUT = 3600
SUB = re.compile(r"^\s{1,6}([a-z][a-z0-9-]+)(?:\s{2,}|\s*$)", re.M)
PSEUDO = ("pseudo", "pseudocode", "pseudo-code", "export-pseudo", "decompile", "export-pseudocode")


def log(text):
    with open("aotopsy.log", "a", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n")


def run(args, cwd=None, timeout=TRY_TIMEOUT):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, errors="replace")
        log("$ " + " ".join(args) + "\nexit " + str(r.returncode) + "\n" + (r.stdout or "") + (r.stderr or ""))
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        log("$ " + " ".join(args) + "\ntimed out after " + str(timeout // 60) + " minutes")
        return -1, ""
    except Exception as e:
        log("$ " + " ".join(args) + "\nerror " + str(e))
        return -2, ""


def commit():
    r = subprocess.run(["git", "-C", "aotopsy", "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip()


def licence():
    for n in sorted(os.listdir("aotopsy")):
        if n.upper().startswith(("LICENSE", "LICENCE", "COPYING")):
            try:
                with open(os.path.join("aotopsy", n), encoding="utf-8", errors="replace") as f:
                    return n, f.read(400).strip()
            except Exception:
                return n, ""
    return "", ""


def main_dir():
    found = []
    for d, _, names in os.walk("aotopsy"):
        if ".git" in d.split(os.sep):
            continue
        for n in names:
            if n.endswith(".go") and not n.endswith("_test.go"):
                try:
                    with open(os.path.join(d, n), encoding="utf-8", errors="replace") as f:
                        if re.search(r"^package main\b", f.read(4000), re.M):
                            found.append(d)
                            break
                except Exception:
                    pass
    found.sort(key=lambda d: (0 if os.sep + "cmd" in d else 1, len(d)))
    return found[0] if found else "aotopsy"


def put_status(state, error="", **kw):
    s = {"state": state, "error": error}
    try:
        with open("aotopsy-info.json", encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    s.update(kw)
    with open("aotopsy-status.json", "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)
    R.upload("aotopsy-status.json", "aotopsy-status.json")


def cmd_tool():
    shutil.rmtree("aotopsy", ignore_errors=True)
    subprocess.run(["git", "clone", URL, "aotopsy"], check=True)
    if REF:
        subprocess.run(["git", "-C", "aotopsy", "checkout", REF], check=True)
    name, head = licence()
    info = {"aotopsy_url": URL, "aotopsy_commit": commit(), "aotopsy_licence_file": name, "aotopsy_licence_head": head}
    with open("aotopsy-info.json", "w", encoding="utf-8") as f:
        json.dump(info, f)
    log("aotopsy " + info["aotopsy_commit"] + "\nlicence file " + (name or "none") + "\n" + head)
    print("aotopsy " + info["aotopsy_commit"] + " licence " + (name or "none"))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write("commit=" + info["aotopsy_commit"] + "\n")


def cmd_build():
    d = main_dir()
    r = subprocess.run(["go", "build", "-o", BIN, "."], cwd=d)
    if r.returncode != 0:
        raise SystemExit("go build failed in " + d)
    print("built from " + d)


def cmd_fetch():
    os.makedirs("in", exist_ok=True)
    for a in R.assets():
        if a["name"] == "libapp.so.xz":
            tmp = os.path.join("in", "libapp.so.xz")
            R.fetch_asset(a, tmp)
            with lzma.open(tmp, "rb") as src, open(LIB, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.remove(tmp)
            return
    raise SystemExit("libapp.so was not uploaded")


def files_in(d):
    n = 0
    for _, _, names in os.walk(d):
        n += len(names)
    return n


def attempt(label, variants, tries):
    for v in variants:
        sub = os.path.join(OUT, label)
        shutil.rmtree(sub, ignore_errors=True)
        os.makedirs(sub, exist_ok=True)
        args = [BIN] + [x.replace("{lib}", LIB).replace("{out}", sub) for x in v]
        code, text = run(args, cwd=sub)
        n = files_in(sub)
        if code == 0 and n == 0 and text.strip():
            with open(os.path.join(sub, "stdout.txt"), "w", encoding="utf-8") as f:
                f.write(text)
            n = 1
        tries.append({"label": label, "args": " ".join(v), "exit": code, "files": n})
        if code == 0 and n > 0:
            return True
    return False


def cmd_run():
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    tries = []
    helptext = ""
    for h in (["--help"], ["help"]):
        code, text = run([BIN] + h, timeout=120)
        helptext += text
    with open(os.path.join(OUT, "help.txt"), "w", encoding="utf-8") as f:
        f.write(helptext)
    attempt("doctor", [["doctor"], ["doctor", "{lib}"]], tries)
    attempt("export-dart", [["export-dart", "--app-only", "{lib}", "{out}"], ["export-dart", "--app-only", "-o", "{out}", "{lib}"], ["export-dart", "--app-only", "{lib}"], ["export-dart", "{lib}", "{out}"]], tries)
    subs = set(SUB.findall(helptext))
    for name in PSEUDO:
        if name in subs:
            attempt(name, [[name, "{lib}", "{out}"], [name, "-o", "{out}", "{lib}"], [name, "{lib}"]], tries)
    got = [t["label"] for t in tries if t["exit"] == 0 and t["files"] > 0]
    with open(os.path.join(OUT, "attempts.json"), "w", encoding="utf-8") as f:
        json.dump(tries, f, indent=1)
    with open("aotopsy-run.json", "w", encoding="utf-8") as f:
        json.dump({"worked": got, "attempts": tries}, f)
    print("worked " + ",".join(got))


def cmd_pack():
    try:
        with open("aotopsy-run.json", encoding="utf-8") as f:
            run_info = json.load(f)
    except Exception:
        run_info = {"worked": [], "attempts": []}
    n = 0
    with zipfile.ZipFile("aotopsy.zip", "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for d, _, names in os.walk(OUT):
            for fn in names:
                p = os.path.join(d, fn)
                z.write(p, "flutter/aotopsy/" + os.path.relpath(p, OUT).replace(os.sep, "/"))
                n += 1
        if os.path.exists("aotopsy.log"):
            z.write("aotopsy.log", "flutter/aotopsy/aotopsy.log")
        if os.path.exists("aotopsy-info.json"):
            z.write("aotopsy-info.json", "flutter/aotopsy/info.json")
    R.upload("aotopsy.zip", "aotopsy.zip")
    put_status("done" if run_info["worked"] else "nothing", "" if run_info["worked"] else "no AOTopsy command produced output", files=n, worked=run_info["worked"], attempts=run_info["attempts"])


def cmd_fail():
    err = ""
    if os.path.exists("aotopsy.log"):
        with open("aotopsy.log", errors="replace") as f:
            err = f.read().strip()[-600:]
    put_status("failed", err or "the AOTopsy step stopped before it ran")


if __name__ == "__main__":
    c = sys.argv[1]
    if c == "tool":
        cmd_tool()
    elif c == "build":
        cmd_build()
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
