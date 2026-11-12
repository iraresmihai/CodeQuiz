"""CodeQuiz: a small Kahoot-style coding session server, with a quiz editor.

Run:   python server.py                      (port 8000)
       python server.py 8080                 (other port)
       python server.py --quizzes D:\\quizzes (keep the quiz files in another folder, e.g. OneDrive or a USB stick)

Students: http://<laptop-ip>:<port>/
Host:     http://localhost:<port>/host      (only on this laptop)
Editor:   http://localhost:<port>/editor    (only on this laptop)
Board:    http://localhost:<port>/board     (for the projector)

Student code is compiled (javac only) when it is submitted. It is only RUN when the host
closes a question, or clicks "Compile & run tests" for one submission.
"""
import base64, copy, hashlib, json, os, re, secrets, shutil, socket, subprocess, sys, tempfile, threading, time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
SESSION_QUIZ_FILE = os.path.join(HERE, "session_quiz.json")
RUNNER_SRC = os.path.join(HERE, "runner")
RUNNER_BUILD = os.path.join(HERE, "runner", "build")
QUIZ_DIR = os.path.join(HERE, "quizzes")     # changed by --quizzes

FORMAT = "codequiz-quiz"
FORMAT_VERSION = 1
MAX_CODE = 50_000
MAX_STUDENT_BODY = 200_000
MAX_LOCAL_BODY = 100_000_000
BASE = {"correct": 1000, "partial": 500, "wrong": 0}
MAX_BONUS = 500
COMPARE_MODES = ("lines", "exact", "tokens", "anyorder", "numbers")
BACKUP_EVERY = 180      # seconds: autosave keeps at most one backup per 3 minutes
BACKUPS_KEPT = 40

lock = threading.RLock()          # session state
store_lock = threading.RLock()    # quiz files
compile_slots = threading.Semaphore(2)
share_tokens = {}                 # token -> {"id": quiz id, "expires": time}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def new_id(n=6):
    return secrets.token_hex(n)


