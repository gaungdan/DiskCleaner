#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DiskCleaner - 磁盘垃圾清理小工具

选一个盘 → 扫描 → 告诉你每个文件是哪个软件的什么包（安装包/更新包/缓存）
→ 勾选后移到回收站。

扫描引擎：Everything CLI (es.exe) 优先，缺失时自动回退到 Python 遍历。
"""
from __future__ import annotations

import csv
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# 兼容 PyInstaller 打包后的路径
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys._MEIPASS)          # type: ignore[attr-defined]
    EXE_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).resolve().parent
    EXE_DIR = APP_DIR

sys.path.insert(0, str(APP_DIR))

import scanner as SC  # noqa: E402

APP_TITLE = "DiskCleaner · 磁盘垃圾清理"
VERSION = "1.1"

# ── 配色主题 ────────────────────────────────────────────────
LIGHT_THEME = {
    "name": "明亮",
    "BG": "#f2f5f9",          # 窗口底
    "PANEL": "#ffffff",       # 面板/表格底
    "FG": "#1f2937",          # 主文字
    "DIM": "#6b7280",         # 次要文字
    "ACCENT": "#2563eb",      # 主题强调色
    "ACCENT_FG": "#ffffff",   # 强调色上的文字
    "SAFE": "#15803d",
    "MEDIUM": "#b45309",
    "DANGER": "#dc2626",
    "SEL": "#dbeafe",         # 选中行背景
    "SEL_FG": "#1e3a8a",
    "BORDER": "#d5dbe3",
    "HEAD_BG": "#e4e9f0",     # 表头
    "STRIPE": "#f8fafc",      # 斑马纹
}

DARK_THEME = {
    "name": "深色",
    "BG": "#1e2229",
    "PANEL": "#262b33",
    "FG": "#e6e6e6",
    "DIM": "#9aa4b2",
    "ACCENT": "#4aa3ff",
    "ACCENT_FG": "#0f1720",
    "SAFE": "#3ddc84",
    "MEDIUM": "#ffb84d",
    "DANGER": "#ff5c5c",
    "SEL": "#2f3743",
    "SEL_FG": "#ffffff",
    "BORDER": "#3a4250",
    "HEAD_BG": "#313845",
    "STRIPE": "#2b313a",
}

DEFAULT_THEME = "light"

CHECK_ON = "☑"
CHECK_OFF = "☐"
CHECK_NA = "—"


def human(n: int) -> str:
    for unit, div in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{n} B"


def send_to_recycle_bin(paths: list[str]) -> tuple[int, list[str]]:
    """把文件/目录移到 Windows 回收站。返回 (成功数, 失败信息)。"""
    if not paths:
        return 0, []
    listfile = Path(os.environ.get("TEMP", ".")) / f"dc_rb_{os.getpid()}.txt"
    with open(listfile, "w", encoding="utf-8") as f:
        f.write("\n".join(paths))
    ps = f"""
