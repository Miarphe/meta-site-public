import re

OFF = re.compile(r"@(0x[0-9a-f]+)$")
SHORT = re.compile(r"^_{0,2}[A-Za-z]{1,3}[0-9]{0,2}$")


def shape(c):
    offs = sorted({OFF.search(f).group(1) for f in c.get("fields", []) if OFF.search(f)})
    sizes = sorted(f[2] for f in c.get("funcs", []))
    return [str(c.get("size", "")), offs, len(c.get("funcs", [])), sizes]


def shapes(classes):
    return {n: shape(c) for n, c in classes.items() if "&" not in n and (c.get("fields") or c.get("funcs"))}


def full_key(s):
    return (s[0], tuple(s[1]), s[2], tuple(s[3]))


def coarse_key(s):
    return (s[0], tuple(s[1]), s[2])


def match(app, ref):
    full = {}
    coarse = {}
    for n, s in ref.items():
        full.setdefault(full_key(s), []).append(n)
        coarse.setdefault(coarse_key(s), []).append(n)
    rows = []
    for n in sorted(app):
        if not SHORT.match(n) or n in ref:
            continue
        s = app[n]
        a = full.get(full_key(s), [])
        if len(a) == 1:
            rows.append([n, a[0], "high", "same size, field offsets, method count and method sizes"])
            continue
        b = coarse.get(coarse_key(s), [])
        if len(a) == 0 and len(b) == 1:
            rows.append([n, b[0], "medium", "same size, field offsets and method count"])
            continue
        pool = a or b
        if 1 < len(pool) <= 3:
            rows.append([n, "|".join(sorted(pool)), "low", "several reference classes share this shape"])
    taken = {}
    for r in rows:
        if r[2] != "low":
            taken.setdefault(r[1], []).append(r)
    for name, group in taken.items():
        if len(group) > 1:
            for r in group:
                r[2] = "low"
                r[3] = "another class has the same match: " + ", ".join(sorted(x[0] for x in group))[:100]
    return rows


def stats(rows):
    out = {"names_ref_high": 0, "names_ref_medium": 0, "names_ref_low": 0}
    for r in rows:
        out["names_ref_" + r[2]] += 1
    return out


def tsv(rows):
    return "obfuscated\treal\tconfidence\treason\n" + "".join("\t".join(r) + "\n" for r in rows)


def merge_map(ref_rows, older_rows):
    got = {}
    for source, rows in (("reference", ref_rows), ("older", older_rows)):
        for r in rows:
            if "|" in r[1]:
                continue
            got[r[0]] = [r[0], r[1], r[2], source, "yes" if r[2] in ("high", "medium") else "no"]
    return [got[k] for k in sorted(got)]


def map_tsv(rows):
    return "obfuscated\treal\tconfidence\tsource\tuse\n" + "".join("\t".join(r) + "\n" for r in rows)