def atomic_write(path, data):
    """Write to a temp file, flush it to disk, then swap it in: a crash never leaves half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# =====================================================================
# Quiz files
# =====================================================================

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
JAVA_NAME_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def text(v, limit):
    return str(v if v is not None else "")[:limit]


def num(v, default, lo, hi):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, x))


def clean_test(t, seen):
    t = t if isinstance(t, dict) else {}
    tid = str(t.get("id") or "")
    if not ID_RE.match(tid) or tid in seen:
        tid = new_id(4)
    seen.add(tid)
    kind = t.get("kind") if t.get("kind") in ("output", "code") else "output"
    compare = t.get("compare") if t.get("compare") in COMPARE_MODES else "lines"
    weight = num(t.get("weight"), 1, 0, 1000)
    return {"id": tid, "name": text(t.get("name"), 300), "kind": kind,
            "weight": int(weight) if weight == int(weight) else weight,
            "hidden": bool(t.get("hidden")), "timeout": num(t.get("timeout"), 2, 0.2, 60),
            "stdin": text(t.get("stdin"), 2_000_000), "expected": text(t.get("expected"), 2_000_000),
            "compare": compare, "code": text(t.get("code"), 200_000)}


def clean_image(im, seen):
    if not isinstance(im, dict):
        return None
    data = str(im.get("data") or "")
    if not re.match(r"^data:image/(png|jpeg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=\s]+$", data[:100] + data[-100:] if len(data) > 200 else data):
        return None
    iid = str(im.get("id") or "")
    if not ID_RE.match(iid) or iid in seen:
        iid = new_id(4)
    seen.add(iid)
    return {"id": iid, "name": text(im.get("name"), 200) or "image", "data": data}


def clean_question(q, seen):
    q = q if isinstance(q, dict) else {}
    qid = str(q.get("id") or "")
    if not ID_RE.match(qid) or qid in seen:
        qid = new_id(4)
    seen.add(qid)
    cls = str(q.get("className") or "Solution").strip()
    tseen, iseen = set(), set()
    images = [x for x in (clean_image(i, iseen) for i in (q.get("images") or [])) if x]
    return {"id": qid, "title": text(q.get("title"), 300) or "Untitled question",
            "minutes": num(q.get("minutes"), 5, 0.25, 600),
            "statement": text(q.get("statement"), 500_000),
            "images": images,
            "className": cls if JAVA_NAME_RE.match(cls) else "Solution",
            "starter": text(q.get("starter"), 200_000),
            "solution": text(q.get("solution"), 500_000),
            "tests": [clean_test(t, tseen) for t in (q.get("tests") or [])][:500]}


def clean_quiz(q):
    if not isinstance(q, dict):
        raise ValueError("not a quiz")
    qid = str(q.get("id") or "")
    seen = set()
    return {"format": FORMAT, "version": FORMAT_VERSION,
            "id": qid if ID_RE.match(qid) else new_id(),
            "title": text(q.get("title"), 300) or "Untitled quiz",
            "description": text(q.get("description"), 20_000),
            "provided": text(q.get("provided"), 500_000),
            "helpers": text(q.get("helpers"), 500_000),
            "questions": [clean_question(x, seen) for x in (q.get("questions") or [])][:200],
            "created": text(q.get("created"), 40) or now_iso(),
            "updated": text(q.get("updated"), 40) or now_iso(),
            "rev": int(num(q.get("rev"), 0, 0, 1e12))}


def checksum(quiz):
    body = {k: v for k, v in quiz.items() if k not in ("checksum", "rev", "exported", "updated")}
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def quiz_path(qid):
    if not ID_RE.match(qid or ""):
        raise ValueError("bad quiz id")
    return os.path.join(QUIZ_DIR, qid + ".quiz.json")


def load_quiz(qid):
    with store_lock:
        with open(quiz_path(qid), encoding="utf-8") as f:
            return json.load(f)


_list_cache = {}   # file name -> ((mtime, size), row)


def list_quizzes():
    """All quizzes in QUIZ_DIR (each file is only read again when it changed)."""
    out = []
    with store_lock:
        os.makedirs(QUIZ_DIR, exist_ok=True)
        names = [n for n in os.listdir(QUIZ_DIR) if n.endswith(".quiz.json")]
        for name in names:
            path = os.path.join(QUIZ_DIR, name)
            st = os.stat(path)
            key = (st.st_mtime, st.st_size)
            hit = _list_cache.get(name)
            if hit and hit[0] == key:
                out.append(hit[1])
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    q = json.load(f)
                row = {"id": q["id"], "title": q.get("title", ""), "updated": q.get("updated", ""),
                       "questions": len(q.get("questions", [])), "size": st.st_size}
            except Exception:
                row = {"id": name[:-10], "title": "(damaged file: " + name + ")", "updated": "", "questions": 0,
                       "size": st.st_size, "broken": True}
            _list_cache[name] = (key, row)
            out.append(row)
        for gone in set(_list_cache) - set(names):
            _list_cache.pop(gone, None)
    out.sort(key=lambda r: r["updated"], reverse=True)
    return [dict(r) for r in out]


def backup_dir(qid):
    return os.path.join(QUIZ_DIR, ".backups", qid)


def make_backup(qid, force=False):
    """Copy the current file of a quiz into .backups (at most every BACKUP_EVERY seconds unless forced)."""
    src = quiz_path(qid)
    if not os.path.exists(src):
        return
    d = backup_dir(qid)
    os.makedirs(d, exist_ok=True)
    names = sorted(n for n in os.listdir(d) if n.endswith(".quiz.json"))
    if names and not force and time.time() - os.path.getmtime(os.path.join(d, names[-1])) < BACKUP_EVERY:
        return
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    shutil.copy2(src, os.path.join(d, f"{stamp}.quiz.json"))
    names = sorted(n for n in os.listdir(d) if n.endswith(".quiz.json"))
    for old in names[:-BACKUPS_KEPT]:
        os.remove(os.path.join(d, old))


class Conflict(Exception):
    pass


def save_quiz(quiz, base_rev=None, force_backup=False):
    """Save a quiz. base_rev = the rev the editor started from; if the file changed meanwhile, raise Conflict."""
    with store_lock:
        quiz = clean_quiz(quiz)
        path = quiz_path(quiz["id"])
        if os.path.exists(path):
            try:
                cur = load_quiz(quiz["id"])
                cur_rev = cur.get("rev", 0)
            except Exception:
                cur_rev = None
            if base_rev is not None and cur_rev is not None and cur_rev != base_rev:
                raise Conflict(cur_rev)
            make_backup(quiz["id"], force=force_backup)
            quiz["rev"] = (cur_rev or 0) + 1
        else:
            quiz["rev"] = 1
        quiz["updated"] = now_iso()
        atomic_write(path, json.dumps(quiz, ensure_ascii=False, indent=1).encode("utf-8"))
        return quiz


def export_bytes(quiz):
    out = dict(quiz)
    out.pop("rev", None)
    out["exported"] = now_iso()
    out["checksum"] = checksum(quiz)
    return json.dumps(out, ensure_ascii=False, indent=1).encode("utf-8")


def slug(s):
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:60] or "quiz"


def import_quiz(raw, mode="ask"):
    """mode: ask (report if it exists already) | replace | copy."""
    try:
        data = json.loads(raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw)
    except Exception:
        raise ValueError("This is not a quiz file (or it is damaged: it isn't valid JSON).")
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("This is not a CodeQuiz quiz file.")
    if int(data.get("version") or 0) > FORMAT_VERSION:
        raise ValueError("This quiz was made with a newer CodeQuiz version. Update this copy of CodeQuiz first.")
    warning = None
    if data.get("checksum") and data["checksum"] != checksum(data):
        warning = "The file was changed after it was exported (by hand?) or it is damaged. Check the quiz carefully."
    quiz = clean_quiz(data)
    with store_lock:
        exists = os.path.exists(quiz_path(quiz["id"]))
        if exists:
            try:
                cur = load_quiz(quiz["id"])
            except Exception:
                cur = None
            if cur and checksum(cur) == checksum(quiz):
                return {"status": "same", "id": quiz["id"], "title": quiz["title"], "warning": warning}
            if mode == "ask":
                return {"status": "exists", "id": quiz["id"], "title": quiz["title"],
                        "current": {"title": (cur or {}).get("title", "?"), "updated": (cur or {}).get("updated", "?")},
                        "incoming": {"updated": data.get("updated", "?"), "exported": data.get("exported", "?")},
                        "warning": warning}
            if mode == "copy":
                quiz["id"] = new_id()
                quiz["title"] += " (copy)"
                saved = save_quiz(quiz)
            else:
                make_backup(quiz["id"], force=True)
                saved = save_quiz(quiz, base_rev=None)
        else:
            saved = save_quiz(quiz)
        return {"status": "imported", "id": saved["id"], "title": saved["title"], "warning": warning}


def delete_quiz(qid):
    with store_lock:
        src = quiz_path(qid)
        d = os.path.join(QUIZ_DIR, ".trash")
        os.makedirs(d, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        os.replace(src, os.path.join(d, f"{qid}__{stamp}.quiz.json"))


def list_trash():
    d = os.path.join(QUIZ_DIR, ".trash")
    out = []
    if os.path.isdir(d):
        for n in sorted(os.listdir(d), reverse=True):
            if not n.endswith(".quiz.json"):
                continue
            try:
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    q = json.load(f)
                out.append({"file": n, "title": q.get("title", "?"), "deleted": n.split("__")[-1][:-10].replace("_", " ")})
            except Exception:
                pass
    return out


def list_backups(qid):
    d = backup_dir(qid)
    out = []
    if os.path.isdir(d):
        for n in sorted(os.listdir(d), reverse=True):
            if n.endswith(".quiz.json"):
                p = os.path.join(d, n)
                out.append({"file": n, "when": n[:-10].replace("_", " "), "size": os.path.getsize(p)})
    return out


# =====================================================================
# Session state
# =====================================================================

def fresh_state():
    return {"players": {}, "phase": "lobby", "quiz": None, "q": None, "open": False,
            "opened_at": None, "deadline": None, "subs": {}, "opened": []}


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            os.replace(path, path + ".broken")
    return default


S = load_json(STATE_FILE, fresh_state())
for k, v in fresh_state().items():
    S.setdefault(k, v)
SQ = load_json(SESSION_QUIZ_FILE, None)   # the quiz being played (a frozen copy)


def save():
    atomic_write(STATE_FILE, json.dumps(S, indent=1).encode("utf-8"))


def session_question(qid):
    if not SQ or qid is None:
        return None, None
    for i, q in enumerate(SQ["questions"]):
        if q["id"] == qid:
            return i, q
    return None, None


def use_quiz(quiz):
    global SQ
    SQ = quiz
    atomic_write(SESSION_QUIZ_FILE, json.dumps(quiz, ensure_ascii=False).encode("utf-8"))
    S["quiz"] = {"id": quiz["id"], "title": quiz["title"], "rev": quiz.get("rev")}


def tick():
    if S["open"] and S["deadline"] and time.time() >= S["deadline"]:
        close_question()
        save()


def close_question():
    """Close submissions and start the tests for every submission of the current question."""
    S["open"] = False
    if S["q"] is None:
        return
    for pid, sub in S["subs"].get(S["q"], {}).items():
        if sub.get("tested_stamp") != sub["stamp"]:
            start_tests(S["q"], pid, sub)


def start_tests(qid, pid, sub):
    sub["tests"] = {"pending": True}
    sub["tested_stamp"] = sub["stamp"]
    threading.Thread(target=test_submission, args=(qid, pid, sub["code"], sub["stamp"]), daemon=True).start()


def totals():
    t = {pid: 0 for pid in S["players"]}
    for subs in S["subs"].values():
        for pid, s in subs.items():
            if pid in t and s.get("grade"):
                t[pid] += s["grade"]["points"]
    return t


def leaderboard():
    t = totals()
    last = {}
    if S["q"] is not None:
        for pid, s in S["subs"].get(S["q"], {}).items():
            if s.get("grade"):
                last[pid] = s["grade"]["points"]
    rows = [{"pid": pid, "name": p["name"], "points": t[pid], "last": last.get(pid, 0)}
            for pid, p in S["players"].items()]
    rows.sort(key=lambda r: (-r["points"], r["name"].lower()))
    rank, prev = 0, None
    for i, r in enumerate(rows):
        if r["points"] != prev:
            rank, prev = i + 1, r["points"]
        r["rank"] = rank
    return rows


def question_public(qid):
    """What students and the board may see of a question (no tests, no solution)."""
    i, q = session_question(qid)
    if q is None:
        return None
    return {"id": q["id"], "number": i + 1, "count": len(SQ["questions"]), "title": q["title"],
            "statement": q["statement"], "className": q["className"], "starter": q["starter"],
            "images": [{"id": im["id"], "name": im["name"], "url": f"/img/{q['id']}/{im['id']}"} for im in q["images"]],
            "provided": SQ.get("provided", ""), "description": SQ.get("description", "")}


def test_share(sub):
    t = sub.get("tests") or {}
    if t.get("pending") or not t.get("wtotal"):
        return None
    return t["wpassed"] / t["wtotal"]


def compute_points(verdict, sub):
    """correct: 1000 + speed bonus (up to 500)
       partial: weighted share of tests passed * (1000 + speed bonus); 500 + half bonus if no tests ran
       wrong: 0"""
    if verdict not in BASE or BASE[verdict] == 0:
        return 0
    limit = sub.get("limit") or 0
    speed = max(0.0, 1 - sub["t"] / limit) if limit > 0 else 0
    full = BASE["correct"] + MAX_BONUS * speed
    if verdict == "correct":
        return round(full)
    share = test_share(sub)
    if share is None:
        return round(BASE["partial"] + MAX_BONUS * speed / 2)
    return round(full * share)


def verdict_from_tests(sub):
    share = test_share(sub)
    if share is None:
        return None
    return "correct" if share == 1 else "wrong" if share == 0 else "partial"


# =====================================================================
# Java: preparing the source, compiling, running the tests
# =====================================================================

class SubmissionError(ValueError):
    pass


def blank(s):
    """Same length, newlines kept: removes text without moving line numbers."""
    return re.sub(r"[^\n]", " ", s)


def mask_literals(code):
    """Replace comments, strings and char literals by spaces (same length), to find structure safely."""
    pattern = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|//[^\n]*|/\*[\s\S]*?\*/')
    return pattern.sub(lambda m: blank(m.group(0)), code)


TYPE_RE = re.compile(r"\b(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")


def top_level_types(code):
    """[(name, decl_start, keyword_start, end)] for each top-level type."""
    m = mask_literals(code)
    out, depth, seg, i, n = [], 0, 0, 0, len(m)
    while i < n:
        c = m[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                seg = i + 1
        elif c == ";" and depth == 0:
            seg = i + 1
        elif depth == 0 and c.isalpha():
            t = TYPE_RE.match(m, i)
            if t and (i == 0 or not (m[i - 1].isalnum() or m[i - 1] in "_$.@")):
                j = m.find("{", t.end())
                if j < 0:
                    break
                d, k = 0, j
                while k < n:
                    if m[k] == "{":
                        d += 1
                    elif m[k] == "}":
                        d -= 1
                        if d == 0:
                            break
                    k += 1
                out.append((t.group(2), seg, i, k + 1))
                i = seg = k + 1
                continue
            while i < n and (m[i].isalnum() or m[i] in "_$"):
                i += 1
            continue
        i += 1
    return out


IMPORT_RE = re.compile(r"(?m)^[ \t]*(import[ \t]+(?:static[ \t]+)?[\w.]+(?:\.\*)?[ \t]*;)")
PACKAGE_RE = re.compile(r"(?m)^[ \t]*package[ \t]+[\w.]+[ \t]*;")


def take_imports(code):
    """Return (code with import/package lines blanked, list of imports)."""
    masked = mask_literals(code)
    imports, chars = [], list(code)
    for rx in (IMPORT_RE, PACKAGE_RE):
        for m in rx.finditer(masked):
            if rx is IMPORT_RE:
                imports.append(re.sub(r"\s+", " ", m.group(1)))
            for k in range(m.start(), m.end()):
                if chars[k] != "\n":
                    chars[k] = " "
    return "".join(chars), imports


def strip_public(code, types):
    """Top-level types can't be public when several share one file."""
    chars = list(code)
    masked = mask_literals(code)
    for _, start, kw, _ in types:
        for m in re.finditer(r"\bpublic\b", masked[start:kw]):
            for k in range(start + m.start(), start + m.end()):
                chars[k] = " "
    return "".join(chars)