$ErrorActionPreference='SilentlyContinue'
Add-Type -AssemblyName Microsoft.VisualBasic
$ok=0; $bad=@()
foreach($line in [System.IO.File]::ReadAllLines('{listfile}', [System.Text.Encoding]::UTF8)){{
  if([string]::IsNullOrWhiteSpace($line)){{continue}}
  try{{
    if([System.IO.Directory]::Exists($line)){{
      [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($line,'OnlyErrorDialogs','SendToRecycleBin')
    }} else {{
      [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($line,'OnlyErrorDialogs','SendToRecycleBin')
    }}
    if([System.IO.Directory]::Exists($line) -or [System.IO.File]::Exists($line)){{
      $bad += "占用/权限: $line"
    }} else {{ $ok++ }}
  }} catch {{ $bad += "$($_.Exception.Message): $line" }}
}}
Write-Output "OK=$ok"
foreach($b in $bad){{ Write-Output "BAD=$b" }}
"""
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=600, creationflags=flags)
        out = p.stdout.decode("utf-8", errors="replace")
        ok, bad = 0, []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("OK="):
                ok = int(line[3:] or 0)
            elif line.startswith("BAD="):
                bad.append(line[4:])
        return ok, bad
    finally:
        try:
            listfile.unlink()
        except OSError:
            pass


def hard_delete(paths: list[str]) -> tuple[int, list[str]]:
    """永久删除（不进回收站）。"""
    import shutil
    ok, bad = 0, []
    for p in paths:
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=False)
            else:
                os.remove(p)
            ok += 1
        except Exception as e:  # noqa: BLE001
            bad.append(f"{e}: {p}")
    return ok, bad


# ════════════════════════════════════════════════════════════
# 主窗口
# ════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE}  v{VERSION}")
        self.geometry("1340x820")
        self.minsize(1040, 640)

        # 主题
        self.themes = {"light": LIGHT_THEME, "dark": DARK_THEME}
        self.theme_key = DEFAULT_THEME
        self.pal = self.themes[self.theme_key]
        self.configure(bg=self.pal["BG"])

        # 状态
        self.ruleset = SC.RuleSet(SC.load_signatures())
        self.result: SC.ScanResult | None = None
        self.matches: list[SC.Hit] = []
        self.checked: set[str] = set()
        self.iid_to_hit: dict[str, SC.Hit] = {}
        self.group_mode = "flat"           # flat | software | category
        self.sort_mode = "size"            # size | software | category
        self.risk_filter = tk.StringVar(value="reclaim")   # 默认可清理项
        self.only_reclaim = tk.BooleanVar(value=False)
        self.keyword = tk.StringVar(value="")
        self.theme_var = tk.StringVar(value=self.pal["name"])
        self.scan_q: queue.Queue = queue.Queue()
        self.cancel_evt = threading.Event()
        self.scanning = False
        self.es_exe = SC.find_es_exe()

        self._build_style()
        self._build_menu_colors()
        self._build_ui()
        self._refresh_drives()
        self.after(120, self._pump)

    # ── 样式 ─────────────────────────────────────────────
    def _build_style(self):
        p = self.pal
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass

        FONT = "Microsoft YaHei UI"
        st.configure(".", background=p["BG"], foreground=p["FG"],
                     fieldbackground=p["PANEL"], bordercolor=p["BORDER"],
                     lightcolor=p["BORDER"], darkcolor=p["BORDER"],
                     focuscolor=p["ACCENT"], font=(FONT, 9))
        st.configure("TFrame", background=p["BG"])
        st.configure("Panel.TFrame", background=p["PANEL"], relief="solid",
                     borderwidth=1)
        st.configure("TLabel", background=p["BG"], foreground=p["FG"])
        st.configure("Dim.TLabel", background=p["BG"], foreground=p["DIM"])
        st.configure("Panel.TLabel", background=p["PANEL"], foreground=p["FG"])
        st.configure("Head.TLabel", background=p["BG"], foreground=p["ACCENT"],
                     font=(FONT, 16, "bold"))
        st.configure("Big.TLabel", background=p["BG"], foreground=p["ACCENT"],
                     font=("Consolas", 17, "bold"))
        st.configure("TButton", padding=(10, 5), background=p["PANEL"],
                     foreground=p["FG"], borderwidth=1)
        st.map("TButton",
               background=[("pressed", p["HEAD_BG"]), ("active", p["SEL"]),
                           ("disabled", p["BG"])],
               foreground=[("disabled", p["DIM"])])
        st.configure("Accent.TButton", padding=(14, 6), background=p["ACCENT"],
                     foreground=p["ACCENT_FG"], borderwidth=0)
        st.map("Accent.TButton",
               background=[("pressed", p["ACCENT"]), ("active", p["ACCENT"]),
                           ("disabled", p["HEAD_BG"])],
               foreground=[("disabled", p["DIM"])])
        st.configure("TCheckbutton", background=p["BG"], foreground=p["FG"])
        st.map("TCheckbutton", background=[("active", p["BG"])],
               indicatorcolor=[("selected", p["ACCENT"]), ("!selected", p["PANEL"])])
        st.configure("TRadiobutton", background=p["BG"], foreground=p["FG"])
        st.map("TRadiobutton", background=[("active", p["BG"])],
               indicatorcolor=[("selected", p["ACCENT"]), ("!selected", p["PANEL"])])
        st.configure("TEntry", fieldbackground=p["PANEL"], foreground=p["FG"],
                     bordercolor=p["BORDER"], insertcolor=p["FG"])
        st.configure("TCombobox", fieldbackground=p["PANEL"], background=p["PANEL"],
                     foreground=p["FG"], arrowcolor=p["FG"],
                     bordercolor=p["BORDER"], selectbackground=p["SEL"],
                     selectforeground=p["SEL_FG"])
        st.map("TCombobox",
               fieldbackground=[("readonly", p["PANEL"])],
               foreground=[("readonly", p["FG"])])
        self.option_add("*TCombobox*Listbox.background", p["PANEL"])
        self.option_add("*TCombobox*Listbox.foreground", p["FG"])
        self.option_add("*TCombobox*Listbox.selectBackground", p["ACCENT"])
        self.option_add("*TCombobox*Listbox.selectForeground", p["ACCENT_FG"])
        st.configure("TNotebook", background=p["BG"], borderwidth=0, tabmargins=(2, 4, 2, 0))
        st.configure("TNotebook.Tab", padding=(12, 5), background=p["HEAD_BG"],
                     foreground=p["DIM"], borderwidth=0)
        st.map("TNotebook.Tab",
               background=[("selected", p["PANEL"])],
               foreground=[("selected", p["ACCENT"])])
        st.configure("TProgressbar", background=p["ACCENT"], troughcolor=p["HEAD_BG"],
                     bordercolor=p["BORDER"], lightcolor=p["ACCENT"],
                     darkcolor=p["ACCENT"])
        st.configure("Treeview", background=p["PANEL"], fieldbackground=p["PANEL"],
                     foreground=p["FG"], rowheight=25, borderwidth=0,
                     bordercolor=p["BORDER"])
        st.configure("Treeview.Heading", background=p["HEAD_BG"], foreground=p["FG"],
                     relief="flat", padding=(6, 6), font=(FONT, 9, "bold"))
        st.map("Treeview.Heading", background=[("active", p["SEL"])])
        st.map("Treeview", background=[("selected", p["SEL"])],
               foreground=[("selected", p["SEL_FG"])])
        st.configure("TScrollbar", background=p["HEAD_BG"], troughcolor=p["BG"],
                     bordercolor=p["BG"], arrowcolor=p["DIM"])
        st.map("TScrollbar", background=[("active", p["BORDER"])])
        st.configure("TSeparator", background=p["BORDER"])

    # ── 布局 ─────────────────────────────────────────────
    def _build_ui(self):
        # ===== 顶部：标题 + 主题切换 =====
        top = ttk.Frame(self, padding=(14, 12, 14, 6))
        top.pack(fill="x")
        ttk.Label(top, text="🧹 DiskCleaner", style="Head.TLabel").pack(side="left")
        ttk.Label(top, text="  磁盘垃圾扫描 · 识别安装包 / 更新包 / 缓存",
                  style="Dim.TLabel").pack(side="left", pady=(6, 0))
        self.theme_cb = ttk.Combobox(top, state="readonly", width=7,
                                     textvariable=self.theme_var,
                                     values=[t["name"] for t in self.themes.values()])
        self.theme_cb.pack(side="right")
        ttk.Label(top, text="主题：", style="Dim.TLabel").pack(side="right")
        self.theme_cb.bind("<<ComboboxSelected>>", self._on_theme_change)

        bar = ttk.Frame(self, padding=(14, 0, 14, 8))
        bar.pack(fill="x")
        ttk.Label(bar, text="选择磁盘：").pack(side="left")
        self.drive_cb = ttk.Combobox(bar, state="readonly", width=44)
        self.drive_cb.pack(side="left", padx=(0, 10))
        self.scan_btn = ttk.Button(bar, text="🔍  开始扫描", style="Accent.TButton",
                                   command=self.start_scan)
        self.scan_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="停止", command=self.cancel_scan,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.engine_lbl = ttk.Label(bar, text="", style="Dim.TLabel")
        self.engine_lbl.pack(side="left", padx=12)
        self._update_engine_label()

        # ===== 进度 =====
        pf = ttk.Frame(self, padding=(14, 0, 14, 10))
        pf.pack(fill="x")
        self.pbar = ttk.Progressbar(pf, mode="determinate", maximum=100)
        self.pbar.pack(fill="x", side="left", expand=True)
        self.status = ttk.Label(pf, text="就绪", style="Dim.TLabel", width=34,
                                anchor="e")
        self.status.pack(side="right", padx=(10, 0))

        # ===== 主体：左结果树 + 右汇总 =====
        body = ttk.Frame(self, padding=(14, 0, 14, 0))
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)

        # 过滤工具条（独立的 pack 容器，避免与下面的 grid 混用）
        fl = ttk.Frame(left, padding=(0, 0, 0, 6))
        fl.pack(fill="x")
        ttk.Label(fl, text="显示：", style="Dim.TLabel").pack(side="left")
        for val, txt in (("reclaim", "可清理"), ("safe", "仅安全"),
                         ("medium", "仅需确认"), ("danger", "仅勿删"),
                         ("all", "全部")):
            ttk.Radiobutton(fl, text=txt, value=val, variable=self.risk_filter,
                            command=self.render_tree).pack(side="left", padx=2)
        ttk.Checkbutton(fl, text="只看可清理", variable=self.only_reclaim,
                        command=self.render_tree).pack(side="left", padx=(12, 0))
        ttk.Label(fl, text="  搜索：", style="Dim.TLabel").pack(side="left")
        ent = ttk.Entry(fl, textvariable=self.keyword, width=22)
        ent.pack(side="left")
        ent.bind("<KeyRelease>", lambda e: self.render_tree())
        ttk.Label(fl, text="分组：", style="Dim.TLabel").pack(side="left", padx=(12, 0))
        self.group_cb = ttk.Combobox(fl, state="readonly", width=12,
                                     values=["平铺列表", "按软件分组", "按类型分组"])
        self.group_cb.current(0)
        self.group_cb.pack(side="left")
        self.group_cb.bind("<<ComboboxSelected>>", self._on_group_change)

        gridwrap = ttk.Frame(left)
        gridwrap.pack(fill="both", expand=True)

        cols = ("chk", "size", "software", "category", "confidence", "name")
        self.tree = ttk.Treeview(gridwrap, columns=cols, show="tree headings",
                                 selectmode="extended")
        self.tree.heading("#0", text="项目")
        self.tree.column("#0", width=150, minwidth=100, stretch=False)
        self.tree.heading("chk", text="选")
        self.tree.column("chk", width=36, minwidth=36, anchor="center", stretch=False)
        self.tree.heading("size", text="大小")
        self.tree.column("size", width=88, minwidth=80, anchor="e", stretch=False)
        self.tree.heading("software", text="所属软件")
        self.tree.column("software", width=196, minwidth=120, stretch=False)
        self.tree.heading("category", text="类型")
        self.tree.column("category", width=96, minwidth=76, stretch=False)
        self.tree.heading("confidence", text="可信度")
        self.tree.column("confidence", width=60, minwidth=52, anchor="center",
                         stretch=False)
        self.tree.heading("name", text="文件")
        self.tree.column("name", width=200, minwidth=110, stretch=True)

        vs = ttk.Scrollbar(gridwrap, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(gridwrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        gridwrap.rowconfigure(0, weight=1)
        gridwrap.columnconfigure(0, weight=1)

        self.tree.tag_configure("safe", foreground=self.pal["SAFE"])
        self.tree.tag_configure("medium", foreground=self.pal["MEDIUM"])
        self.tree.tag_configure("danger", foreground=self.pal["DANGER"])
        self.tree.tag_configure("group", foreground=self.pal["ACCENT"],
                                font=("Microsoft YaHei UI", 10, "bold"))
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<space>", lambda e: self.toggle_selected())
        self.tree.bind("<Double-1>", self._open_location)

        # 右侧汇总面板
        right = ttk.Frame(body, style="Panel.TFrame", padding=10, width=300)
        right.pack(side="right", fill="y", padx=(10, 0))
        right.pack_propagate(False)
        ttk.Label(right, text="扫描汇总", style="Panel.TLabel",
                  font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        self.sum_lbl = ttk.Label(right, text="尚未扫描", style="Panel.TLabel",
                                 justify="left", wraplength=272)
        self.sum_lbl.pack(anchor="w", pady=(6, 10))

        self.nb = ttk.Notebook(right)
        self.nb.pack(fill="both", expand=True)
        self.sw_tree = self._make_sum_tree(self.nb, "按软件")
        self.cat_tree = self._make_sum_tree(self.nb, "按类型")
        self.nb.add(self.sw_tree.master, text="按软件")
        self.nb.add(self.cat_tree.master, text="按类型")

        ttk.Label(right, text="↑ 双击可只看该软件", style="Panel.TLabel",
                  foreground=self.pal["DIM"]).pack(anchor="w", pady=(6, 0))

        # ===== 详情 =====
        det = ttk.Frame(self, style="Panel.TFrame", padding=(12, 8))
        det.pack(fill="x", padx=14, pady=(10, 0))
        self.detail = ttk.Label(det, text="选中一行查看详情", style="Panel.TLabel",
                                justify="left", anchor="w", wraplength=1180)
        self.detail.pack(fill="x")

        # ===== 底部操作栏 =====
        bottom = ttk.Frame(self, padding=(14, 8, 14, 12))
        bottom.pack(fill="x")
        self.sel_lbl = ttk.Label(bottom, text="已选 0 项 · 0 B", style="Big.TLabel")
        self.sel_lbl.pack(side="left")

        self.clean_btn = ttk.Button(bottom, text="🧺  清理选中（移到回收站）",
                                    command=self.do_clean, state="disabled")
        self.clean_btn.pack(side="right")
        ttk.Button(bottom, text="导出报告", command=self.export_report).pack(
            side="right", padx=6)
        ttk.Button(bottom, text="全不选", command=self.uncheck_all).pack(
            side="right", padx=6)
        ttk.Button(bottom, text="选中所有安全项",
                   command=self.check_all_safe).pack(side="right")

    def _make_sum_tree(self, parent, _title):
        f = ttk.Frame(parent, style="Panel.TFrame")
        t = ttk.Treeview(f, columns=("size", "count"), show="tree",
                         selectmode="browse", height=16)
        t.heading("#0", text="名称")
        t.column("#0", width=118, minwidth=70, stretch=True)
        t.heading("size", text="大小")
        t.column("size", width=64, minwidth=56, anchor="e", stretch=False)
        t.heading("count", text="个数")
        t.column("count", width=44, minwidth=40, anchor="e", stretch=False)
        vs = ttk.Scrollbar(f, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=vs.set)
        t.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        t.tag_configure("safe", foreground=self.pal["SAFE"])
        t.tag_configure("medium", foreground=self.pal["MEDIUM"])
        t.tag_configure("danger", foreground=self.pal["DANGER"])
        t.bind("<Double-1>", lambda e, tr=t: self._filter_from_summary(tr))
        return t

    # ── 主题切换 ─────────────────────────────────────────
    def _on_theme_change(self, _e=None):
        name = self.theme_var.get()
        for key, t in self.themes.items():
            if t["name"] == name:
                self.theme_key = key
                break
        self.pal = self.themes[self.theme_key]
        self._apply_theme()

    def _apply_theme(self):
        """把当前主题套到已经建好的控件上（不重建界面，扫描结果保留）。"""
        p = self.pal
        self.configure(bg=p["BG"])
        self._build_style()
        self._build_menu_colors()

        self.tree.tag_configure("safe", foreground=p["SAFE"])
        self.tree.tag_configure("medium", foreground=p["MEDIUM"])
        self.tree.tag_configure("danger", foreground=p["DANGER"])
        self.tree.tag_configure("group", foreground=p["ACCENT"])
        for t in (self.sw_tree, self.cat_tree):
            t.tag_configure("safe", foreground=p["SAFE"])
            t.tag_configure("medium", foreground=p["MEDIUM"])
            t.tag_configure("danger", foreground=p["DANGER"])

        self._update_engine_label()
        self.render_tree()
        self.render_summary()

    def _build_menu_colors(self):
        """ttk 管不到 tk.Menu，单独上色。"""
        p = self.pal
        self.option_add("*Menu.background", p["PANEL"])
        self.option_add("*Menu.foreground", p["FG"])
        self.option_add("*Menu.activeBackground", p["ACCENT"])
        self.option_add("*Menu.activeForeground", p["ACCENT_FG"])
        self.option_add("*Menu.selectColor", p["ACCENT"])
        self.option_add("*Menu.borderWidth", 1)
        self.option_add("*Menu.relief", "solid")

    # ── 引擎/磁盘信息 ────────────────────────────────────
    def _update_engine_label(self):
        if self.es_exe:
            self.engine_lbl.configure(
                text=f"引擎：Everything CLI ✅  {os.path.basename(self.es_exe)}",
                foreground=self.pal["SAFE"])
        else:
            self.engine_lbl.configure(
                text="引擎：Python 遍历（未找到 es.exe，速度较慢）",
                foreground=self.pal["MEDIUM"])

    def _refresh_drives(self):
        self.drives = SC.list_drives()
        items = []
        for d in self.drives:
            if d["total"]:
                items.append(f"{d['letter']}:  可用 {human(d['free'])} / 共 "
                             f"{human(d['total'])}")
            else:
                items.append(f"{d['letter']}:")
        self.drive_cb["values"] = items
        # 默认选剩余空间最少的盘
        if items:
            idx = min(range(len(self.drives)),
                      key=lambda i: self.drives[i]["free"] or (1 << 62))
            self.drive_cb.current(idx)

    def current_drive(self) -> str:
        i = self.drive_cb.current()
        if i < 0 or i >= len(self.drives):
            return ""
        return self.drives[i]["letter"] + ":"

    # ── 扫描 ────────────────────────────────────────────
    def start_scan(self):
        if self.scanning:
            return
        drive = self.current_drive()
        if not drive:
            messagebox.showwarning("提示", "请先选择一个磁盘")
            return
        self.scanning = True
        self.cancel_evt.clear()
        self.scan_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.pbar.configure(value=0, maximum=100)
        self.checked.clear()
        self.matches.clear()
        self.iid_to_hit.clear()
        self.tree.delete(*self.tree.get_children())
        self.status.configure(text=f"正在扫描 {drive} …")
        t = threading.Thread(target=self._scan_worker, args=(drive,), daemon=True)
        t.start()

    def _scan_worker(self, drive: str):
        def prog(msg: str):
            self.scan_q.put(("progress", msg))
        try:
            engines = SC.get_engines(prefer_everything=True)
            res = SC.scan_drive(drive, self.ruleset, engines=engines,
                                progress=prog, cancel=self.cancel_evt)
            self.scan_q.put(("done", res))
        except Exception:  # noqa: BLE001
            self.scan_q.put(("error", traceback.format_exc()))

    def cancel_scan(self):
        self.cancel_evt.set()
        self.status.configure(text="正在停止…")

    def _pump(self):
        try:
            while True:
                kind, payload = self.scan_q.get_nowait()
                if kind == "progress":
                    self.status.configure(text=payload[-70:])
                    self.pbar.configure(mode="indeterminate")
                    self.pbar.start(60)
                elif kind == "done":
                    self._on_scan_done(payload)
                elif kind == "error":
                    self.pbar.stop()
                    self.pbar.configure(mode="determinate", value=0)
                    messagebox.showerror("扫描出错", payload)
                    self._scan_finished()
        except queue.Empty:
            pass
        self.after(120, self._pump)

    def _on_scan_done(self, res: SC.ScanResult):
        self.pbar.stop()
        self.pbar.configure(mode="determinate", value=100)
        self.result = res
        self.matches = sorted(res.hits, key=lambda h: -h.size)
        self._scan_finished()
        self.render_tree()
        self.render_summary()
        msg = (f"完成 {res.duration:.1f}s · {res.engine} · "
               f"检查 {res.files_examined:,} 个文件 · 命中 {len(res.hits):,} 项")
        self.status.configure(text=msg)

    def _scan_finished(self):
        self.scanning = False
        self.cancel_btn.configure(state="disabled")
        self.scan_btn.configure(state="normal")

    # ── 渲染 ────────────────────────────────────────────
    def _visible_hits(self) -> list[SC.Hit]:
        out = []
        kw = self.keyword.get().strip().lower()
        rf = self.risk_filter.get()
        only = self.only_reclaim.get()
        for h in self.matches:
            if rf == "reclaim":
                if not h.reclaimable:
                    continue
            elif rf == "all":
                pass
            elif h.risk != rf:
                continue
            if only and not h.reclaimable:
                continue
            if kw and kw not in h.path.lower() and kw not in h.software.lower():
                continue
            out.append(h)
        return out

    def render_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.iid_to_hit.clear()
        hits = self._visible_hits()
        if not hits:
            return
        if self.group_mode == "flat":
            for h in hits:
                self._insert_hit("", h)
        else:
            key = (lambda h: h.software) if self.group_mode == "software" \
                else (lambda h: self.ruleset.category_label(h.category))
            groups: dict[str, list[SC.Hit]] = {}
            for h in hits:
                groups.setdefault(key(h), []).append(h)
            for gname, items in sorted(groups.items(),
                                       key=lambda kv: -sum(i.size for i in kv[1])):
                total = sum(i.size for i in items)
                reclaim = sum(i.size for i in items if i.reclaimable)
                label = (f"{gname}   —   {len(items)} 项 · {human(total)}"
                         f"（可清 {human(reclaim)}）")
                gid = self.tree.insert("", "end", text=label, values=("", "", "", "", "", ""),
                                       open=True, tags=("group",))
                if gid not in self.checked:
                    self.checked.add(gid)          # 组默认视为选中，便于整组勾选
                for h in items:
                    self._insert_hit(gid, h)
        self._update_selection_label()

    def _insert_hit(self, parent: str, h: SC.Hit):
        mark = CHECK_ON if h.path in self.checked else (
            CHECK_OFF if h.reclaimable else CHECK_NA)
        iid = self.tree.insert(
            parent, "end",
            text="",
            values=(mark, human(h.size), h.software,
                    self.ruleset.category_label(h.category),
                    f"{h.confidence}%", os.path.basename(h.path)),
            tags=(h.risk,))
        self.iid_to_hit[iid] = h

    def render_summary(self):
        self.sw_tree.delete(*self.sw_tree.get_children())
        self.cat_tree.delete(*self.cat_tree.get_children())
        if not self.result:
            self.sum_lbl.configure(text="尚未扫描")
            return
        r = self.result
        self.sum_lbl.configure(
            text=(f"磁盘：{r.drive}\n"
                  f"引擎：{r.engine}\n"
                  f"耗时：{r.duration:.1f} 秒\n"
                  f"检查文件：{r.files_examined:,} 个\n"
                  f"命中：{len(r.hits):,} 项\n"
                  f"命中合计：{human(r.total_size)}\n"
                  f"可放心清理：{human(r.reclaimable_size)}"))
        s = SC.summarize(r, self.ruleset)
        for name, v in sorted(s["by_software"].items(), key=lambda kv: -kv[1]["size"]):
            risk = "safe" if v["reclaimable"] == v["size"] else (
                "danger" if v["reclaimable"] == 0 else "medium")
            self.sw_tree.insert("", "end", text=name,
                                values=(human(v["size"]), v["count"]), tags=(risk,))
        for key, v in sorted(s["by_category"].items(), key=lambda kv: -kv[1]["size"]):
            risk = "safe" if v["reclaimable"] == v["size"] else (
                "danger" if v["reclaimable"] == 0 else "medium")
            self.cat_tree.insert("", "end",
                                 text=self.ruleset.category_label(key),
                                 values=(human(v["size"]), v["count"]), tags=(risk,))

    def _filter_from_summary(self, tree):
        sel = tree.selection()
        if not sel:
            return
        name = tree.item(sel[0], "text")
        self.group_cb.current(0)
        self.group_mode = "flat"
        self.keyword.set(name)
        self.render_tree()

    def _on_group_change(self, _e=None):
        self.group_mode = ["flat", "software", "category"][self.group_cb.current()]
        self.render_tree()

    # ── 勾选 ────────────────────────────────────────────
    def _on_click(self, event):
        """点击「选」列切换勾选；点在展开箭头上不处理。"""
        region = self.tree.identify("region", event.x, event.y)
        if region not in ("cell", "tree"):
            return
        col = self.tree.identify_column(event.x)
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if self.tree.identify_element(event.x, event.y) == "Treeitem.indicator":
            return
        if col == "#1":                     # chk 列
            if iid in self.iid_to_hit:
                self.toggle_iid(iid)
            else:                            # 分组行：整组切换
                self.toggle_group(iid)
            return "break"

    def toggle_iid(self, iid: str):
        h = self.iid_to_hit.get(iid)
        if not h or not h.reclaimable:
            return
        if h.path in self.checked:
            self.checked.discard(h.path)
            mark = CHECK_OFF
        else:
            self.checked.add(h.path)
            mark = CHECK_ON
        vals = list(self.tree.item(iid, "values"))
        vals[0] = mark
        self.tree.item(iid, values=vals)
        self._update_selection_label()

    def toggle_group(self, gid: str):
        kids = self.tree.get_children(gid)
        on = any(self.iid_to_hit.get(k) and
                 self.iid_to_hit[k].path in self.checked for k in kids)
        for k in kids:
            h = self.iid_to_hit.get(k)
            if not h or not h.reclaimable:
                continue
            if on:
                self.checked.discard(h.path)
            else:
                self.checked.add(h.path)
            vals = list(self.tree.item(k, "values"))
            vals[0] = CHECK_OFF if on else CHECK_ON
            self.tree.item(k, values=vals)
        self._update_selection_label()

    def toggle_selected(self):
        for iid in self.tree.selection():
            if iid in self.iid_to_hit:
                self.toggle_iid(iid)

    def check_all_safe(self):
        pool = self._visible_hits()
        for h in pool:
            if h.risk == "safe" and h.reclaimable:
                self.checked.add(h.path)
        self.render_tree()

    def uncheck_all(self):
        self.checked.clear()
        self.render_tree()

    def _checked_hits(self) -> list[SC.Hit]:
        by_path = {h.path: h for h in self.matches}
        return [by_path[p] for p in self.checked if p in by_path]

    def _update_selection_label(self):
        hits = self._checked_hits()
        n = len(hits)
        total = sum(h.size for h in hits)
        self.sel_lbl.configure(text=f"已选 {n} 项 · {human(total)}")
        self.clean_btn.configure(state="normal" if n else "disabled")

    # ── 详情 ────────────────────────────────────────────
    def _on_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        h = self.iid_to_hit.get(iid)
        if not h:
            self.detail.configure(text="（分组行）双击汇总面板可只看该软件")
            return
        self.detail.configure(
            text=(f"【{h.software}】 {self.ruleset.category_label(h.category)}"
                  f" · 风险：{SC.RISK_LABEL.get(h.risk, h.risk)}"
                  f" · 可信度 {h.confidence}%\n"
                  f"路径：{h.path}\n"
                  f"大小：{human(h.size)}（{h.size:,} 字节）\n"
                  f"建议：{h.advice}"))

    def _open_location(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        h = self.iid_to_hit.get(sel[0])
        if not h:
            return
        target = h.path if os.path.isfile(h.path) else os.path.dirname(h.path)
        try:
            subprocess.Popen(["explorer", "/select,", h.path])
        except Exception:  # noqa: BLE001
            try:
                os.startfile(target)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass

    def _on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            self.tree.focus(iid)
        m = tk.Menu(self, tearoff=0)
        has = bool(self.tree.selection())
        m.add_command(label="勾选 / 取消勾选（空格）", command=self.toggle_selected,
                      state="normal" if has else "disabled")
        m.add_separator()
        m.add_command(label="在资源管理器中定位", command=self._open_location,
                      state="normal" if has else "disabled")
        m.add_separator()
        m.add_command(label="清空回收站（彻底释放空间）", command=self.empty_recycle)
        m.add_command(label="打开回收站", command=self.open_recycle)
        m.add_separator()
        m.add_command(label="永久删除选中项…（不可恢复）", command=self.do_clean_hard,
                      state="normal" if has else "disabled")
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def empty_recycle(self):
        if not messagebox.askyesno(
                "清空回收站",
                "这会彻底删除回收站里的所有内容（无法恢复），从而真正释放磁盘空间。\n\n"
                "注意：包括你之前放进回收站但还没清空的东西。\n继续？"):
            return
        try:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Clear-RecycleBin -DriveLetter C -Force -ErrorAction SilentlyContinue"],
                           capture_output=True, timeout=300, creationflags=flags)
            self._refresh_drives()
            messagebox.showinfo("完成", "已清空回收站。")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("失败", str(e))

    def open_recycle(self):
        try:
            subprocess.Popen(["explorer", "shell:RecycleBinFolder"])
        except Exception:  # noqa: BLE001
            pass

    # ── 清理 ────────────────────────────────────────────
    def do_clean(self):
        hits = self._checked_hits()
        if not hits:
            messagebox.showinfo("提示", "还没有勾选任何项目")
            return
        total = sum(h.size for h in hits)
        danger = [h for h in hits if h.risk == "danger" or not h.reclaimable]
        warn = ""
        if danger:
            warn = f"\n\n⚠ 其中 {len(danger)} 项被标记为「勿删」，请再次确认！"
        if not messagebox.askyesno(
                "确认清理",
                f"将把 {len(hits)} 个项目（{human(total)}）移到回收站。{warn}\n\n"
                f"回收站内容是可以还原的，清空回收站后才真正释放空间。\n继续？"):
            return
        self.status.configure(text="正在移到回收站…")
        self.update_idletasks()
        ok, bad = send_to_recycle_bin([h.path for h in hits])
        for h in hits:
            if h.path in self.checked:
                self.checked.discard(h.path)
        # 从结果里移除已成功的
        gone = {h.path for h in hits} - {b.split(": ", 1)[-1] for b in bad}
        self.matches = [h for h in self.matches if h.path not in gone]
        if self.result:
            self.result.hits = [h for h in self.result.hits if h.path not in gone]
        self.render_tree()
        self.render_summary()
        self._refresh_drives()
        msg = f"已移动 {ok} 项到回收站。"
        if bad:
            msg += f"\n\n{len(bad)} 项失败：\n" + "\n".join(bad[:8])
        self.status.configure(text=msg.replace("\n", " "))
        messagebox.showinfo("清理完成", msg)

    def do_clean_hard(self):
        hits = self._checked_hits()
        if not hits:
            messagebox.showinfo("提示", "还没有勾选任何项目")
            return
        total = sum(h.size for h in hits)
        if not messagebox.askyesno(
                "确认永久删除",
                f"将【永久删除】{len(hits)} 个项目（{human(total)}），"
                f"不进回收站，无法恢复！\n\n确定继续？"):
            return
        if not messagebox.askyesno("最后确认",
                                   "真的要永久删除吗？此操作不可撤销。"):
            return
        ok, bad = hard_delete([h.path for h in hits])
        gone = {h.path for h in hits} - {b.split(": ", 1)[-1] for b in bad}
        self.matches = [h for h in self.matches if h.path not in gone]
        if self.result:
            self.result.hits = [h for h in self.result.hits if h.path not in gone]
        self.render_tree()
        self.render_summary()
        self._refresh_drives()
        messagebox.showinfo("删除完成",
                            f"已永久删除 {ok} 项。" +
                            (f"\n{len(bad)} 项失败。\n" + "\n".join(bad[:8]) if bad else ""))

    # ── 导出 ────────────────────────────────────────────
    def export_report(self):
        if not self.matches:
            messagebox.showinfo("提示", "还没有扫描结果")
            return
        path = filedialog.asksaveasfilename(
            title="导出扫描报告", defaultextension=".csv",
            initialfile=f"DiskCleaner_{self.result.drive.replace(':', '')}_"
                        f"{time.strftime('%Y%m%d_%H%M')}.csv" if self.result else "report.csv",
            filetypes=[("CSV 表格", "*.csv"), ("Markdown", "*.md"),
                       ("JSON", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        r = self.result
        try:
            if path.lower().endswith(".json"):
                data = {
                    "drive": r.drive, "engine": r.engine,
                    "duration": round(r.duration, 2),
                    "files_examined": r.files_examined,
                    "total_size": r.total_size,
                    "reclaimable_size": r.reclaimable_size,
                    "items": [{
                        "path": h.path, "size": h.size, "size_human": human(h.size),
                        "software": h.software, "category": h.category,
                        "category_label": self.ruleset.category_label(h.category),
                        "risk": h.risk, "confidence": h.confidence,
                        "reclaimable": h.reclaimable, "rule": h.rule_id,
                        "advice": h.advice,
                    } for h in sorted(self.matches, key=lambda x: -x.size)],
                }
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            elif path.lower().endswith(".md"):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"# DiskCleaner 扫描报告\n\n")
                    f.write(f"- 磁盘：`{r.drive}`\n- 引擎：{r.engine}\n")
                    f.write(f"- 耗时：{r.duration:.1f} 秒\n")
                    f.write(f"- 检查文件：{r.files_examined:,}\n")
                    f.write(f"- 命中：{len(r.hits):,} 项，合计 {human(r.total_size)}\n")
                    f.write(f"- 建议清理：**{human(r.reclaimable_size)}**\n\n")
                    f.write("| 大小 | 风险 | 所属软件 | 类型 | 可信度 | 路径 |\n")
                    f.write("|---|---|---|---|---|---|\n")
                    for h in sorted(self.matches, key=lambda x: -x.size):
                        f.write(f"| {human(h.size)} | "
                                f"{SC.RISK_LABEL.get(h.risk, h.risk)} | {h.software} | "
                                f"{self.ruleset.category_label(h.category)} | "
                                f"{h.confidence}% | `{h.path}` |\n")
            else:
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["大小(字节)", "大小", "所属软件", "类型",
                                "风险", "可信度", "建议清理", "完整路径", "清理建议"])
                    for h in sorted(self.matches, key=lambda x: -x.size):
                        w.writerow([h.size, human(h.size), h.software,
                                    self.ruleset.category_label(h.category),
                                    SC.RISK_LABEL.get(h.risk, h.risk),
                                    f"{h.confidence}%",
                                    "是" if h.reclaimable else "否",
                                    h.path, h.advice])
            messagebox.showinfo("导出完成", f"报告已保存到：\n{path}")
            try:
                subprocess.Popen(["explorer", "/select,", path])
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("导出失败", str(e))


def main():
    # 高 DPI 适配
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
