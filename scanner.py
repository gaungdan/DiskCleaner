"""
scanner.py - 磁盘垃圾扫描引擎

两种引擎：
  1) Everything CLI (es.exe)  —— 快，走 Everything 索引，秒级返回
  2) Python os.walk           —— 回退方案，不依赖任何外部程序

两者返回相同的数据结构，由 signatures.json 规则库给每个文件打上
「哪个软件的 / 什么包 / 风险等级 / 建议」标签。
"""
from __future__ import annotations

import csv
import fnmatch
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


# ────────────────────────────────────────────────────────────
# 数据模型
# ────────────────────────────────────────────────────────────

@dataclass
class Hit:
    """一个命中的垃圾文件。"""
    path: str
    size: int
    software: str
    category: str
    risk: str            # safe | medium | danger
    confidence: int      # 0-100
    rule_id: str
    advice: str
    reclaimable: bool    # 是否建议清理（danger / user_data 为 False）

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def size_mb(self) -> float:
        return self.size / 1048576


@dataclass
class ScanResult:
    hits: list[Hit] = field(default_factory=list)
    engine: str = ""
    drive: str = ""
    duration: float = 0.0
    files_examined: int = 0
    error: str = ""
    truncated: bool = False

    @property
    def total_size(self) -> int:
        return sum(h.size for h in self.hits)

    @property
    def reclaimable_size(self) -> int:
        return sum(h.size for h in self.hits if h.reclaimable)


# ────────────────────────────────────────────────────────────
# 工具函数
# ────────────────────────────────────────────────────────────

def human_size(n: int) -> str:
    for unit, div in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"


def load_signatures(path: Path | None = None) -> dict:
    import json
    p = path or (APP_DIR / "signatures.json")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def expand(p: str) -> str:
    """展开环境变量占位符。"""
    return os.path.expandvars(p)


def norm_drive(drive: str) -> str:
    """把 'C' / 'c:' / 'C:\\' 统一成 'C:'。"""
    d = (drive or "").strip().rstrip("\\")
    if d.endswith(":"):
        d = d[:-1]
    return (d[:1].upper() + ":") if d else ""


# ────────────────────────────────────────────────────────────
# 通配符匹配（* 不跨目录分隔符，** 跨）
# ────────────────────────────────────────────────────────────