def prepare_source(quiz, question, code):
    """Submission.java: the student's code (line numbers unchanged) + the quiz's provided code.
    A provided class that the student also wrote is replaced by the student's version."""
    if len(code) > MAX_CODE:
        raise SubmissionError("Code too long.")
    body, imports = take_imports(code)
    types = top_level_types(body)
    body = strip_public(body, types)
    names = {t[0] for t in types}
    wrap = not types
    prov, prov_imports = take_imports(quiz.get("provided") or "")
    ptypes = top_level_types(prov)
    prov = strip_public(prov, ptypes)
    for name, start, _, end in sorted(ptypes, key=lambda t: -t[1]):
        if name in names:
            prov = prov[:start] + blank(prov[start:end]) + prov[end:]
    all_imports = list(dict.fromkeys(imports + prov_imports))
    first = " ".join(all_imports) + (f" class {question['className']} {{ " if wrap else "")
    src = first + body + ("\n}\n" if wrap else "\n")
    if prov.strip():
        src += "\n// ----- provided code -----\n" + prov + "\n"
    return src


def javac(d, files):
    r = subprocess.run(["javac", "-proc:none", "-nowarn", "-encoding", "UTF-8", "-cp", RUNNER_BUILD, "-d", d] + files,
                       capture_output=True, text=True, timeout=90, cwd=d)
    return r.returncode == 0, (r.stdout + r.stderr).replace(d + os.sep, "").strip()


