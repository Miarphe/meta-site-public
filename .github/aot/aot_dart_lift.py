import re

HEAD = re.compile(r"^fn (.+?) @0x([0-9a-f]+)$")
LITERAL = re.compile(r'^(?:r|x|w)\d+\s*=\s*(?:\[pp\+0x[0-9a-f]+\]=)?("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false|Null)$')
LOADFIELD = re.compile(r"^(?:LoadField:\s*(?:r|x|w)\d+\s*=\s*(?:r|x|w)\d+->(\w+)|(?:r|x|w)\d+\s*=\s*LoadField:\s*(?:r|x|w)\d+->(\w+))$")
MOVE = re.compile(r"^(?:r|x|w)\d+\s*=\s*(?:r|x|w)\d+$")
QUIET = ("EnterFrame", "LeaveFrame", "ret", "SetupParameters", "Return")
SQL = re.compile(r"\b(create\s+table|create\s+index|select\s|insert\s+into|update\s+\w+\s+set|delete\s+from|alter\s+table|drop\s+table)", re.I)
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quiet(line):
    return any(line == q or line.startswith(q + "(") or line.startswith(q + " ") for q in QUIET)


def lift_lines(lines):
    body = [l.strip() for l in lines if l.strip()]
    value = None
    field = None
    for l in body:
        if quiet(l):
            continue
        m = LITERAL.match(l)
        if m and value is None and field is None:
            value = m.group(1)
            if value.startswith('"'):
                value = re.sub(r"(?<!\\)\$", r"\\$", value)
            continue
        f = LOADFIELD.match(l)
        if f and field is None and value is None:
            field = f.group(1) or f.group(2)
            continue
        if MOVE.match(l):
            continue
        return None
    if not any(l == "ret" or l.startswith("ret ") or l == "Return" for l in body):
        return None
    if value is not None:
        if value.startswith('"') and SQL.search(value) and len(value) >= 14:
            return ("sql", "high", value)
        return ("constant", "high", value)
    if field is not None:
        return ("getter", "medium", "this." + field)
    return ("empty", "high", "null")


def lift_text(text):
    out = {}
    cur = None
    lines = []
    for raw in text.split("\n"):
        m = HEAD.match(raw)
        if m:
            if cur:
                out[cur[1]] = (cur[0], lift_lines(lines), list(lines))
            cur = (m.group(1), m.group(2))
            lines = []
            continue
        if cur:
            lines.append(raw)
    if cur:
        out[cur[1]] = (cur[0], lift_lines(lines), list(lines))
    return out


def lift_all(texts):
    res = {}
    for cls, text in texts.items():
        got = {}
        for addr, (name, lifted, lines) in lift_text(text).items():
            if lifted is None:
                continue
            got[addr] = {"name": name, "kind": lifted[0], "confidence": lifted[1], "expr": lifted[2], "pseudo": [l.strip() for l in lines if l.strip()]}
        if got:
            res[cls] = got
    return res


def dart_line(cls, fn_name, item, fields, used):
    if not IDENT.match(fn_name):
        return None
    if item["kind"] == "getter":
        field = item["expr"].split(".", 1)[1]
        if fn_name in fields or fn_name in used or not IDENT.match(field):
            return None
        used.add(fn_name)
        return "dynamic get " + fn_name + " => " + item["expr"] + ";"
    if fn_name in used:
        return None
    used.add(fn_name)
    if item["kind"] == "empty":
        return "dynamic " + fn_name + "() => null;"
    return "dynamic " + fn_name + "() => " + item["expr"] + ";"


def tsv(lifted):
    rows = ["class\tfunction\taddr\tkind\tconfidence"]
    for cls in sorted(lifted):
        for addr, it in sorted(lifted[cls].items()):
            rows.append(cls + "\t" + it["name"] + "\t0x" + addr + "\t" + it["kind"] + "\t" + it["confidence"])
    return "\n".join(rows) + "\n"