def compile_glob(pattern: str):
    """把 glob 编译成正则；* 不跨 \\ ，** 跨。返回 (regex, literal_prefix)。"""
    pattern = pattern.replace("/", "\\")
    literal_prefix = ""
    m = re.match(r"^(.*?)[*?]", pattern)
    if m:
        literal_prefix = m.group(1)
        if "\\" in literal_prefix:
            literal_prefix = literal_prefix[: literal_prefix.rfind("\\") + 1]
        else:
            literal_prefix = ""
    else:
        literal_prefix = pattern

    out = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        # 目录分隔符 + 通配符，必须整体处理：
        #   re.escape('\\*') 会退化成「同层文件名」，无法跨子目录
        if c == "\\" and i + 1 < n and pattern[i + 1] in "*?":
            nxt = pattern[i + 1]
            at_end = (i + 2 >= n)
            if nxt == "*" and i + 2 < n and pattern[i + 2] == "*":
                out.append("\\\\.*")                  # '\**' -> 该目录及以下全部
                i += 3
                if i < n and pattern[i] == "\\":
                    i += 1
            elif at_end and nxt == "*":
                out.append("\\\\" + ".*")             # 结尾 '\*' -> 该目录及其下任意层级
                i += 2
            else:
                out.append("\\\\" + ".*")             # '\*.exe' / '\?\?' -> 任意层级
                i += 2
            continue
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
                continue
            out.append("[^\\\\]*")
            i += 1
        elif c == "?":
            out.append("[^\\\\]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE), literal_prefix


class Rule:
    __slots__ = ("id", "software", "category", "risk", "confidence", "raw_paths",
                 "patterns", "prefixes", "min_size", "max_size", "advice",
                 "reclaimable", "name_re")

    def __init__(self, raw: dict):
        self.id = raw.get("id", "?")
        self.software = raw.get("software", "未知")
        self.category = raw.get("category", "app_cache")
        self.risk = raw.get("risk", "medium")
        self.confidence = int(raw.get("confidence", 50))
        self.raw_paths = raw.get("path", [])
        self.min_size = int(raw.get("min_size", 0))
        self.max_size = int(raw.get("max_size", 0)) or None
        self.advice = raw.get("advice", "")
        self.reclaimable = bool(raw.get("reclaimable", self.risk == "safe"))

        pat = raw.get("name_regex")
        self.name_re = re.compile(pat, re.IGNORECASE) if pat else None

        self.patterns = []
        self.prefixes = []
        for p in self.raw_paths:
            rx, prefix = compile_glob(expand(p))
            self.patterns.append(rx)
            if prefix:
                self.prefixes.append(prefix)

    def match(self, path: str, size: int) -> bool:
        if size < self.min_size:
            return False
        if self.max_size and size > self.max_size:
            return False
        if self.name_re and not self.name_re.search(os.path.basename(path)):
            return False
        for rx in self.patterns:
            if rx.match(path):
                return True
        return False

    def to_hit(self, path: str, size: int) -> Hit:
        return Hit(path=path, size=size, software=self.software,
                   category=self.category, risk=self.risk,
                   confidence=self.confidence, rule_id=self.id,
                   advice=self.advice, reclaimable=self.reclaimable)


class RuleSet:
    def __init__(self, data: dict):
        self.meta = data
        self.categories = data.get("categories", {})
        self.rules = [Rule(r) for r in data.get("rules", [])]
        # 置信度高的规则优先匹配，避免被泛化规则抢走
        self.rules.sort(key=lambda r: -r.confidence)

    def match(self, path: str, size: int) -> Rule | None:
        for r in self.rules:
            if r.match(path, size):
                return r
        return None

    @property
    def min_size(self) -> int:
        vals = [r.min_size for r in self.rules]
        return min(vals) if vals else 0

    def roots_for_drive(self, drive: str) -> list[str]:
        """把规则里所有字面量前缀限制到指定盘上。

        system_keep 类（WinSxS / DriverStore / WindowsApps / 分页文件等）
        是明确标注「不要删」的，不作为扫描目标 —— 既省时间也避免误导，
        工具的职责是告诉用户该用什么系统命令去清理它们。
        """
        drive = norm_drive(drive)
        seen, out = set(), []
        for r in self.rules:
            if r.category == "system_keep":
                continue
            for pref in r.prefixes:
                p = os.path.normpath(pref)
                if p[:2].upper() != drive:
                    continue
                if p not in seen:
                    seen.add(p)
                    out.append(p)
        return out

    def category_label(self, key: str) -> str:
        c = self.categories.get(key)
        return c.get("label", key) if c else key

    def category_icon(self, key: str) -> str:
        c = self.categories.get(key)
        return c.get("icon", "") if c else ""


RISK_ORDER = {"safe": 0, "medium": 1, "danger": 2}
RISK_LABEL = {"safe": "安全", "medium": "需确认", "danger": "勿删"}


# ────────────────────────────────────────────────────────────
# Everything CLI 定位
# ────────────────────────────────────────────────────────────

def find_es_exe() -> str | None:
    """在常见位置寻找 es.exe（Everything 命令行接口）。"""
    import shutil

    env = os.environ.get("ES_EXE") or os.environ.get("ES_PATH")
    if env and os.path.isfile(env):
        return env

    found = shutil.which("es.exe") or shutil.which("es")
    if found:
        return found

    local = os.environ.get("LOCALAPPDATA", "")
    cands = [
        r"C:\Program Files\Everything\es.exe",
        r"C:\Program Files (x86)\Everything\es.exe",
        os.path.join(local, "Microsoft", "WinGet", "Packages"),
        os.path.join(local, "Programs", "Everything", "es.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    # WinGet 包目录下递归找一层
    winget = os.path.join(local, "Microsoft", "WinGet", "Packages")
    if os.path.isdir(winget):
        try:
            for d in os.listdir(winget):
                if "everything" in d.lower():
                    sub = os.path.join(winget, d)
                    for root, _dirs, files in os.walk(sub):
                        if "es.exe" in files:
                            return os.path.join(root, "es.exe")
        except OSError:
            pass
    return None


# ────────────────────────────────────────────────────────────
# 引擎 1：Everything CLI
# ────────────────────────────────────────────────────────────

class EverythingEngine:
    name = "Everything CLI (es.exe)"

    # 命令行长度上限较保守地取 7000 字符
    MAX_CMD = 7000

    def __init__(self, es_exe: str):
        self.es_exe = es_exe

    def available(self) -> bool:
        try:
            p = subprocess.run([self.es_exe, "-version"], capture_output=True,
                               text=True, timeout=15)
            return p.returncode == 0
        except Exception:
            return False

    def _run(self, args: list[str], timeout: int = 180) -> list[str]:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        p = subprocess.run([self.es_exe] + args, capture_output=True,
                           timeout=timeout, creationflags=flags)
        # es.exe 输出为 UTF-8（路径可能含中文）
        return p.stdout.decode("utf-8", errors="replace").splitlines()

    def _query(self, root: str, query: str, export_csv: Path) -> list[tuple[str, int]]:
        """对单个根路径跑一次 es.exe，返回 (path, size) 列表。

        注意：es.exe 1.x 的 -path 只接受一个路径（多个会互相覆盖），
        所以这里一次只查一个根，由 scan() 循环调用。
        """
        if export_csv.exists():
            try:
                export_csv.unlink()
            except OSError:
                pass
        args = ["-path", root if root.endswith("\\") else root + "\\",
                query, "-size", "-export-csv", str(export_csv)]
        self._run(args)
        rows: list[tuple[str, int]] = []
        if not export_csv.exists():
            return rows
        with open(export_csv, "r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            cols = reader.fieldnames or []
            path_col = next((c for c in cols if c.lower() == "filename"), None)
            size_col = next((c for c in cols if c.lower() == "size"), None)
            if not path_col:
                return rows
            for rec in reader:
                p = (rec.get(path_col) or "").strip()
                if not p:
                    continue
                raw = (rec.get(size_col) or "0") if size_col else "0"
                try:
                    sz = int(str(raw).replace(",", "").strip() or 0)
                except ValueError:
                    sz = 0
                rows.append((p, sz))
        return rows

    def scan(self, drive: str, ruleset: RuleSet, progress=None,
             cancel: threading.Event | None = None) -> ScanResult:
        t0 = time.time()
        drive = norm_drive(drive)
        roots = ruleset.roots_for_drive(drive)
        res = ScanResult(engine=self.name, drive=drive)

        if not roots:
            res.duration = time.time() - t0
            res.error = "该盘没有可扫描的规则路径"
            return res

        # 一条查询：大于最小体积门槛，之后交给 Python 精确匹配
        min_mb = max(1, ruleset.min_size // 1048576)
        query = f"size:>{min_mb}mb"
        tmp = Path(os.environ.get("TEMP", ".")) / f"diskcleaner_es_{os.getpid()}.csv"

        seen: set[str] = set()
        examined = 0
        errors: list[str] = []
        total = len(roots)

        for i, root in enumerate(roots):
            if cancel is not None and cancel.is_set():
                break
            if progress:
                progress(f"Everything 查询 {i + 1}/{total}：{root}")
            try:
                rows = self._query(root, query, tmp)
            except subprocess.TimeoutExpired:
                errors.append(f"{root}: 查询超时")
                continue
            except Exception as e:  # noqa: BLE001
                errors.append(f"{root}: {e}")
                continue

            for path, size in rows:
                if path in seen:
                    continue
                seen.add(path)
                examined += 1
                if size <= 0:
                    continue
                rule = ruleset.match(path, size)
                if rule:
                    res.hits.append(rule.to_hit(path, size))

        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass

        if errors:
            res.error = f"{len(errors)} 个路径查询失败"
        res.files_examined = examined
        res.duration = time.time() - t0
        return res


# ────────────────────────────────────────────────────────────
# 引擎 2：Python os.walk
# ────────────────────────────────────────────────────────────

class WalkerEngine:
    name = "Python 文件遍历（回退）"

    SKIP_DIRS = {
        "$recycle.bin", "system volume information", "winsxs", "driverstore",
        "windowsapps", "node_modules", ".git", ".svn", "__pycache__",
        "assembly", "servicing", "installer",
    }

    def scan(self, drive: str, ruleset: RuleSet, progress=None,
             cancel: threading.Event | None = None) -> ScanResult:
        t0 = time.time()
        drive = norm_drive(drive)
        roots = ruleset.roots_for_drive(drive)
        res = ScanResult(engine=self.name, drive=drive)

        if not roots:
            res.duration = time.time() - t0
            res.error = "该盘没有可扫描的规则路径"
            return res

        min_size = max(1, ruleset.min_size)
        seen: set[str] = set()
        examined = 0

        for i, root in enumerate(roots):
            if cancel is not None and cancel.is_set():
                break
            if not os.path.isdir(root):
                continue
            if progress:
                progress(f"遍历 {i + 1}/{len(roots)}: {root}")
            for dirpath, dirnames, filenames in os.walk(root, topdown=True,
                                                        onerror=lambda e: None):
                if cancel is not None and cancel.is_set():
                    break
                dirnames[:] = [d for d in dirnames
                               if d.lower() not in self.SKIP_DIRS
                               and not os.path.islink(os.path.join(dirpath, d))]
                for fn in filenames:
                    fp = os.path.join(dirpath, fn)
                    if fp in seen:
                        continue
                    seen.add(fp)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    examined += 1
                    if st.st_size < min_size:
                        continue
                    rule = ruleset.match(fp, st.st_size)
                    if rule:
                        res.hits.append(rule.to_hit(fp, st.st_size))

        res.files_examined = examined
        res.duration = time.time() - t0
        return res


# ────────────────────────────────────────────────────────────
# 统一入口
# ────────────────────────────────────────────────────────────

def get_engines(prefer_everything: bool = True):
    """返回可用引擎列表，Everything 优先。"""
    engines = []
    if prefer_everything:
        es = find_es_exe()
        if es:
            eng = EverythingEngine(es)
            if eng.available():
                engines.append(eng)
    engines.append(WalkerEngine())
    return engines


def list_drives() -> list[dict]:
    """列出本机所有固定磁盘。"""
    out = []
    if sys.platform == "win32":
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if not (bitmask >> i) & 1:
                continue
            letter = chr(ord("A") + i)
            root = f"{letter}:\\"
            dtype = ctypes.windll.kernel32.GetDriveTypeW(root)
            if dtype != 3:          # 只看固定磁盘
                continue
            try:
                usage = __import__("shutil").disk_usage(root)
                out.append({"letter": letter, "root": root,
                            "total": usage.total, "free": usage.free,
                            "used": usage.used})
            except OSError:
                out.append({"letter": letter, "root": root,
                            "total": 0, "free": 0, "used": 0})
    return out


def scan_drive(drive: str, ruleset: RuleSet, engines=None,
               progress=None, cancel: threading.Event | None = None) -> ScanResult:
    """扫描指定盘：优先 Everything，失败自动回退到遍历。"""
    engines = engines if engines is not None else get_engines()
    last = None
    for eng in engines:
        if progress:
            progress(f"使用引擎：{eng.name}")
        try:
            res = eng.scan(drive, ruleset, progress=progress, cancel=cancel)
        except Exception as e:  # noqa: BLE001
            res = ScanResult(engine=getattr(eng, "name", "?"), drive=drive, error=str(e))
        if res.error and not res.hits:
            last = res
            continue
        return res
    return last or ScanResult(drive=drive, error="没有可用引擎")


def summarize(result: ScanResult, ruleset: RuleSet) -> dict:
    """按软件和类别汇总。"""
    by_sw: dict[str, dict] = {}
    by_cat: dict[str, dict] = {}
    for h in result.hits:
        s = by_sw.setdefault(h.software, {"size": 0, "count": 0, "reclaimable": 0})
        s["size"] += h.size
        s["count"] += 1
        if h.reclaimable:
            s["reclaimable"] += h.size
        c = by_cat.setdefault(h.category, {"size": 0, "count": 0, "reclaimable": 0})
        c["size"] += h.size
        c["count"] += 1
        if h.reclaimable:
            c["reclaimable"] += h.size
    return {"by_software": by_sw, "by_category": by_cat}


if __name__ == "__main__":
    # 命令行自测
    import argparse
    ap = argparse.ArgumentParser(description="磁盘垃圾扫描（命令行自测）")
    ap.add_argument("drive", nargs="?", default="C:")
    ap.add_argument("--engine", choices=["auto", "es", "walk"], default="auto")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()

    rs = RuleSet(load_signatures())
    if a.engine == "walk":
        engs = [WalkerEngine()]
    elif a.engine == "es":
        es = find_es_exe()
        engs = [EverythingEngine(es)] if es else []
    else:
        engs = None

    r = scan_drive(a.drive, rs, engines=engs,
                   progress=lambda m: print("  ·", m, file=sys.stderr))
    print(f"\n引擎: {r.engine}")
    print(f"盘符: {r.drive}")
    print(f"耗时: {r.duration:.2f}s  检查文件 {r.files_examined} 个")
    print(f"命中: {len(r.hits)} 个，合计 {human_size(r.total_size)}"
          f"（其中建议清理 {human_size(r.reclaimable_size)}）")
    if r.error:
        print("错误:", r.error)

    print("\n=== 按软件 ===")
    s = summarize(r, rs)
    for k, v in sorted(s["by_software"].items(), key=lambda kv: -kv[1]["size"])[:a.top]:
        print(f"  {human_size(v['size']):>12}  {v['count']:>5} 个  {k}")

    print("\n=== 明细 Top ===")
    for h in sorted(r.hits, key=lambda x: -x.size)[:a.top]:
        print(f"  {human_size(h.size):>12}  [{RISK_LABEL.get(h.risk, h.risk)}] "
              f"{h.software} · {h.category}\n                {h.path}")