def errors_by_file(out):
    """javac output -> {file name: [error lines]}"""
    res, cur = {}, None
    for line in out.splitlines():
        m = re.match(r"^(?:.*[\\/])?([\w$]+\.java):(\d+): error: (.*)", line)
        if m:
            name, ln = m.group(1), int(m.group(2))
            if name.startswith("CQ_T"):
                ln -= 1          # line 1 of a generated test file is our header
            cur = res.setdefault(name, [])
            cur.append(f"line {ln}: {m.group(3)}")
        elif cur is not None and re.match(r"^\s+(symbol|location|required|found|reason):", line):
            cur.append("  " + line.strip())
    return res


def compile_check(quiz, question, code):
    """javac only (nothing is run). Returns {"ok": bool|None, "output": str}."""
    with compile_slots:
        d = tempfile.mkdtemp(prefix="cq_c_")
        try:
            src = os.path.join(d, "Submission.java")
            with open(src, "w", encoding="utf-8") as f:
                f.write(prepare_source(quiz, question, code))
            ok, out = javac(d, [src])
            return {"ok": ok, "output": out[:4000]}
        except SubmissionError as e:
            return {"ok": False, "output": str(e)}
        except subprocess.TimeoutExpired:
            return {"ok": False, "output": "javac timed out"}
        except FileNotFoundError:
            return {"ok": None, "output": "javac not found on the host laptop"}
        finally:
            shutil.rmtree(d, ignore_errors=True)


def norm_nl(s):
    return s.replace("\r\n", "\n").replace("\r", "\n")


NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def compare_output(mode, got, want):
    got, want = norm_nl(got), norm_nl(want)
    if mode == "exact":
        return got == want
    if mode == "tokens":
        return got.split() == want.split()
    if mode == "numbers":
        a, b = NUM_RE.findall(got), NUM_RE.findall(want)
        if len(a) != len(b):
            return False
        for x, y in zip(a, b):
            fx, fy = float(x), float(y)
            if not (fx == fy or abs(fx - fy) <= 1e-6 * max(1.0, abs(fy))):
                return False
        return True
    lines_a = [l.rstrip() for l in got.split("\n")]
    lines_b = [l.rstrip() for l in want.split("\n")]
    while lines_a and not lines_a[-1]:
        lines_a.pop()
    while lines_b and not lines_b[-1]:
        lines_b.pop()
    if mode == "anyorder":
        return sorted(l.strip() for l in lines_a if l.strip()) == sorted(l.strip() for l in lines_b if l.strip())
    return lines_a == lines_b


def cut(s, n=700):
    return s if len(s) <= n else s[:n] + f"\n... ({len(s)} characters)"


def run_tests(quiz, question, code, keep_output=False):
    """Compile the code with all tests of the question and run them.
    Returns {passed, total, wpassed, wtotal, results: [...], error}."""
    tests = question["tests"]

    def summary(results, error=None):
        return {"passed": sum(r["ok"] for r in results), "total": len(results),
                "wpassed": sum(r["weight"] for r in results if r["ok"]),
                "wtotal": sum(r["weight"] for r in results), "results": results, "error": error}

    def result(i, ok, status, detail, output=None):
        t = tests[i]
        r = {"id": t["id"], "name": t["name"] or f"Test {i + 1}", "kind": t["kind"], "weight": t["weight"],
             "hidden": t["hidden"], "ok": ok, "status": status, "detail": detail}
        if keep_output and output is not None:
            r["output"] = output
        return r

    def all_fail(msg):
        short = msg.split("\n")[0]
        return summary([result(i, False, "error", short) for i in range(len(tests))], msg)

    if not tests:
        return summary([], "This question has no tests.")
    if not os.path.exists(os.path.join(RUNNER_BUILD, "QuizRunner.class")):
        return all_fail("The test runner isn't built (is a JDK installed?). Restart the server.")
    with compile_slots:
        d = tempfile.mkdtemp(prefix="cq_t_")
        try:
            try:
                src = prepare_source(quiz, question, code)
            except SubmissionError as e:
                return all_fail(str(e))
            files = {"Submission.java": src}
            body, imports = take_imports(code)
            prov_imports = take_imports(quiz.get("provided") or "")[1]
            helpers = quiz.get("helpers") or ""
            hbody, himports = take_imports(helpers)
            if helpers.strip():
                files["CQHelpers.java"] = " ".join(himports) + " " + strip_public(hbody, top_level_types(hbody))
            head = " ".join(dict.fromkeys(["import java.util.*;", "import java.util.function.*;"] + imports + prov_imports + himports))
            code_tests = {}
            for i, t in enumerate(tests):
                if t["kind"] == "code":
                    name = f"CQ_T{i}"
                    files[name + ".java"] = (f"{head} class {name} extends CQ {{ static void run() throws Throwable {{\n"
                                             f"{t['code']}\n}} }}\n")
                    code_tests[name + ".java"] = i
                else:
                    with open(os.path.join(d, f"in_{i}.txt"), "w", encoding="utf-8", newline="") as f:
                        f.write(norm_nl(t["stdin"]))
            for name, content in files.items():
                with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                    f.write(content)
            # compile; tests that don't compile against this code fail alone
            broken = {}
            pending = list(files)
            for _ in range(6):
                ok, out = javac(d, pending)
                if ok:
                    break
                errs = errors_by_file(out)
                if "Submission.java" in errs or not errs:
                    return all_fail("Doesn't compile:\n" + cut(out, 1500))
                if "CQHelpers.java" in errs:
                    return all_fail("Compiles alone, but not together with the test helpers (did you change a given class?)\n"
                                    + cut(out, 1500))
                for f, e in errs.items():
                    if f in code_tests:
                        broken[code_tests[f]] = "This test doesn't compile with your code (wrong method name or types?):\n" + "\n".join(e[:6])
                        pending.remove(f)
                        os.remove(os.path.join(d, f))
            else:
                return all_fail("Doesn't compile:\n" + cut(out, 1500))
            # run, again and again if the program kills the JVM (System.exit)
            todo = [i for i in range(len(tests)) if i not in broken]
            raw = {}
            for _ in range(len(todo) + 1):
                if not todo:
                    break
                manifest = os.path.join(d, "manifest.txt")
                with open(manifest, "w", encoding="utf-8") as f:
                    for i in todo:
                        t = tests[i]
                        cls = f"CQ_T{i}" if t["kind"] == "code" else question["className"]
                        stdin = "" if t["kind"] == "code" else f"in_{i}.txt"
                        f.write(f"{t['kind']}\t{i}\t{cls}\t{stdin}\t{int(t['timeout'] * 1000)}\n")
                limit = sum(tests[i]["timeout"] for i in todo) + 30
                try:
                    r = subprocess.run(["java", "-Xmx512m", "-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8",
                                        "-cp", RUNNER_BUILD, "QuizRunner", d, manifest],
                                       capture_output=True, timeout=limit, cwd=d)
                    stdout = r.stdout.decode("utf-8", "replace")
                except subprocess.TimeoutExpired as e:
                    stdout = (e.stdout or b"").decode("utf-8", "replace")
                started = None
                for line in stdout.splitlines():
                    p = line.split("\t")
                    if p[0] == "##START":
                        started = int(p[1])
                    elif p[0] == "##R" and len(p) >= 5:
                        dec = lambda s: base64.b64decode(s).decode("utf-8", "replace")
                        raw[int(p[1])] = (p[2], dec(p[3]), dec(p[4]))
                        started = None
                if started is not None and started not in raw:
                    raw[started] = ("error", "", "The program stopped the whole test run (System.exit?) or crashed.")
                todo = [i for i in todo if i not in raw]
            results = []
            for i, t in enumerate(tests):
                if i in broken:
                    results.append(result(i, False, "error", broken[i]))
                    continue
                status, printed, msg = raw.get(i, ("error", "", "The test did not run."))
                if t["kind"] == "code":
                    ok = status == "ok"
                    detail = "" if ok else msg + (f"\nprinted:\n{cut(printed, 400)}" if printed.strip() and status != "fail" else "")
                    results.append(result(i, ok, status, detail, printed))
                else:
                    ok = status == "ok" and compare_output(t["compare"], printed, t["expected"])
                    if ok:
                        detail = ""
                    elif status != "ok":
                        detail = msg + (f"\nprinted so far:\n{cut(printed, 400)}" if printed.strip() else "")
                    else:
                        detail = (f"input:\n{cut(t['stdin'], 300) or '(none)'}\nexpected:\n{cut(t['expected'], 500)}"
                                  f"\ngot:\n{cut(printed, 500) or '(nothing)'}")
                    results.append(result(i, ok, status, detail, printed))
            return summary(results)
        except subprocess.TimeoutExpired:
            return all_fail("Compiling took too long.")
        except FileNotFoundError:
            return all_fail("java/javac not found on the host laptop.")
        finally:
            shutil.rmtree(d, ignore_errors=True)


def submit_compile(qid, pid, code, stamp):
    _, q = session_question(qid)
    res = compile_check(SQ, q, code) if q else {"ok": None, "output": "question not found"}
    with lock:
        sub = S["subs"].get(qid, {}).get(pid)
        if sub and sub["stamp"] == stamp:
            sub["compile"] = res
            save()


def test_submission(qid, pid, code, stamp):
    quiz = SQ
    _, q = session_question(qid)
    res = run_tests(quiz, q, code) if q else {"passed": 0, "total": 0, "results": [], "error": "question not found"}
    with lock:
        sub = S["subs"].get(qid, {}).get(pid)
        if sub and sub["stamp"] == stamp:
            sub["tests"] = res
            save()


def ensure_runner():
    srcs = [os.path.join(RUNNER_SRC, f) for f in ("QuizRunner.java", "CQ.java")]
    target = os.path.join(RUNNER_BUILD, "QuizRunner.class")
    if os.path.exists(target) and all(os.path.getmtime(target) >= os.path.getmtime(s) for s in srcs):
        return True
    os.makedirs(RUNNER_BUILD, exist_ok=True)
    try:
        r = subprocess.run(["javac", "-encoding", "UTF-8", "-nowarn", "-d", RUNNER_BUILD] + srcs,
                           capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        print("WARNING: javac not found. Install a JDK, otherwise code can't be compiled or tested.", flush=True)
        return False
    if r.returncode != 0:
        print("WARNING: could not build the test runner:\n" + r.stdout + r.stderr, flush=True)
        return False
    return True


# =====================================================================
# HTTP
# =====================================================================

def static_file(name):
    with open(os.path.join(HERE, "static", name), "rb") as f:
        return f.read()


CTYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def send(self, code, body, ctype="application/json", headers=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/json")) else ""))
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        if not headers or "Cache-Control" not in headers:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def raw_body(self, limit):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            self.rfile.read(n)
            return None
        return self.rfile.read(n)

    def json_body(self, limit):
        raw = self.raw_body(limit)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return None

    def forbid(self):
        return self.send(403, {"error": "Only on the host laptop (open it via localhost)."})

    # ----- routing -----
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                return self.send(200, static_file("student.html"), "text/html")
            if path == "/common.js":
                return self.send(200, static_file("common.js"), "text/javascript")
            if path == "/board":
                return self.send(200, static_file("board.html"), "text/html")
            if path == "/api/state":
                return self.send(200, self.student_state((qs.get("pid") or [""])[0]))
            if path == "/api/board":
                return self.send(200, self.board_state())
            if path.startswith("/img/"):
                return self.image(path)
            if path.startswith("/share/"):
                return self.share_download(path[7:])
            if path in ("/host", "/editor"):
                if not self.is_local():
                    return self.send(403, b"This page only works on the host laptop (open it via localhost).", "text/plain")
                return self.send(200, static_file(path[1:] + ".html"), "text/html")
            if path.startswith("/api/"):
                if not self.is_local():
                    return self.forbid()
                return self.local_get(path, qs)
            self.send(404, {"error": "not found"})
        except BrokenPipeError:
            pass

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        if path in ("/api/join", "/api/submit"):
            data = self.json_body(MAX_STUDENT_BODY)
            if data is None:
                return self.send(400, {"error": "bad request"})
            return self.join(data) if path == "/api/join" else self.submit(data)
        if not self.is_local():
            self.raw_body(MAX_STUDENT_BODY)
            return self.forbid()
        if path == "/api/import":
            raw = self.raw_body(MAX_LOCAL_BODY)
            if raw is None:
                return self.send(413, {"error": "File too big."})
            mode = (parse_qs(u.query).get("mode") or ["ask"])[0]
            try:
                return self.send(200, import_quiz(raw, mode))
            except ValueError as e:
                return self.send(400, {"error": str(e)})
        data = self.json_body(MAX_LOCAL_BODY)
        if data is None:
            return self.send(400, {"error": "bad request"})
        if path == "/api/host":
            return self.host_action(data)
        return self.local_post(path, data)

    def do_PUT(self):
        u = urlparse(self.path)
        if not self.is_local():
            self.raw_body(MAX_STUDENT_BODY)
            return self.forbid()
        m = re.match(r"^/api/quiz/([\w-]+)$", u.path)
        data = self.json_body(MAX_LOCAL_BODY)
        if not m or data is None:
            return self.send(400, {"error": "bad request"})
        quiz = data.get("quiz") or {}
        quiz["id"] = m.group(1)
        try:
            saved = save_quiz(quiz, base_rev=data.get("base_rev"))
        except Conflict as c:
            return self.send(409, {"error": "This quiz was changed somewhere else (another window or an import).",
                                   "rev": c.args[0]})
        except (ValueError, OSError) as e:
            return self.send(500, {"error": "Could not save: " + str(e)})
        return self.send(200, {"ok": True, "rev": saved["rev"], "updated": saved["updated"]})

    # ----- images & sharing -----
    def image(self, path):
        m = re.match(r"^/img/([\w-]+)/([\w-]+)$", path)
        if not m:
            return self.send(404, {"error": "not found"})
        qid, iid = m.groups()
        with lock:
            allowed = self.is_local() or qid in S["opened"]
            _, q = session_question(qid)
        if not allowed or not q:
            return self.send(404, {"error": "not found"})
        for im in q["images"]:
            if im["id"] == iid:
                head, b64 = im["data"].split(",", 1)
                ctype = head[5:].split(";")[0]
                return self.send(200, base64.b64decode(b64), ctype, {"Cache-Control": "private, max-age=3600",
                                                                       "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
        self.send(404, {"error": "not found"})

    def share_download(self, token):
        info = share_tokens.get(token)
        if not info or info["expires"] < time.time():
            return self.send(404, b"This share link is unknown or has expired. Make a new one in the editor.", "text/plain")
        try:
            quiz = load_quiz(info["id"])
        except Exception:
            return self.send(404, b"The quiz no longer exists.", "text/plain")
        name = slug(quiz["title"]) + ".quiz.json"
        return self.send(200, export_bytes(quiz), "application/json",
                         {"Content-Disposition": f'attachment; filename="{name}"'})

    # ----- editor API (local only) -----
    def local_get(self, path, qs):
        if path == "/api/host":
            return self.send(200, self.host_state())
        if path == "/api/quizzes":
            return self.send(200, {"quizzes": list_quizzes(), "folder": QUIZ_DIR, "trash": list_trash()})
        m = re.match(r"^/api/quiz/([\w-]+)(/export|/backups|/backup)?$", path)
        if m:
            qid, sub = m.groups()
            try:
                if sub == "/backups":
                    return self.send(200, {"backups": list_backups(qid)})
                if sub == "/backup":
                    name = os.path.basename((qs.get("file") or [""])[0])
                    with open(os.path.join(backup_dir(qid), name), encoding="utf-8") as f:
                        return self.send(200, json.load(f))
                quiz = load_quiz(qid)
            except (OSError, ValueError):
                return self.send(404, {"error": "Quiz not found."})
            if sub == "/export":
                name = slug(quiz["title"]) + ".quiz.json"
                return self.send(200, export_bytes(quiz), "application/json",
                                 {"Content-Disposition": f'attachment; filename="{name}"'})
            return self.send(200, quiz)
        self.send(404, {"error": "not found"})

    def local_post(self, path, data):
        try:
            if path == "/api/quiz":
                quiz = save_quiz({"title": data.get("title") or "New quiz", "questions": [{"title": "Question 1"}]})
                return self.send(200, quiz)
            if path == "/api/import_url":
                url = str(data.get("url") or "").strip()
                if not re.match(r"^https?://[^/\s]+/share/[A-Za-z0-9_-]+$", url):
                    return self.send(400, {"error": "That doesn't look like a share link (http://.../share/...)."})
                try:
                    with urllib.request.urlopen(url, timeout=30) as r:
                        raw = r.read(MAX_LOCAL_BODY + 1)
                except Exception as e:
                    return self.send(400, {"error": f"Could not download it: {e}"})
                return self.send(200, import_quiz(raw, data.get("mode") or "ask"))
            if path == "/api/trash/restore":
                name = os.path.basename(str(data.get("file") or ""))
                with open(os.path.join(QUIZ_DIR, ".trash", name), "rb") as f:
                    res = import_quiz(f.read(), "copy" if data.get("copy") else "ask")
                if res["status"] in ("imported", "same"):
                    os.remove(os.path.join(QUIZ_DIR, ".trash", name))
                return self.send(200, res)
            if path == "/api/check":
                quiz = clean_quiz(data.get("quiz") or {})
                q = next((x for x in quiz["questions"] if x["id"] == data.get("qid")), None)
                if not q:
                    return self.send(404, {"error": "Question not found."})
                code = q["solution"] if data.get("which") == "solution" else q["starter"] if data.get("which") == "starter" else str(data.get("code") or "")
                if not code.strip():
                    return self.send(400, {"error": "There is no code to test (write a reference solution first)."})
                res = run_tests(quiz, q, code, keep_output=True)
                res["compile"] = compile_check(quiz, q, code)
                return self.send(200, res)
            m = re.match(r"^/api/quiz/([\w-]+)/(delete|duplicate|share|restore)$", path)
            if m:
                qid, what = m.groups()
                if what == "delete":
                    delete_quiz(qid)
                    return self.send(200, {"ok": True})
                if what == "duplicate":
                    quiz = load_quiz(qid)
                    quiz["id"] = new_id()
                    quiz["title"] += " (copy)"
                    quiz["created"] = now_iso()
                    return self.send(200, save_quiz(quiz))
                if what == "share":
                    load_quiz(qid)
                    token = secrets.token_urlsafe(12)
                    share_tokens[token] = {"id": qid, "expires": time.time() + 30 * 60}
                    return self.send(200, {"url": f"{ADDRESS}/share/{token}", "minutes": 30})
                if what == "restore":
                    name = os.path.basename(str(data.get("file") or ""))
                    with open(os.path.join(backup_dir(qid), name), encoding="utf-8") as f:
                        old = json.load(f)
                    old["id"] = qid
                    make_backup(qid, force=True)
                    saved = save_quiz(old, base_rev=None)
                    return self.send(200, {"ok": True, "rev": saved["rev"]})
        except (OSError, ValueError) as e:
            return self.send(400, {"error": str(e)})
        self.send(404, {"error": "not found"})

    # ----- student -----
    def join(self, data):
        name = re.sub(r"\s+", " ", str(data.get("name", ""))).strip()[:30]
        pid = str(data.get("pid") or "")
        if not name:
            return self.send(400, {"error": "Please enter a name."})
        with lock:
            for other, p in S["players"].items():
                if p["name"].lower() == name.lower() and other != pid:
                    return self.send(409, {"error": "That name is taken. Pick another one."})
            if pid not in S["players"]:
                pid = secrets.token_hex(8)
                S["players"][pid] = {"name": name, "joined": time.time()}
            else:
                S["players"][pid]["name"] = name
            save()
        self.send(200, {"pid": pid, "name": name})

    def submit(self, data):
        pid = str(data.get("pid") or "")
        code = str(data.get("code") or "")
        if len(code) > MAX_CODE:
            return self.send(400, {"error": "Code too long."})
        if not code.strip():
            return self.send(400, {"error": "Your code is empty."})
        with lock:
            tick()
            if pid not in S["players"]:
                return self.send(403, {"error": "Please join again."})
            if S["phase"] != "question" or not S["open"] or session_question(S["q"])[1] is None:
                return self.send(409, {"error": "Submissions are closed."})
            qid = S["q"]
            subs = S["subs"].setdefault(qid, {})
            old = subs.get(pid)
            if old and old.get("grade"):
                return self.send(409, {"error": "Already graded, can't resubmit."})
            stamp = secrets.token_hex(4)
            t = time.time()
            subs[pid] = {"code": code, "at": t, "t": round(t - S["opened_at"], 1),
                         "limit": (S["deadline"] - S["opened_at"]) if S["deadline"] else None,
                         "count": (old or {}).get("count", 0) + 1,
                         "stamp": stamp, "compile": None, "grade": None}
            save()
        threading.Thread(target=submit_compile, args=(qid, pid, code, stamp), daemon=True).start()
        self.send(200, {"ok": True})

    def student_tests(self, t):
        """Test results as students see them (after the question closed): hidden tests without details."""
        if not t or t.get("pending"):
            return t
        res = []
        for r in t.get("results", []):
            res.append({"name": "Hidden test" if r["hidden"] else r["name"], "ok": r["ok"],
                        "detail": "" if r["hidden"] else r["detail"], "hidden": r["hidden"]})
        return {**{k: t.get(k) for k in ("passed", "total", "wpassed", "wtotal")}, "results": res,
                "error": t.get("error")}

    def student_state(self, pid):
        with lock:
            tick()
            me = S["players"].get(pid)
            out = {"now": time.time(), "phase": S["phase"], "open": S["open"], "deadline": S["deadline"],
                   "known": me is not None, "quiz": SQ["title"] if SQ else ""}
            if me:
                out["name"] = me["name"]
            q = question_public(S["q"]) if S["phase"] in ("question", "leaderboard") else None
            if q:
                out["question"] = q
                sub = S["subs"].get(S["q"], {}).get(pid)
                if sub:
                    out["mine"] = {"code": sub["code"], "t": sub["t"], "count": sub["count"],
                                   "compile": sub["compile"], "grade": sub["grade"],
                                   "tests": self.student_tests(sub.get("tests")) if not S["open"] else None}
            lb = leaderboard()
            mine = next((r for r in lb if r["pid"] == pid), None)
            if mine:
                out["me"] = {"rank": mine["rank"], "points": mine["points"], "of": len(lb)}
            if S["phase"] == "leaderboard":
                out["leaderboard"] = [{k: r[k] for k in ("name", "points", "rank", "last")} for r in lb[:10]]
            return out

    # ----- board -----
    def board_state(self):
        with lock:
            tick()
            subs = S["subs"].get(S["q"], {}) if S["q"] is not None else {}
            return {"now": time.time(), "phase": S["phase"], "open": S["open"], "deadline": S["deadline"],
                    "question": question_public(S["q"]), "quiz": SQ["title"] if SQ else "",
                    "players": len(S["players"]), "submitted": len(subs),
                    "names": sorted((p["name"] for p in S["players"].values()), key=str.lower),
                    "leaderboard": [{k: r[k] for k in ("name", "points", "rank", "last")} for r in leaderboard()],
                    "address": ADDRESS}

    # ----- host -----
    def host_state(self):
        with lock:
            tick()
            subs = {}
            for qk, d in S["subs"].items():
                subs[qk] = {pid: {k: v for k, v in s.items() if k != "stamp"} for pid, s in d.items()}
            questions = []
            for q in (SQ or {}).get("questions", []):
                questions.append({"id": q["id"], "title": q["title"], "minutes": q["minutes"],
                                  "tests": len(q["tests"]), "weights": sum(t["weight"] for t in q["tests"]),
                                  "hasSolution": bool(q["solution"].strip())})
            return {"now": time.time(), "phase": S["phase"], "q": S["q"], "open": S["open"],
                    "opened_at": S["opened_at"], "deadline": S["deadline"], "quiz": S["quiz"],
                    "players": S["players"], "subs": subs, "questions": questions,
                    "leaderboard": leaderboard(), "address": ADDRESS, "quizzes": list_quizzes()}

    def host_action(self, d):
        a = d.get("action")
        with lock:
            tick()
            if a == "use_quiz":
                try:
                    quiz = clean_quiz(load_quiz(str(d.get("id") or "")))
                except (OSError, ValueError):
                    return self.send(404, {"error": "Quiz not found."})
                if S["open"]:
                    close_question()
                use_quiz(quiz)
                if not (S["quiz"] and session_question(S["q"])[1]):
                    S["q"], S["phase"] = None, "lobby"
            elif a == "open":
                qid = str(d.get("q"))
                _, q = session_question(qid)
                if not q:
                    return self.send(404, {"error": "Question not found."})
                if S["open"]:
                    close_question()  # starting a new question closes (and tests) the old one
                minutes = num(d.get("minutes"), q["minutes"], 0.1, 600)
                S["phase"], S["q"], S["open"] = "question", qid, True
                S["opened_at"] = time.time()
                S["deadline"] = S["opened_at"] + minutes * 60
                S["subs"].setdefault(qid, {})
                if qid not in S["opened"]:
                    S["opened"].append(qid)
            elif a == "close":
                S["deadline"] = min(S["deadline"] or time.time(), time.time())
                close_question()
            elif a == "retest":
                sub = S["subs"].get(str(d.get("q")), {}).get(d.get("pid"))
                if sub and not (sub.get("tests") or {}).get("pending"):
                    start_tests(str(d["q"]), d["pid"], sub)
            elif a == "extend":
                add = num(d.get("minutes"), 2, 0.1, 120) * 60
                if S["phase"] == "question" and S["q"] is not None:
                    S["deadline"] = max(S["deadline"] or 0, time.time()) + add
                    S["open"] = True
            elif a == "leaderboard":
                S["phase"] = "leaderboard"
            elif a == "show_question":
                if S["q"] is not None:
                    S["phase"] = "question"
            elif a == "lobby":
                S["phase"] = "lobby"
            elif a == "grade":
                sub = S["subs"].get(str(d.get("q")), {}).get(d.get("pid"))
                if not sub:
                    return self.send(404, {"error": "no submission"})
                verdict = d.get("verdict")
                if verdict is None:
                    sub["grade"] = None
                else:
                    try:
                        pts = int(float(d["points"])) if verdict == "custom" else compute_points(verdict, sub)
                    except (KeyError, ValueError):
                        return self.send(400, {"error": "points?"})
                    sub["grade"] = {"verdict": verdict, "points": pts, "note": str(d.get("note") or "")[:500]}
            elif a == "grade_all_tests":
                n = 0
                for sub in S["subs"].get(str(d.get("q")), {}).values():
                    v = verdict_from_tests(sub)
                    if v and not sub.get("grade"):
                        sub["grade"] = {"verdict": v, "points": compute_points(v, sub), "note": ""}
                        n += 1
                save()
                return self.send(200, {"ok": True, "graded": n})
            elif a == "kick":
                S["players"].pop(d.get("pid"), None)
                for subs in S["subs"].values():
                    subs.pop(d.get("pid"), None)
            elif a == "reset":
                quiz = S["quiz"]
                S.clear()
                S.update(fresh_state())
                S["quiz"] = quiz
            else:
                return self.send(400, {"error": "unknown action"})
            save()
        self.send(200, {"ok": True})


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


ADDRESS = ""

if __name__ == "__main__":
    args = sys.argv[1:]
    port = 8000
    while args:
        a = args.pop(0)
        if a == "--quizzes" and args:
            QUIZ_DIR = os.path.abspath(args.pop(0))
        elif a.isdigit():
            port = int(a)
        else:
            sys.exit(__doc__)
    os.makedirs(QUIZ_DIR, exist_ok=True)
    ensure_runner()
    ADDRESS = f"http://{lan_ip()}:{port}"
    print(f"Students: {ADDRESS}", flush=True)
    print(f"Host:     http://localhost:{port}/host", flush=True)
    print(f"Editor:   http://localhost:{port}/editor", flush=True)
    print(f"Board:    http://localhost:{port}/board", flush=True)
    print(f"Quizzes:  {QUIZ_DIR}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
