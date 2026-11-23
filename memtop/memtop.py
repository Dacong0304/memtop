#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memtop — macOS 内存速查 + 安全结束进程（对齐“活动监视器”的统计口径）。

为什么不用 ps 的 RSS：
    活动监视器“内存”列显示的是 phys_footprint（物理足迹），包含被压缩的内存；
    ps 的 RSS 只统计常驻页，压缩过的部分会漏掉，所以一个吃满内存的浏览器
    在 ps 里可能只有几百 MB。本工具统一用 top 的 MEM 列（同为 phys_footprint），
    与活动监视器排序基本一致。

汇总口径：
    已用 = 应用(匿名页) + 有线(wired) + 压缩(compressor)
    缓存 = file-backed pages（可被系统回收，不算“已用”）
    压力 = kern.memorystatus_vm_pressure_level，1 正常 / 2 警告 / 4 严重

安全模型：
    - 硬保护：PID 0/1、系统路径（/System、/usr/libexec…）、窗口服务器/Finder/Dock/
      launchd 等关键进程、当前会话的祖先进程（终端、tmux、DSH 等），永不允许结束。
    - 他用户的进程默认受保护，--allow-root 可放开（仍不能碰硬保护项）。
    - 结束前必须确认；应用主进程需要输入 yes；SIGTERM 宽限后仍存活再询问 SIGKILL。
    - 所有操作写入 ~/.local/state/memtop/kill.log。
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import ctypes
import curses
import fnmatch
import hashlib
import json
import locale
import os
import platform
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

VERSION = "1.4.0"
HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(HOME, ".config", "memtop")
PROTECT_FILE = os.path.join(CONFIG_DIR, "protect.txt")
STATE_DIR = os.path.join(HOME, ".local", "state", "memtop")
KILL_LOG = os.path.join(STATE_DIR, "kill.log")

# 子进程统一 C locale，避免 top/vm_stat 被本地化
ENV = dict(os.environ, LC_ALL="C", LANG="C")

# ---------------------------------------------------------------- 常量

# 永久不可结束的关键进程（小写比较）
ESSENTIAL = {
    "kernel_task", "launchd", "launchctl", "windowserver", "loginwindow",
    "dock", "finder", "systemuiserver", "controlcenter", "notificationcenter",
    "notificationcenterui", "spotlight", "corespotlightd", "mds", "mds_stores",
    "mdworker", "mdworker_shared", "mdsync", "backupd", "timed", "coreaudiod",
    "securityd", "tccd", "cfprefsd", "opendirectoryd", "configd", "powerd",
    "diskarbitrationd", "distnoted", "hidd", "bluetoothd", "airportd", "wifid",
    "usereventagent", "runningboardd", "logd", "syslogd", "watchdogd",
    "launchservicesd", "syspolicyd", "amfid", "sshd", "sudo", "login", "authd",
    "nehelper", "nesessionmanager", "networkd", "sharingd", "rapportd",
    "coreservicesd", "fseventsd", "thermalmonitord", "appleeventsd",
    "securityagent", "windowserver_arm", "skywalk", "dasd", "filecoordinationd",
    "mediaremoted", "universalaccessd", "studentd", "symptomsd", "nsurlsessiond",
    "cloudd", "bird", "ckdiscretionaryd", "spotlightknowledged",
}

# 结束会明显影响使用体验、但技术上允许的进程（确认时加一句警告）
WARN_NAMES = {
    "google chrome", "safari", "microsoft edge", "firefox", "arc", "brave browser",
    "trae", "code", "cursor", "xcode", "lark", "feishu", "doubao", "wechat", "qq",
    "dingtalk", "slack", "notion", "obsidian", "keynote", "pages", "numbers",
    "microsoft excel", "microsoft word", "microsoft powerpoint", "microsoft outlook",
    "docker", "chatgpt", "claude", "cherry studio", "copilot", "zoom.us",
}

SYSTEM_PATH_PREFIXES = ("/System/", "/usr/libexec/", "/Library/Apple/", "/usr/sbin/", "/sbin/")

# 归组时遇到这些“启动器”就停：shell/终端里起的进程不属于终端 App
SHELL_BARRIERS = {
    "bash", "zsh", "sh", "fish", "dash", "ksh", "csh", "tcsh", "sudo", "su",
    "login", "tmux", "screen", "sshd", "launchctl", "open", "env",
}

PRESSURE_LABEL = {1: "正常", 2: "警告", 4: "严重"}


# ---------------------------------------------------------------- 基础工具

def sh(argv: Sequence[str], timeout: float = 15.0) -> str:
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           text=True, timeout=timeout, env=ENV)
        return p.stdout or ""
    except Exception:
        return ""


def human(n: Optional[int], digits: int = 1) -> str:
    n = int(n or 0)
    for unit, mult in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= mult:
            v = n / mult
            if v >= 100:
                return "%.0f %s" % (v, unit)
            return "%.*f %s" % (digits, v, unit)
    return "%d B" % n


def pct(part: int, total: int) -> float:
    return (100.0 * part / total) if total else 0.0


_SIZE_RE = re.compile(r"^([\d.]+)\s*([KMGTP]?)B?$", re.I)


def parse_size(tok: str) -> int:
    tok = (tok or "").strip().rstrip("+-")
    if not tok:
        return 0
    m = _SIZE_RE.match(tok)
    if not m:
        return 0
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
            "T": 1024 ** 4, "P": 1024 ** 5}[m.group(2).upper()]
    return int(float(m.group(1)) * mult)


def dwidth(s: str) -> int:
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def clip(s: str, width: int) -> str:
    if width <= 0:
        return ""
    if dwidth(s) <= width:
        return s
    out, w = [], 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width - 1:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "…"


def pad(s: str, width: int) -> str:
    s = clip(s, width)
    return s + " " * max(0, width - dwidth(s))


def proc_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # 僵尸进程算已结束
    out = sh(["ps", "-o", "stat=", "-p", str(pid)], timeout=2).strip()
    return bool(out) and not out.startswith("Z")


# ---------------------------------------------------------------- 数据模型

@dataclass
class Proc:
    pid: int
    ppid: int
    uid: int
    user: str
    pname: str
    path: str
    args: str
    rss: int
    elapsed: str
    state: str
    footprint: int = 0
    compressed: int = 0
    bundle: Optional[str] = None
    owner: Optional[str] = None
    protected: bool = False
    reason: str = ""
    risk: str = "safe"          # safe | caution

    @property
    def display(self) -> str:
        return self.pname or "pid %d" % self.pid


@dataclass
class Group:
    key: str
    name: str
    is_app: bool
    members: List[Proc] = field(default_factory=list)
    footprint: int = 0
    compressed: int = 0
    risk: str = "safe"

    @property
    def count(self) -> int:
        return len(self.members)

    @property
    def main_pid(self) -> int:
        for m in self.members:
            if m.pname.lower() == self.name.lower():
                return m.pid
        for m in self.members:
            if m.risk == "caution":
                return m.pid
        return max(self.members, key=lambda p: p.footprint).pid if self.members else 0

    @property
    def all_protected(self) -> bool:
        return all(m.protected for m in self.members) if self.members else True


@dataclass
class MemInfo:
    total: int = 0
    used: int = 0
    app: int = 0
    cached: int = 0
    wired: int = 0
    compressed: int = 0
    free: int = 0
    swap_total: int = 0
    swap_used: int = 0
    pressure: int = 1


@dataclass
class Snapshot:
    ts: float = 0.0
    mem: MemInfo = field(default_factory=MemInfo)
    procs: List[Proc] = field(default_factory=list)
    groups: List[Group] = field(default_factory=list)
    sample_ms: int = 0
    source: str = "top"
    gen: int = 0

    def by_pid(self) -> Dict[int, Proc]:
        return {p.pid: p for p in self.procs}

    @property
    def proc_total(self) -> int:
        return sum(p.footprint for p in self.procs)


# ---------------------------------------------------------------- 采集

def _top_sizes() -> Dict[int, Tuple[int, int]]:
    out = sh(["top", "-l", "1", "-o", "mem", "-stats", "pid,mem,cmprs", "-n", "2000"])
    res: Dict[int, Tuple[int, int]] = {}
    started = False
    for line in out.splitlines():
        if not started:
            if line.strip().startswith("PID") and "MEM" in line:
                started = True
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        res[pid] = (parse_size(parts[1]), parse_size(parts[2]) if len(parts) > 2 else 0)
    return res


PS_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\d+)\s+(\S+)\s+(\S+)\s*(.*)$")


def _ps_rows() -> Tuple[List[dict], Dict[int, str]]:
    out = sh(["ps", "-axo", "pid=,ppid=,uid=,user=,rss=,etime=,stat=,args=", "-ww"])
    rows = []
    for line in out.splitlines():
        m = PS_RE.match(line)
        if not m:
            continue
        rows.append({
            "pid": int(m.group(1)), "ppid": int(m.group(2)), "uid": int(m.group(3)),
            "user": m.group(4), "rss": int(m.group(5)) * 1024,
            "elapsed": m.group(6), "state": m.group(7), "args": m.group(8).strip(),
        })
    comm = {}
    out2 = sh(["ps", "-axo", "pid=,comm="])
    for line in out2.splitlines():
        m = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if m:
            comm[int(m.group(1))] = m.group(2).strip()
    return rows, comm


def _vm_stats() -> dict:
    out = sh(["vm_stat"])
    page = 16384
    m = re.search(r"page size of (\d+)", out)
    if m:
        page = int(m.group(1))
    pages: Dict[str, int] = {}
    for line in out.splitlines()[1:]:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip().rstrip(".").replace(",", "")
        if v.isdigit():
            pages[k.strip().strip('"')] = int(v)
    return {"page": page, "pages": pages}


def _sys_info() -> dict:
    total = 0
    try:
        total = int(sh(["sysctl", "-n", "hw.memsize"]).strip() or 0)
    except ValueError:
        pass
    try:
        pressure = int(sh(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip() or 1)
    except ValueError:
        pressure = 1
    swap_total = swap_used = 0
    sw = sh(["sysctl", "-n", "vm.swapusage"])
    mt = re.search(r"total\s*=\s*([\d.]+)([KMG])", sw)
    mu = re.search(r"used\s*=\s*([\d.]+)([KMG])", sw)
    if mt:
        swap_total = parse_size(mt.group(1) + mt.group(2))
    if mu:
        swap_used = parse_size(mu.group(1) + mu.group(2))
    return {"total": total, "pressure": pressure,
            "swap_total": swap_total, "swap_used": swap_used}


_BUNDLE_RE = re.compile(r"^(.*?\.app)(?:/|$)")

try:
    _LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    _LIBPROC.proc_pidpath.restype = ctypes.c_int
except OSError:  # pragma: no cover
    _LIBPROC = None


def pid_paths(pids: Sequence[int]) -> Dict[int, str]:
    """用 proc_pidpath 取精确可执行路径（对 root 进程同样有效，无需 sudo）。"""
    out: Dict[int, str] = {}
    if _LIBPROC is None:
        return out
    buf = ctypes.create_string_buffer(4096)
    for pid in pids:
        try:
            n = _LIBPROC.proc_pidpath(int(pid), buf, len(buf))
            if n > 0:
                out[pid] = buf.value.decode("utf-8", "replace")
        except Exception:
            continue
    return out


def bundle_of(path: str) -> Optional[str]:
    m = _BUNDLE_RE.match(path or "")
    return m.group(1) if m else None


def ancestors_of(pid: int, by_pid: Dict[int, "Proc"]) -> Set[int]:
    out = set()
    cur = by_pid.get(pid)
    hops = 0
    while cur is not None and hops < 32:
        out.add(cur.pid)
        cur = by_pid.get(cur.ppid)
        hops += 1
    return out


def load_user_protect() -> List[str]:
    try:
        with open(PROTECT_FILE, "r", encoding="utf-8") as f:
            return [ln.strip() for ln in f
                    if ln.strip() and not ln.strip().startswith("#")]
    except OSError:
        return []


def classify(p: Proc, ancestors: Set[int], user_patterns: List[str], allow_root: bool) -> None:
    name = (p.pname or "").lower()
    path = p.path or ""
    if p.pid in (0, 1):
        p.protected, p.reason = True, "系统核心进程"
    elif p.pid in ancestors:
        p.protected, p.reason = True, "当前会话/终端"
    elif name in ESSENTIAL:
        p.protected, p.reason = True, "关键系统进程"
    elif path.startswith(SYSTEM_PATH_PREFIXES):
        p.protected, p.reason = True, "系统组件"
    elif p.uid != os.getuid() and not allow_root:
        p.protected, p.reason = True, "其他用户进程"
    else:
        # 自定义名单同时匹配进程名、所属 .app 名和完整路径：
        # 这样 protect add "Lark" 能覆盖 Lark Helper (Renderer) 这些子进程。
        names = [name]
        if p.bundle:
            names.append(os.path.basename(p.bundle)[:-4].lower())
        for pat in user_patterns:
            pl = pat.lower()
            if (any(fnmatch.fnmatch(n, pl) for n in names)
                    or fnmatch.fnmatch(path, pat)):
                p.protected, p.reason = True, "自定义保护"
                break
    if not p.protected:
        main_of_bundle = (
            bool(p.bundle)
            and ".framework/" not in p.bundle
            and os.path.basename(p.bundle)[:-4].lower() == name
        )
        p.risk = "caution" if (name in WARN_NAMES or main_of_bundle) else "safe"


def build_groups(procs: List[Proc]) -> List[Group]:
    by_pid = {p.pid: p for p in procs}
    for p in procs:
        # 向上最多 3 层找所属 .app；遇到 shell/终端等启动器就停，
        # 否则终端里跑的 node/python 会被错误归到终端 App 名下。
        cur, hops = p, 0
        while cur is not None and hops < 3 and not cur.bundle:
            parent = by_pid.get(cur.ppid)
            if parent is None:
                cur = None
                break
            if os.path.basename(parent.path).lower() in SHELL_BARRIERS:
                cur = None
                break
            cur = parent
            hops += 1
        p.owner = cur.bundle if cur is not None else None

    groups: Dict[str, Group] = {}
    for p in procs:
        if p.owner:
            key = "app:" + p.owner
            name = os.path.basename(p.owner)[:-4]
            g = groups.get(key)
            if g is None:
                g = groups[key] = Group(key=key, name=name, is_app=True)
            g.members.append(p)
        else:
            key = "pid:%d" % p.pid
            g = groups[key] = Group(key=key, name=p.pname or ("pid %d" % p.pid), is_app=False)
            g.members.append(p)

    for g in groups.values():
        g.footprint = sum(m.footprint for m in g.members)
        g.compressed = sum(m.compressed for m in g.members)
        g.members.sort(key=lambda x: x.footprint, reverse=True)
        if g.is_app:
            warn = any(w in g.name.lower() for w in WARN_NAMES)
            has_main = any(m.pname.lower() == g.name.lower() for m in g.members)
            if warn or has_main:
                g.risk = "caution"
        elif any(m.risk == "caution" for m in g.members):
            g.risk = "caution"
    return sorted(groups.values(), key=lambda g: g.footprint, reverse=True)


def sample(include_all: bool = True, user_patterns: Optional[List[str]] = None,
           allow_root: bool = False, gen: int = 0) -> Snapshot:
    t0 = time.time()
    with futures.ThreadPoolExecutor(max_workers=4) as ex:
        f_top = ex.submit(_top_sizes)
        f_ps = ex.submit(_ps_rows)
        f_vm = ex.submit(_vm_stats)
        f_sys = ex.submit(_sys_info)
        top = f_top.result()
        rows, comm = f_ps.result()
        vm = f_vm.result()
        info = f_sys.result()

    page = vm["page"]
    pages = vm["pages"]
    free_pages = pages.get("Pages free", 0) + pages.get("Pages speculative", 0)
    mem = MemInfo(
        total=info["total"],
        app=pages.get("Anonymous pages", 0) * page,
        cached=pages.get("File-backed pages", 0) * page,
        wired=pages.get("Pages wired down", 0) * page,
        compressed=pages.get("Pages occupied by compressor", 0) * page,
        free=free_pages * page,
        swap_total=info["swap_total"],
        swap_used=info["swap_used"],
        pressure=info["pressure"],
    )
    mem.used = mem.app + mem.wired + mem.compressed

    procs: List[Proc] = []
    paths = pid_paths([r["pid"] for r in rows])
    for r in rows:
        pid = r["pid"]
        fp, comp = top.get(pid, (r["rss"], 0))
        path = paths.get(pid) or comm.get(pid, "")
        name = os.path.basename(path) or comm.get(pid, "") or ("pid %d" % pid)
        if name.endswith(".app"):
            name = name[:-4]
        p = Proc(pid=pid, ppid=r["ppid"], uid=r["uid"], user=r["user"],
                 pname=name, path=path, args=r["args"],
                 rss=r["rss"], elapsed=r["elapsed"], state=r["state"],
                 footprint=fp, compressed=comp, bundle=bundle_of(path))
        procs.append(p)

    by_pid = {p.pid: p for p in procs}
    ancestors = ancestors_of(os.getpid(), by_pid)
    patterns = user_patterns if user_patterns is not None else load_user_protect()
    for p in procs:
        classify(p, ancestors, patterns, allow_root)

    if not include_all:
        procs = [p for p in procs if not p.protected or p.uid == os.getuid()]

    groups = build_groups(procs)
    return Snapshot(ts=time.time(), mem=mem, procs=procs, groups=groups,
                    sample_ms=int((time.time() - t0) * 1000),
                    source="top" if top else "ps", gen=gen)


# ---------------------------------------------------------------- 分析

def reclaimable(snap: Snapshot) -> int:
    return sum(m.footprint for g in snap.groups for m in g.members if not m.protected)


def compressed_reclaimable(snap: Snapshot) -> int:
    """可结束进程当前占用的压缩页；结束它们才能真正回收这部分。"""
    return sum(m.compressed for g in snap.groups for m in g.members if not m.protected)


def killable_members(group: Group) -> List[Proc]:
    return [m for m in group.members if not m.protected]


def flag_text(p: Proc) -> str:
    if p.protected:
        return "受保护"
    if p.risk == "caution":
        return "慎"
    return ""


# ---------------------------------------------------------------- 结束进程

def log_kill(record: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(KILL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def execute_kill(targets: List[Proc], sig: int = signal.SIGTERM, grace: float = 3.0,
                 force: bool = False, dry_run: bool = False) -> dict:
    sig_name = signal.Signals(sig).name
    plan = [{"pid": t.pid, "name": t.display, "footprint": t.footprint} for t in targets]
    if dry_run:
        return {"dry_run": True, "plan": plan, "sent": [], "failed": [], "survivors": [], "sig": sig_name}

    sent: List[Proc] = []
    failed = []
    for t in targets:
        try:
            os.kill(t.pid, sig)
            sent.append(t)
        except ProcessLookupError:
            failed.append({"pid": t.pid, "name": t.display, "error": "进程已退出"})
        except PermissionError:
            failed.append({"pid": t.pid, "name": t.display, "error": "权限不足"})
        except OSError as e:
            failed.append({"pid": t.pid, "name": t.display, "error": str(e)})

    deadline = time.time() + max(0.0, grace)
    while time.time() < deadline and any(proc_alive(t.pid) for t in sent):
        time.sleep(0.1)

    survivors = [t for t in sent if proc_alive(t.pid)]
    forced: List[Proc] = []
    if survivors and force:
        for t in survivors:
            try:
                os.kill(t.pid, signal.SIGKILL)
                forced.append(t)
            except OSError:
                pass
        time.sleep(0.3)
        survivors = [t for t in sent if proc_alive(t.pid)]

    result = {
        "dry_run": False, "sig": sig_name, "plan": plan,
        "sent": [t.pid for t in sent], "failed": failed,
        "survivors": [{"pid": t.pid, "name": t.display} for t in survivors],
        "forced": [t.pid for t in forced],
        "reclaimed_est": sum(t.footprint for t in sent),
    }
    log_kill({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "sig": sig_name,
              "pids": [t.pid for t in sent], "names": [t.display for t in sent],
              "survivors": [t.pid for t in survivors]})
    return result


# ---------------------------------------------------------------- 渲染（快照）

C_RED = "\033[31m"
C_YEL = "\033[33m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_RST = "\033[0m"


def render_text(snap: Snapshot, limit: int = 20, tree: bool = False,
                color: bool = True) -> str:
    mem = snap.mem
    red = C_RED if color else ""
    yel = C_YEL if color else ""
    dim = C_DIM if color else ""
    bold = C_BOLD if color else ""
    rst = C_RST if color else ""

    lines = []
    head = "%s %s" % (bold + "memtop" + rst, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snap.ts)))
    lines.append(head)
    p = PRESSURE_LABEL.get(mem.pressure, "未知")
    pcolor = red if mem.pressure >= 4 else (yel if mem.pressure == 2 else "")
    lines.append(
        "内存 %s   已用 %s (%.0f%%)   应用 %s   有线 %s   压缩 %s   缓存 %s   交换 %s   压力 %s%s%s"
        % (human(mem.total), human(mem.used), pct(mem.used, mem.total), human(mem.app),
           human(mem.wired), human(mem.compressed), human(mem.cached),
           human(mem.swap_used), pcolor, p, rst))
    lines.append("%s%d 进程 / %d 组 · 采样 %d ms · 进程足迹合计 %s（含共享页，会大于已用）%s"
                 % (dim, len(snap.procs), len(snap.groups), snap.sample_ms,
                    human(snap.proc_total), rst))
    lines.append("")

    name_w = 30
    header = "  %s %s %s %s %s" % (
        pad("#", 3), pad("内存", 9), pad("占比", 7), pad("压缩", 9), pad("PID", 7))
    header += "进程"
    lines.append(bold + header + rst)

    shown = 0
    for idx, g in enumerate(snap.groups, 1):
        if shown >= limit:
            break
        shown += 1
        tag = ""
        if g.all_protected:
            tag = " [受保护]"
        elif g.risk == "caution":
            tag = " [应用]"
        label = g.name + (" (%d)" % g.count if g.count > 1 else "") + tag
        row = "  %s %s %s %s %s " % (
            pad(str(idx), 3), pad(human(g.footprint), 9),
            pad("%.1f%%" % pct(g.footprint, mem.used), 7),
            pad(human(g.compressed), 9), pad(str(g.main_pid), 7))
        if g.all_protected:
            row = dim + row + clip(label, name_w) + rst
        elif g.risk == "caution":
            row += yel + clip(label, name_w) + rst
        else:
            row += clip(label, name_w)
        lines.append(row)

        if tree:
            for m in g.members[:6]:
                sub = "       %s %s %s %s %s" % (
                    pad("", 12), pad(human(m.footprint), 9), " " * 7,
                    pad(human(m.compressed), 9), pad(str(m.pid), 7))
                sub += dim + "└ " + clip(m.display, name_w) + ("  [%s]" % m.reason if m.protected else "") + rst
                lines.append(sub)

    lines.append("")
    lines.append("%s当前用户可结束的进程合计 ≈ %s%s"
                 % (dim, human(reclaimable(snap)), rst))
    return "\n".join(lines)


def render_json(snap: Snapshot) -> str:
    mem = snap.mem
    data = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(snap.ts)),
        "memory": {
            "total": mem.total, "used": mem.used, "app": mem.app,
            "wired": mem.wired, "compressed": mem.compressed,
            "cached": mem.cached, "free": mem.free,
            "swap_total": mem.swap_total, "swap_used": mem.swap_used,
            "pressure": mem.pressure, "pressure_label": PRESSURE_LABEL.get(mem.pressure),
            "reclaimable": reclaimable(snap),
            "compressed_killable": compressed_reclaimable(snap),
        },
        "processes": len(snap.procs),
        "groups": [{
            "name": g.name, "app": g.is_app, "footprint": g.footprint,
            "compressed": g.compressed, "risk": g.risk,
            "all_protected": g.all_protected, "main_pid": g.main_pid,
            "bundle": g.key[4:] if g.is_app else None,
            "mine": any(m.uid == os.getuid() for m in g.members),
            "protected": g.all_protected,
            "killable": sum(m.footprint for m in g.members if not m.protected),
            "members": [{
                "pid": m.pid, "name": m.display, "path": m.path, "user": m.user,
                "footprint": m.footprint, "compressed": m.compressed,
                "protected": m.protected, "reason": m.reason, "risk": m.risk,
                "elapsed": m.elapsed,
            } for m in g.members],
        } for g in snap.groups],
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 采样线程

class Sampler:
    def __init__(self, interval: float = 2.0, include_all: bool = True,
                 allow_root: bool = False):
        self.interval = max(0.5, interval)
        self.include_all = include_all
        self.allow_root = allow_root
        self._lock = threading.Lock()
        self._sample_lock = threading.Lock()
        self._snap: Optional[Snapshot] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._gen = 0
        self.error: Optional[BaseException] = None

    def sample_now(self) -> Snapshot:
        with self._lock:
            self._gen += 1
            snap = sample(self.include_all, allow_root=self.allow_root, gen=self._gen)
            self._snap = snap
            return snap

    def current(self) -> Optional[Snapshot]:
        with self._lock:
            return self._snap

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self.interval):
                break
            try:
                self.sample_now()
            except Exception as e:  # 保持界面存活
                self.error = e

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------- 交互界面

PAIRS = {"red": 1, "yellow": 2, "green": 3, "cyan": 4, "dim": 5, "sel": 6}


class UI:
    def __init__(self, sampler: Sampler):
        self.sampler = sampler
        self.scr = None
        self.sort = "mem"
        self.filter = ""
        self.expanded: Set[str] = set()
        self.cursor = 0
        self.sel: Optional[Tuple[str, int]] = None   # (group key, pid or 0)
        self.mode = "list"                           # list|search|confirm|force|help|protect
        self.search_buf = ""
        self.confirm: dict = {}
        self.status = ""
        self.status_ts = 0.0
        self.dirty = True
        self.offset = 0

    # -------- 生命周期

    def main(self, stdscr) -> None:
        self.scr = stdscr
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        stdscr.nodelay(True)
        self._init_colors()

        last_gen = -1
        while True:
            snap = self.sampler.current()
            if snap is None:
                time.sleep(0.05)
                continue
            if snap.gen != last_gen or self.dirty:
                self.draw()
                last_gen = snap.gen
                self.dirty = False
            ch = stdscr.getch()
            if ch == -1:
                time.sleep(0.05)
                continue
            if not self.handle(ch, snap):
                break

    def _init_colors(self) -> None:
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(PAIRS["red"], curses.COLOR_RED, -1)
            curses.init_pair(PAIRS["yellow"], curses.COLOR_YELLOW, -1)
            curses.init_pair(PAIRS["green"], curses.COLOR_GREEN, -1)
            curses.init_pair(PAIRS["cyan"], curses.COLOR_CYAN, -1)
            curses.init_pair(PAIRS["dim"], curses.COLOR_WHITE, -1)
            curses.init_pair(PAIRS["sel"], curses.COLOR_BLACK, curses.COLOR_CYAN)
        except curses.error:
            pass

    def color(self, name: str, extra: int = 0) -> int:
        try:
            return curses.color_pair(PAIRS[name]) | extra
        except Exception:
            return extra

    # -------- 行构建

    def rows(self, snap: Snapshot) -> List[Tuple[str, Group, Optional[Proc]]]:
        groups = list(snap.groups)
        if self.sort == "name":
            groups.sort(key=lambda g: g.name.lower())
        elif self.sort == "compressed":
            groups.sort(key=lambda g: g.compressed, reverse=True)
        else:
            groups.sort(key=lambda g: g.footprint, reverse=True)

        f = self.filter.lower()
        out: List[Tuple[str, Group, Optional[Proc]]] = []
        for g in groups:
            if f and f not in g.name.lower() and not any(f in m.pname.lower() for m in g.members):
                continue
            out.append(("group", g, None))
            if g.key in self.expanded:
                members = g.members
                if f:
                    members = [m for m in members if f in m.pname.lower() or f in g.name.lower()]
                for m in members:
                    out.append(("member", g, m))
        if not out:
            out = [("group", g, None) for g in groups[:1]]
        return out

    # -------- 绘制

    def draw(self) -> None:
        scr = self.scr
        h, w = scr.getmaxyx()
        snap = self.sampler.current()
        if snap is None:
            return
        scr.erase()
        rows = self.rows(snap)
        if not rows:
            self.put(scr, 2, 1, "没有匹配的进程", curses.A_DIM)
            self._draw_footer(scr, h, w, snap, rows)
            scr.noutrefresh(); curses.doupdate()
            return

        # 选中项跟随
        if self.sel is not None:
            key, pid = self.sel
            for i, (kind, g, m) in enumerate(rows):
                if kind == "group" and g.key == key and (pid == 0 or not m):
                    self.cursor = i
                    break
                if kind == "member" and g.key == key and m is not None and m.pid == pid:
                    self.cursor = i
                    break
        self.cursor = max(0, min(self.cursor, len(rows) - 1))

        self._draw_header(scr, w, snap, h)
        body_top = 3
        body_h = max(1, h - body_top - 2)
        if self.cursor < self.offset:
            self.offset = self.cursor
        elif self.cursor >= self.offset + body_h:
            self.offset = self.cursor - body_h + 1

        visible = rows[self.offset:self.offset + body_h]
        for i, (kind, g, m) in enumerate(visible):
            y = body_top + i
            self._draw_row(scr, y, w, snap, kind, g, m, self.offset + i == self.cursor)

        self._draw_footer(scr, h, w, snap, rows)
        if self.mode == "confirm":
            self._draw_confirm(scr, h, w, snap)
        elif self.mode == "force":
            self._draw_force(scr, h, w)
        elif self.mode == "help":
            self._draw_help(scr, h, w)
        elif self.mode == "protect":
            self._draw_protect(scr, h, w)
        elif self.mode == "search":
            self._draw_search(scr, h, w)
        scr.noutrefresh()
        curses.doupdate()

    def _draw_header(self, scr, w: int, snap: Snapshot, h: int) -> None:
        mem = snap.mem
        p = PRESSURE_LABEL.get(mem.pressure, "?")
        pc = self.color("red", curses.A_BOLD) if mem.pressure >= 4 else (
            self.color("yellow", curses.A_BOLD) if mem.pressure == 2 else self.color("green", curses.A_BOLD))
        title = " memtop "
        self.put(scr, 0, 0, title, curses.A_REVERSE | curses.A_BOLD)
        self.put(scr, 0, len(title), " 内存 %s · 已用 %s (%.0f%%) · 压缩 %s · 缓存 %s · 交换 %s · 压力 "
                 % (human(mem.total), human(mem.used), pct(mem.used, mem.total),
                    human(mem.compressed), human(mem.cached), human(mem.swap_used)),
                 curses.A_BOLD)
        x = len(title) + dwidth(" 内存 %s · 已用 %s (%.0f%%) · 压缩 %s · 缓存 %s · 交换 %s · 压力 "
                                % (human(mem.total), human(mem.used), pct(mem.used, mem.total),
                                   human(mem.compressed), human(mem.cached), human(mem.swap_used)))
        self.put(scr, 0, x, p, pc)

        bar_w = max(10, min(w - 30, 70))
        frac = min(1.0, mem.used / mem.total if mem.total else 0)
        fill = int(bar_w * frac)
        bar = "█" * fill + "░" * (bar_w - fill)
        self.put(scr, 1, 1, bar[:bar_w], self.color("cyan"))
        legend = "  应用 %s  有线 %s  压缩 %s  缓存 %s" % (
            human(mem.app), human(mem.wired), human(mem.compressed), human(mem.cached))
        self.put(scr, 1, 1 + bar_w, legend, curses.A_DIM)

        hdr = "    %s %s %s %s %s%s" % (
            pad("内存", 8), pad("占比", 6), pad("压缩", 8), pad("PID", 7), pad("进程", 36), "标记")
        self.put(scr, 2, 1, clip(hdr, w - 2), curses.A_BOLD | curses.A_UNDERLINE)

    def _draw_row(self, scr, y: int, w: int, snap: Snapshot, kind: str,
                  g: Group, m: Optional[Proc], selected: bool) -> None:
        mem = snap.mem
        is_group = kind == "group"
        p = m if m is not None else None
        fp = g.footprint if is_group else (p.footprint if p else 0)
        comp = g.compressed if is_group else (p.compressed if p else 0)
        pid = g.main_pid if is_group else (p.pid if p else 0)
        name = g.name if is_group else ("└ " + p.display if p else "")
        if is_group and g.count > 1:
            name += "  (%d)" % g.count

        pctv = pct(fp, mem.used)
        tag = ""
        if is_group:
            tag = "受保护" if g.all_protected else ("应用" if g.risk == "caution" else "")
        else:
            tag = "受保护" if p.protected else ("" if p.risk == "safe" else "慎")

        indent = "  " if is_group else "     "
        arrow = "▸" if is_group and g.key not in self.expanded else ("▾" if is_group else " ")

        left = "%s%s %s %s %s %s" % (
            indent, arrow, pad(human(fp), 8), pad("%.1f%%" % pctv, 6),
            pad(human(comp), 8), pad(str(pid), 7))
        name_w = min(36, max(16, w - dwidth(left) - 10))
        text = left + pad(clip(name, name_w), name_w) + " " + tag

        attr = 0
        if selected:
            attr = self.color("sel", curses.A_BOLD)
        else:
            if is_group and g.all_protected:
                attr = curses.A_DIM
            elif (is_group and g.risk == "caution") or (p and p.risk == "caution"):
                attr = self.color("yellow")
            elif is_group:
                attr = curses.A_BOLD if g.footprint > mem.used * 0.05 else 0
        self.put(scr, y, 0, clip(text, w), attr)

    def _draw_footer(self, scr, h: int, w: int, snap: Snapshot,
                     rows: List[Tuple[str, Group, Optional[Proc]]]) -> None:
        detail_y = h - 2
        status_y = h - 1
        detail = ""
        if rows:
            kind, g, m = rows[self.cursor]
            if kind == "group":
                target = [x for x in g.members if not x.protected]
                detail = "%s · %d 个进程 · 可回收 %s · %s" % (
                    g.name, g.count,
                    human(sum(x.footprint for x in target)) if target else "0 B",
                    "全部受保护" if g.all_protected else ("主进程可能丢失数据" if g.risk == "caution" else "后台进程"))
                if self.expanded and g.key in self.expanded:
                    detail += " · Enter 收起"
                else:
                    detail += " · Enter 展开"
            else:
                detail = "%s · pid %d · %s · %s · %s" % (
                    m.path or m.args, m.pid, m.user, m.elapsed,
                    "受保护：" + m.reason if m.protected else "可结束")
        self.put(scr, detail_y, 1, clip(detail, w - 2), curses.A_DIM)

        hint = " ↑↓ 移动  Enter 展开  x 结束  / 搜索  s 排序(%s)  r 刷新  p 保护  ? 帮助  q 退出" % self.sort
        if self.status and time.time() - self.status_ts < 6:
            self.put(scr, status_y, 1, clip(self.status, w - 2), self.color("green", curses.A_BOLD))
        else:
            self.put(scr, status_y, 1, clip(hint, w - 2), curses.A_REVERSE)

    def _draw_search(self, scr, h: int, w: int) -> None:
        y = h - 3
        self.put(scr, y, 1, "搜索: " + self.search_buf + " ", curses.A_REVERSE)

    def _draw_confirm(self, scr, h: int, w: int, snap: Snapshot) -> None:
        c = self.confirm
        targets: List[Proc] = c["targets"]
        box_w = min(78, w - 4)
        lines = []
        title = "结束 %s" % c.get("title", "")
        lines.append(("  " + title, curses.A_BOLD))
        lines.append(("", 0))
        total = sum(t.footprint for t in targets)
        for t in targets[:6]:
            lines.append(("  · %-32s %8s  pid %d" % (clip(t.display, 32), human(t.footprint), t.pid), 0))
        if len(targets) > 6:
            lines.append(("  … 另外 %d 个进程" % (len(targets) - 6), curses.A_DIM))
        lines.append(("", 0))
        lines.append(("  预计回收 ≈ %s（含共享页，实际会少一些）" % human(total), 0))
        if c.get("risk") == "caution":
            lines.append(("  ⚠ 可能丢失未保存内容 / 关闭标签页", self.color("yellow", curses.A_BOLD)))
            lines.append(("  输入 yes 回车确认，Esc 取消: " + c.get("buf", ""), curses.A_BOLD))
        else:
            lines.append(("  y 确认   n/Esc 取消", curses.A_BOLD))
        self._box(scr, h, w, box_w, len(lines) + 2)
        top = max(0, (h - (len(lines) + 2)) // 2)
        left = max(0, (w - box_w) // 2)
        for i, (text, attr) in enumerate(lines):
            self.put(scr, top + 1 + i, left + 1, clip(text, box_w - 2), attr)

    def _draw_force(self, scr, h: int, w: int) -> None:
        c = self.confirm
        box_w = min(72, w - 4)
        lines = [
            ("  仍有 %d 个进程没有退出" % len(c.get("targets", [])), curses.A_BOLD),
            ("", 0),
            ("  SIGTERM 已发送但不响应，是否强制结束(SIGKILL)？", 0),
            ("  y 强制结束   n 放弃", self.color("yellow", curses.A_BOLD)),
        ]
        self._box(scr, h, w, box_w, len(lines) + 2)
        top = max(0, (h - (len(lines) + 2)) // 2)
        left = max(0, (w - box_w) // 2)
        for i, (text, attr) in enumerate(lines):
            self.put(scr, top + 1 + i, left + 1, clip(text, box_w - 2), attr)

    def _draw_help(self, scr, h: int, w: int) -> None:
        lines = [
            ("  memtop — 快捷键", curses.A_BOLD),
            ("", 0),
            ("  ↑ / ↓ / j / k    上下移动", 0),
            ("  Enter / 空格     展开 / 收起应用分组", 0),
            ("  x                结束选中项（应用分组会结束其全部子进程）", 0),
            ("  /                按名称过滤", 0),
            ("  s                切换排序：内存 → 压缩 → 名称", 0),
            ("  r                立即刷新", 0),
            ("  p                查看受保护名单", 0),
            ("  q                退出", 0),
            ("", 0),
            ("  安全规则：系统进程、其他用户进程、当前终端会话链永远不可结束；", curses.A_DIM),
            ("  应用主进程需要输入 yes 二次确认。所有操作记录在 ~/.local/state/memtop/kill.log", curses.A_DIM),
        ]
        box_w = min(76, w - 4)
        self._box(scr, h, w, box_w, len(lines) + 2)
        top = max(0, (h - (len(lines) + 2)) // 2)
        left = max(0, (w - box_w) // 2)
        for i, (text, attr) in enumerate(lines):
            self.put(scr, top + 1 + i, left + 1, clip(text, box_w - 2), attr)

    def _draw_protect(self, scr, h: int, w: int) -> None:
        patterns = load_user_protect()
        lines = [("  受保护进程", curses.A_BOLD), ("", 0),
                 ("  硬保护：PID 0/1、系统路径、关键系统进程、当前会话祖先进程", curses.A_DIM),
                 ("  其他用户进程默认保护（--allow-root 可放开）", curses.A_DIM), ("", 0)]
        if patterns:
            lines.append(("  自定义规则 (%s):" % PROTECT_FILE, curses.A_BOLD))
            for p in patterns[:20]:
                lines.append(("    " + p, 0))
        else:
            lines.append(("  暂无自定义规则，可写入 %s（每行一个名称/路径，支持 * 通配）" % PROTECT_FILE, 0))
        lines.append(("", 0))
        lines.append(("  q 返回", curses.A_BOLD))
        box_w = min(80, w - 4)
        self._box(scr, h, w, box_w, min(len(lines) + 2, h - 4))
        top = max(0, (h - (min(len(lines) + 2, h - 4) - 2)) // 2)
        left = max(0, (w - box_w) // 2)
        for i, (text, attr) in enumerate(lines):
            if top + 1 + i >= h - 1:
                break
            self.put(scr, top + 1 + i, left + 1, clip(text, box_w - 2), attr)

    def _box(self, scr, h: int, w: int, box_w: int, box_h: int) -> None:
        top = max(0, (h - box_h) // 2)
        left = max(0, (w - box_w) // 2)
        for y in range(top, min(h, top + box_h)):
            self.put(scr, y, left, " " * box_w, curses.A_REVERSE)
        self.put(scr, top, left, "┌" + "─" * (box_w - 2) + "┐", curses.A_REVERSE)
        self.put(scr, top + box_h - 1, left, "└" + "─" * (box_w - 2) + "┘", curses.A_REVERSE)

    def put(self, scr, y: int, x: int, text: str, attr: int = 0) -> None:
        h, w = scr.getmaxyx()
        if y < 0 or y >= h or x >= w or not text:
            return
        text = clip(text, w - x)
        try:
            scr.addstr(y, x, text, attr)
        except curses.error:
            pass

    # -------- 交互

    def handle(self, ch: int, snap: Snapshot) -> bool:
        if self.mode == "confirm":
            return self._handle_confirm(ch)
        if self.mode == "force":
            return self._handle_force(ch)
        if self.mode in ("help", "protect"):
            if ch in (ord("q"), 27, ord("p")):
                self.mode = "list"
                self.dirty = True
            return True
        if self.mode == "search":
            return self._handle_search(ch)

        if ch == ord("q"):
            return False
        if ch == 27:
            if self.filter:
                self.filter = ""
                self.search_buf = ""
                self.cursor = 0
                self.dirty = True
                return True
            return False
        if ch == curses.KEY_RESIZE:
            self.dirty = True
            return True
        if ch in (curses.KEY_UP, ord("k")):
            self.cursor -= 1
            self._sync_sel(snap)
            self.dirty = True
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.cursor += 1
            self._sync_sel(snap)
            self.dirty = True
        elif ch in (curses.KEY_NPAGE,):
            self.cursor += 10
            self._sync_sel(snap)
            self.dirty = True
        elif ch in (curses.KEY_PPAGE,):
            self.cursor -= 10
            self._sync_sel(snap)
            self.dirty = True
        elif ch in (ord("\n"), ord("\r"), ord(" ")):
            self._toggle(snap)
        elif ch in (ord("x"), ord("X")):
            self._ask_kill(snap, group_wide=(ch == ord("X")))
        elif ch == ord("/"):
            self.mode = "search"
            self.search_buf = self.filter
            self.dirty = True
        elif ch == ord("s"):
            order = ["mem", "compressed", "name"]
            self.sort = order[(order.index(self.sort) + 1) % len(order)]
            self.status = "排序：%s" % {"mem": "内存", "compressed": "压缩", "name": "名称"}[self.sort]
            self.status_ts = time.time()
            self.dirty = True
        elif ch == ord("r"):
            self.sampler.sample_now()
            self.status = "已刷新"
            self.status_ts = time.time()
            self.dirty = True
        elif ch in (ord("p"),):
            self.mode = "protect"
            self.dirty = True
        elif ch == ord("?"):
            self.mode = "help"
            self.dirty = True
        return True

    def _handle_search(self, ch: int) -> bool:
        if ch in (27,):
            self.search_buf = ""
            self.filter = ""
            self.mode = "list"
        elif ch in (10, 13):
            self.filter = self.search_buf
            self.mode = "list"
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.search_buf = self.search_buf[:-1]
        elif 32 <= ch < 127:
            self.search_buf += chr(ch)
        self.dirty = True
        return True

    def _sync_sel(self, snap: Snapshot) -> None:
        rows = self.rows(snap)
        if not rows:
            return
        self.cursor = max(0, min(self.cursor, len(rows) - 1))
        kind, g, m = rows[self.cursor]
        self.sel = (g.key, m.pid if m else 0)

    def _toggle(self, snap: Snapshot) -> None:
        rows = self.rows(snap)
        if not rows:
            return
        kind, g, m = rows[self.cursor]
        if kind == "group":
            if g.key in self.expanded:
                self.expanded.discard(g.key)
            else:
                self.expanded.add(g.key)
            self.sel = (g.key, 0)
        self.dirty = True

    def _ask_kill(self, snap: Snapshot, group_wide: bool) -> None:
        rows = self.rows(snap)
        if not rows:
            return
        kind, g, m = rows[self.cursor]
        if kind == "member" and not group_wide:
            targets = [m]
            title = m.display
        else:
            targets = killable_members(g)
            title = "%s（%d 个进程）" % (g.name, len(g.members))
        if not targets:
            self.status = ("%s 全部受保护：%s" % (g.name, m.reason)) if kind == "group" and m is None \
                else ("该进程受保护：%s" % (m.reason if m else "系统组件"))
            self.status_ts = time.time()
            self.dirty = True
            return
        risk = "caution" if (g.risk == "caution" or any(t.risk == "caution" for t in targets)) else "safe"
        self.confirm = {"targets": targets, "title": title, "risk": risk, "buf": ""}
        self.mode = "confirm"
        self.dirty = True

    def _handle_confirm(self, ch: int) -> bool:
        c = self.confirm
        if ch == 27:
            self.mode = "list"
            self.dirty = True
            return True
        if c["risk"] == "caution":
            if ch in (10, 13):
                if c["buf"].strip().lower() == "yes":
                    self._do_kill(c["targets"])
                else:
                    self.mode = "list"
                    self.status = "已取消"
                    self.status_ts = time.time()
                    self.dirty = True
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                c["buf"] = c["buf"][:-1]
                self.dirty = True
            elif 32 <= ch < 127:
                c["buf"] = (c["buf"] + chr(ch))[:20]
                self.dirty = True
        else:
            if ch in (ord("y"), ord("Y")):
                self._do_kill(c["targets"])
            elif ch in (ord("n"), ord("N")):
                self.mode = "list"
                self.status = "已取消"
                self.status_ts = time.time()
                self.dirty = True
        return True

    def _handle_force(self, ch: int) -> bool:
        if ch in (ord("y"), ord("Y")):
            targets = self.confirm.get("targets", [])
            res = execute_kill(targets, signal.SIGKILL, 0.5, force=False)
            self.mode = "list"
            self.status = "已强制结束 %d 个进程" % len(res["sent"])
            self.status_ts = time.time()
            self.sampler.sample_now()
            self.dirty = True
        elif ch in (ord("n"), ord("N"), 27):
            self.mode = "list"
            self.status = "保留未退出的进程"
            self.status_ts = time.time()
            self.dirty = True
        return True

    def _do_kill(self, targets: List[Proc]) -> None:
        self.mode = "list"
        self.put(self.scr, self.scr.getmaxyx()[0] - 1, 1,
                 "正在结束 %d 个进程…" % len(targets), curses.A_REVERSE)
        self.scr.refresh()
        res = execute_kill(targets, signal.SIGTERM, 3.0, force=False)
        total = res.get("reclaimed_est", 0)
        if res["survivors"]:
            still = [t for t in targets if t.pid in {s["pid"] for s in res["survivors"]}]
            self.confirm = {"targets": still}
            self.mode = "force"
            self.status = "已发送 SIGTERM，%d 个进程未退出" % len(still)
            self.status_ts = time.time()
        else:
            self.status = "已结束 %d 个进程，预计回收 %s" % (len(res["sent"]), human(total))
            self.status_ts = time.time()
        self.sampler.sample_now()
        self.dirty = True


# ---------------------------------------------------------------- 命令行

def fmt_plan(result: dict) -> str:
    lines = []
    if result.get("dry_run"):
        lines.append("演练模式（不会真的结束进程）：")
    lines.append("信号 %s，共 %d 个进程：" % (result["sig"], len(result["plan"])))
    for p in result["plan"]:
        lines.append("  - pid %-7d %-32s %s" % (p["pid"], clip(p["name"], 32), human(p["footprint"])))
    lines.append("预计回收 ≈ %s" % human(sum(p["footprint"] for p in result["plan"])))
    if result.get("failed"):
        for f in result["failed"]:
            lines.append("  跳过 pid %d：%s" % (f["pid"], f["error"]))
    if result.get("survivors"):
        lines.append("仍未退出：%s" % ", ".join("%d" % s["pid"] for s in result["survivors"]))
    return "\n".join(lines)


def resolve_targets(specs: List[str], snap: Snapshot) -> Tuple[List[Proc], List[str]]:
    out: List[Proc] = []
    errors: List[str] = []
    by_pid = snap.by_pid()
    for spec in specs:
        if spec.startswith("@"):
            needle = spec[1:].lower()
            groups = [g for g in snap.groups if needle in g.name.lower()]
            if not groups:
                errors.append("没有匹配 %r 的应用/进程组" % spec)
                continue
            for g in groups:
                out.extend(g.members)
        else:
            try:
                pid = int(spec)
            except ValueError:
                errors.append("无法解析 %r（用 pid 或 @名称）" % spec)
                continue
            p = by_pid.get(pid)
            if p is None:
                errors.append("pid %d 不存在" % pid)
                continue
            out.append(p)
    seen, dedup = set(), []
    for p in out:
        if p.pid not in seen:
            seen.add(p.pid)
            dedup.append(p)
    return dedup, errors


def cmd_kill(args) -> int:
    snap = sample(True, allow_root=args.allow_root)
    targets, errors = resolve_targets(args.targets, snap)
    for e in errors:
        print("错误：%s" % e, file=sys.stderr)
    if not targets:
        return 2

    blocked = [t for t in targets if t.protected]
    ok = [t for t in targets if not t.protected]
    for t in blocked:
        print("拒绝结束 pid %d (%s)：%s" % (t.pid, t.display, t.reason), file=sys.stderr)
    if not ok:
        return 2

    total = sum(t.footprint for t in ok)
    print("将结束 %d 个进程（SIGTERM，宽限 %.0fs）：" % (len(ok), args.grace))
    for t in ok:
        print("  - pid %-7d %-32s %8s  %s" % (t.pid, clip(t.display, 32), human(t.footprint),
                                             "慎：应用主进程" if t.risk == "caution" else ""))
    print("预计回收 ≈ %s" % human(total))

    if args.dry_run:
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            print("非交互环境需要 --yes 才会真正结束进程。", file=sys.stderr)
            return 2
        if any(t.risk == "caution" for t in ok):
            print("⚠ 包含应用主进程，可能丢失未保存内容。")
            if input("输入 yes 确认：").strip().lower() != "yes":
                print("已取消")
                return 1
        else:
            if input("确认结束? [y/N] ").strip().lower() not in ("y", "yes"):
                print("已取消")
                return 1

    result = execute_kill(ok, _signal_of(args.signal), args.grace, force=args.force)
    for f in result["failed"]:
        print("失败 pid %d：%s" % (f["pid"], f["error"]), file=sys.stderr)
    print("已发送 %s 给 %d 个进程" % (result["sig"], len(result["sent"])))
    if result["forced"]:
        print("已 SIGKILL：%s" % ", ".join(map(str, result["forced"])))
    if result["survivors"]:
        print("仍未退出：%s（可加 --force）" % ", ".join(str(s["pid"]) for s in result["survivors"]))
        return 1
    print("完成，预计回收 ≈ %s" % human(result["reclaimed_est"]))
    return 0


def _signal_of(name: str) -> int:
    name = (name or "TERM").upper().lstrip("-")
    if name.isdigit():
        return int(name)
    if not name.startswith("SIG"):
        name = "SIG" + name
    return int(getattr(signal, name))


def cmd_protect(args) -> int:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    patterns = load_user_protect()
    if args.action == "list":
        print("配置文件：%s" % PROTECT_FILE)
        if not patterns:
            print("（空）示例：把 “Google Chrome” 或 “*Chrome Helper*” 写进去")
        for p in patterns:
            print("  " + p)
        return 0
    if args.action in ("add", "rm") and not args.pattern:
        print("请提供名称/路径模式，例如：memtop protect add \"Google Chrome\"", file=sys.stderr)
        return 2
    if args.action == "add":
        if args.pattern in patterns:
            print("已存在：%s" % args.pattern)
            return 0
        with open(PROTECT_FILE, "a", encoding="utf-8") as f:
            f.write(args.pattern + "\n")
        print("已加入保护：%s" % args.pattern)
        return 0
    if args.action == "rm":
        keep = [p for p in patterns if p != args.pattern]
        with open(PROTECT_FILE, "w", encoding="utf-8") as f:
            for p in keep:
                f.write(p + "\n")
        print("已移除：%s" % args.pattern)
        return 0
    return 1


def cmd_purge(args) -> int:
    snap = sample(True)
    before = snap.mem.cached
    print("当前文件缓存 ≈ %s，清理需要管理员授权…" % human(before))
    code, out, err = system_purge()
    if code != 0:
        canceled = "cancel" in err.lower() or "-128" in err
        print("已取消授权" if canceled else ("清理失败：%s" % (err or "purge 退出码 %d" % code)),
              file=sys.stderr)
        return 2 if canceled else 1
    after = sample(True).mem.cached
    print("缓存 %s → %s，释放 ≈ %s" % (human(before), human(after), human(max(0, before - after))))
    return 0


def cmd_pressure(args) -> int:
    snap = sample(True)
    mem = snap.mem
    print("物理内存   %s" % human(mem.total))
    print("已用       %s (%.0f%%)" % (human(mem.used), pct(mem.used, mem.total)))
    print("  应用     %s" % human(mem.app))
    print("  有线     %s" % human(mem.wired))
    print("  压缩     %s" % human(mem.compressed))
    print("缓存文件   %s（可回收）" % human(mem.cached))
    print("空闲       %s" % human(mem.free))
    print("交换       %s / %s" % (human(mem.swap_used), human(mem.swap_total)))
    print("压力       %s" % PRESSURE_LABEL.get(mem.pressure, "?"))
    print("可结束进程合计 ≈ %s" % human(reclaimable(snap)))
    print("其中压缩内存   ≈ %s（结束进程才可回收）" % human(compressed_reclaimable(snap)))
    return 0


# ---------------------------------------------------------------- Web 仪表盘

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
ICON_CACHE = os.path.join(HOME, ".cache", "memtop", "icons")
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def find_web_dir() -> str:
    for cand in (WEB_DIR,
                 os.path.join(HOME, ".local", "share", "memtop", "web"),
                 os.path.join(os.path.dirname(WEB_DIR), "share", "memtop", "web")):
        if os.path.isfile(os.path.join(cand, "index.html")):
            return cand
    return WEB_DIR


def _machine_info() -> dict:
    return {
        "user": os.environ.get("USER") or "user",
        "macos": platform.mac_ver()[0],
        "chip": sh(["sysctl", "-n", "machdep.cpu.brand_string"]).strip(),
        "arch": platform.machine(),
        "host": (platform.node() or "").split(".")[0],
    }


def system_purge(timeout: float = 120.0) -> Tuple[int, str, str]:
    """调用系统 purge 丢弃文件缓存；会弹出管理员授权框。"""
    script = 'do shell script "/usr/sbin/purge" with administrator privileges'
    try:
        p = subprocess.run(["/usr/bin/osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return -1, "", "授权或执行超时"
    except OSError as e:
        return -1, "", str(e)


def group_json(g: Group) -> dict:
    return {
        "key": g.key,
        "name": g.name,
        "app": g.is_app,
        "footprint": g.footprint,
        "compressed": g.compressed,
        "risk": g.risk,
        "protected": g.all_protected,
        "killable": sum(m.footprint for m in g.members if not m.protected),
        "main_pid": g.main_pid,
        "bundle": g.key[4:] if g.is_app else None,
        "mine": any(m.uid == os.getuid() for m in g.members),
        "members": [{
            "pid": m.pid,
            "name": m.display,
            "path": m.path,
            "bundle": m.bundle,
            "user": m.user,
            "uid": m.uid,
            "footprint": m.footprint,
            "compressed": m.compressed,
            "protected": m.protected,
            "reason": m.reason,
            "risk": m.risk,
            "elapsed": m.elapsed,
        } for m in g.members],
    }


def snapshot_json(snap: Snapshot, state: "WebState") -> dict:
    mem = snap.mem
    return {
        "ts": round(snap.ts, 3),
        "age": round(max(0.0, time.time() - snap.ts), 2),
        "memory": {
            "total": mem.total,
            "used": mem.used,
            "app": mem.app,
            "wired": mem.wired,
            "compressed": mem.compressed,
            "cached": mem.cached,
            "free": mem.free,
            "swap_total": mem.swap_total,
            "swap_used": mem.swap_used,
            "pressure": mem.pressure,
            "pressure_label": PRESSURE_LABEL.get(mem.pressure, "未知"),
            "reclaimable": reclaimable(snap),
            "compressed_killable": compressed_reclaimable(snap),
        },
        "processes": len(snap.procs),
        "groups": [group_json(g) for g in snap.groups],
        "meta": {
            "version": VERSION,
            "sample_ms": snap.sample_ms,
            "interval": state.interval,
            "protect": load_user_protect(),
            "machine": state.machine,
        },
    }


def icon_png(bundle: str) -> Optional[str]:
    """把 .app 的 .icns 转成 png 并缓存；找不到图标返回 None。"""
    if not bundle or not bundle.endswith(".app"):
        return None
    res = os.path.join(bundle, "Contents", "Resources")
    try:
        names = os.listdir(res)
    except OSError:
        return None
    icns = [os.path.join(res, n) for n in names if n.lower().endswith(".icns")]
    if not icns:
        return None
    prefer = [p for p in icns if os.path.basename(p).lower().startswith("appicon")]
    src = max(prefer or icns, key=lambda p: os.path.getsize(p))
    try:
        stamp = "%s:%d" % (src, os.stat(src).st_mtime_ns)
    except OSError:
        return None
    key = hashlib.sha1(stamp.encode("utf-8")).hexdigest()[:20]
    out = os.path.join(ICON_CACHE, key + ".png")
    if os.path.exists(out):
        return out
    try:
        os.makedirs(ICON_CACHE, exist_ok=True)
        p = subprocess.run(["sips", "-s", "format", "png", "-Z", "96", src, "--out", out],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if p.returncode == 0 and os.path.exists(out):
            return out
    except Exception:
        pass
    return None


class WebState:
    """后台采样线程 + 最近一次快照，HTTP 请求只读缓存，不重复采样。"""

    def __init__(self, interval: float = 2.0, allow_root: bool = False):
        self.interval = max(0.5, float(interval))
        self.allow_root = allow_root
        self.token = secrets.token_urlsafe(24)
        self.machine = _machine_info()
        self._lock = threading.Lock()
        self._sample_lock = threading.Lock()
        self._snap: Optional[Snapshot] = None
        self._error: Optional[str] = None
        self._gen = 0
        self._last_purge = 0.0
        self._last_request = time.time()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def sample_once(self) -> None:
        with self._sample_lock:
            self._gen += 1
            try:
                snap = sample(True, allow_root=self.allow_root, gen=self._gen)
                with self._lock:
                    self._snap, self._error = snap, None
            except Exception as e:  # 采样失败不要杀掉服务
                with self._lock:
                    self._error = "%s: %s" % (type(e).__name__, e)

    def get(self) -> Optional[Snapshot]:
        with self._lock:
            return self._snap

    def snapshot_for_request(self) -> Optional[Snapshot]:
        """被请求时返回快照；如果服务已经空闲很久，同步采一次再返回。"""
        now = time.time()
        idle = now - self._last_request > 30
        self._last_request = now
        if idle or self.get() is None:
            self.sample_once()
        return self.get()

    def last_error(self) -> Optional[str]:
        with self._lock:
            return self._error

    def start(self) -> None:
        self.sample_once()

        def loop() -> None:
            # 没有客户端访问超过 30 秒就停止采样，避免后台空转
            while not self._stop.wait(0.5):
                if time.time() - self._last_request > 30:
                    continue
                self.sample_once()
                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=loop, name="memtop-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)


def make_handler(state: WebState, web_dir: str):
    index_path = os.path.join(web_dir, "index.html")

    class Handler(BaseHTTPRequestHandler):
        server_version = "memtop/" + VERSION
        protocol_version = "HTTP/1.1"

        # ---------- 基础 ----------

        def log_message(self, fmt: str, *args) -> None:  # 静默
            return

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost", "::1")

        def _send(self, code: int, body: bytes, ctype: str,
                  extra: Optional[dict] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, code: int, obj: dict) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       MIME[".json"])

        def _deny(self, code: int = 403, msg: str = "禁止访问") -> None:
            self._json(code, {"ok": False, "error": msg})

        def _token_ok(self) -> bool:
            got = self.headers.get("X-MemTop-Token") or ""
            return bool(got) and secrets.compare_digest(got, state.token)

        def _body(self) -> Optional[dict]:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if n <= 0 or n > 1 << 20:
                return None
            ctype = (self.headers.get("Content-Type") or "").lower()
            if not ctype.startswith("application/json"):
                return None
            try:
                obj = json.loads(self.rfile.read(n).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None
            return obj if isinstance(obj, dict) else None

        # ---------- GET ----------

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._deny()
            parts = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parts.path)
            qs = urllib.parse.parse_qs(parts.query)

            if path in ("/", "/index.html"):
                return self._serve_index()
            if path == "/api/snapshot":
                snap = state.snapshot_for_request()
                if snap is None:
                    err = state.last_error()
                    return self._json(503, {"ok": False, "error": err or "首次采样中"})
                data = snapshot_json(snap, state)
                return self._json(200, {"ok": True, **data})
            if path == "/api/log":
                return self._serve_log(qs)
            if path == "/api/protect":
                return self._json(200, {"ok": True, "patterns": load_user_protect(),
                                        "file": PROTECT_FILE})
            if path == "/api/icon":
                return self._serve_icon(qs)
            return self._serve_static(path)

        def _serve_index(self) -> None:
            try:
                with open(index_path, "rb") as f:
                    html = f.read().decode("utf-8")
            except OSError:
                return self._json(500, {"ok": False, "error": "缺少前端文件 web/index.html"})
            html = html.replace("__MEMTOP_TOKEN__", state.token)
            html = html.replace("__MEMTOP_VERSION__", VERSION)
            self._send(200, html.encode("utf-8"), MIME[".html"])

        def _serve_static(self, path: str) -> None:
            rel = path.lstrip("/")
            full = os.path.realpath(os.path.join(web_dir, rel))
            if not full.startswith(os.path.realpath(web_dir) + os.sep):
                return self._deny(404, "未找到")
            if not os.path.isfile(full):
                return self._deny(404, "未找到")
            ctype = MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
            try:
                with open(full, "rb") as f:
                    body = f.read()
            except OSError:
                return self._deny(404, "未找到")
            self._send(200, body, ctype)

        def _serve_icon(self, qs: dict) -> None:
            bundle = (qs.get("bundle") or [""])[0]
            icon = icon_png(bundle)
            if not icon:
                return self._deny(404, "无图标")
            try:
                with open(icon, "rb") as f:
                    body = f.read()
            except OSError:
                return self._deny(404, "无图标")
            self._send(200, body, MIME[".png"],
                       {"Cache-Control": "public, max-age=86400"})

        def _serve_log(self, qs: dict) -> None:
            try:
                limit = max(1, min(200, int((qs.get("limit") or ["40"])[0])))
            except ValueError:
                limit = 40
            rows = []
            try:
                with open(KILL_LOG, "r", encoding="utf-8") as f:
                    for line in f.readlines()[-limit:]:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            continue
            except OSError:
                pass
            self._json(200, {"ok": True, "rows": list(reversed(rows)), "file": KILL_LOG})

        # ---------- POST ----------

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._deny()
            if not self._token_ok():
                return self._deny(403, "会话令牌无效，请从启动时打印的地址打开页面")
            path = urllib.parse.urlsplit(self.path).path
            body = self._body()
            if body is None:
                return self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
            if path == "/api/kill":
                return self._api_kill(body)
            if path == "/api/protect":
                return self._api_protect(body)
            if path == "/api/purge":
                return self._api_purge(body)
            return self._deny(404, "未找到")

        def _api_kill(self, body: dict) -> None:
            try:
                pids = [int(x) for x in (body.get("pids") or [])][:300]
            except (TypeError, ValueError):
                return self._json(400, {"ok": False, "error": "pids 必须是整数数组"})
            if not pids:
                return self._json(400, {"ok": False, "error": "没有指定进程"})

            sig_name = str(body.get("signal") or "TERM").upper().lstrip("-")
            if sig_name not in ("TERM", "KILL"):
                return self._json(400, {"ok": False, "error": "只支持 TERM / KILL"})
            sig = signal.SIGTERM if sig_name == "TERM" else signal.SIGKILL
            dry_run = bool(body.get("dry_run"))
            force = bool(body.get("force"))
            ack = bool(body.get("ack"))
            try:
                grace = max(0.0, min(10.0, float(body.get("grace", 3.0))))
            except (TypeError, ValueError):
                grace = 3.0

            # 结束前同步重采一次：避免用 2 秒前的缓存判断保护状态，
            # 也避免刚启动的进程因为还没进快照而被当成“不存在”。
            state.sample_once()
            snap = state.get()
            if snap is None:
                return self._json(503, {"ok": False, "error": "采样尚未就绪"})
            by_pid = snap.by_pid()

            targets: List[Proc] = []
            missing, refused, caution = [], [], []
            for pid in dict.fromkeys(pids):
                p = by_pid.get(pid)
                if p is None:
                    missing.append(pid)
                    continue
                if p.protected:
                    refused.append({"pid": pid, "name": p.display, "reason": p.reason})
                    continue
                targets.append(p)
                if p.risk == "caution":
                    caution.append({"pid": pid, "name": p.display})

            if not targets:
                return self._json(400, {"ok": False, "error": "没有可结束的进程",
                                        "refused": refused, "missing": missing})
            if caution and not ack:
                return self._json(409, {
                    "ok": False, "need_ack": True, "caution": caution,
                    "error": "其中包含应用主进程，可能丢失未保存内容",
                })

            result = execute_kill(targets, sig=sig, grace=grace, force=force,
                                  dry_run=dry_run)
            if not dry_run:
                # 同步重采样：让前端紧接着的刷新拿到“已经结束”的状态
                state.sample_once()
            self._json(200, {
                "ok": True, "result": result, "refused": refused, "missing": missing,
                "dry_run": dry_run,
            })

        def _api_purge(self, body: dict) -> None:
            if not body.get("confirm"):
                return self._json(400, {"ok": False, "error": "需要 confirm=true"})
            now = time.time()
            if now - state._last_purge < 30:
                return self._json(429, {"ok": False, "error": "刚清理过，请 30 秒后再试"})

            snap = state.get()
            before = snap.mem.cached if snap else 0
            code, out, err = system_purge()
            if code != 0:
                canceled = "cancel" in err.lower() or "-128" in err
                return self._json(409 if canceled else 500, {
                    "ok": False,
                    "canceled": canceled,
                    "error": "已取消授权" if canceled else (err or "purge 执行失败"),
                })

            state._last_purge = time.time()
            state.sample_once()
            after_snap = state.get()
            after = after_snap.mem.cached if after_snap else 0
            freed = max(0, before - after)
            self._json(200, {
                "ok": True, "before": before, "after": after, "freed": freed,
                "message": "已清理 ≈ %s" % human(freed),
            })

        def _api_protect(self, body: dict) -> None:
            action = str(body.get("action") or "")
            pattern = str(body.get("pattern") or "").strip()
            if action not in ("add", "rm", "list"):
                return self._json(400, {"ok": False, "error": "action 只支持 add/rm/list"})
            if action == "list":
                return self._json(200, {"ok": True, "patterns": load_user_protect()})
            if not pattern or len(pattern) > 200 or "\n" in pattern:
                return self._json(400, {"ok": False, "error": "保护名不能为空且不超过 200 字"})
            patterns = load_user_protect()
            try:
                os.makedirs(CONFIG_DIR, exist_ok=True)
                if action == "add":
                    if pattern not in patterns:
                        with open(PROTECT_FILE, "a", encoding="utf-8") as f:
                            f.write(pattern + "\n")
                else:
                    keep = [p for p in patterns if p != pattern]
                    with open(PROTECT_FILE, "w", encoding="utf-8") as f:
                        for p in keep:
                            f.write(p + "\n")
            except OSError as e:
                return self._json(500, {"ok": False, "error": "写入保护名单失败：%s" % e})
            state.sample_once()  # 立刻重采样，名单生效后返回最新状态
            self._json(200, {"ok": True, "patterns": load_user_protect(),
                             "message": "已加入保护" if action == "add" else "已移除保护"})

    return Handler


def cmd_web(args) -> int:
    web_dir = args.web_dir or find_web_dir()
    if not os.path.isfile(os.path.join(web_dir, "index.html")):
        print("找不到前端文件：%s/index.html" % web_dir, file=sys.stderr)
        return 2

    state = WebState(interval=args.interval, allow_root=args.allow_root)
    print("memtop %s · 正在采样…" % VERSION, file=sys.stderr)
    state.start()

    port = args.port
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state, web_dir))
    except OSError as e:
        print("端口 %d 不可用：%s（换一个 --port）" % (port, e), file=sys.stderr)
        state.stop()
        return 2

    url = "http://127.0.0.1:%d/" % httpd.server_address[1]
    print("MemTop Web 已启动：%s" % url, file=sys.stderr)
    print("仅监听 127.0.0.1；Ctrl+C 退出。", file=sys.stderr)
    if not args.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        state.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memtop",
        description="macOS 内存速查（活动监视器口径）+ 安全结束非必要进程",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  memtop                      交互界面（推荐）
  memtop -1 --limit 15        打印一次快照
  memtop --json               机器可读输出
  memtop pressure             只看内存概况
  memtop kill 35060           结束单个进程（会先确认）
  memtop kill @Chrome --dry-run   预览结束整个 Chrome 组
  memtop kill @Chrome --yes --force  确认后强杀
  memtop protect add "Google Chrome"  永久保护
  memtop purge                清理文件缓存（会请求管理员授权）
  memtop web                  浏览器仪表盘（127.0.0.1，自动打开）
  memtop web --port 9000 --no-open
""")
    p.add_argument("-1", "--once", action="store_true", help="打印一次快照后退出")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    p.add_argument("--limit", type=int, default=20, help="快照显示行数（默认 20）")
    p.add_argument("--tree", action="store_true", help="快照中展开子进程")
    p.add_argument("--sort", choices=["mem", "compressed", "name"], default="mem")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--interval", type=float, default=2.0, help="交互界面刷新间隔秒")
    p.add_argument("--allow-root", action="store_true", help="允许结束其他用户的进程")
    p.add_argument("--version", action="version", version="memtop " + VERSION)

    sub = p.add_subparsers(dest="cmd")

    k = sub.add_parser("kill", help="结束进程（默认 SIGTERM）")
    k.add_argument("targets", nargs="+", help="pid 或 @应用名")
    k.add_argument("-s", "--signal", default="TERM", help="TERM/KILL/INT… 默认 TERM")
    k.add_argument("--grace", type=float, default=3.0, help="SIGTERM 后等待秒数")
    k.add_argument("--force", action="store_true", help="宽限期后仍存活则 SIGKILL")
    k.add_argument("--yes", action="store_true", help="跳过确认")
    k.add_argument("--dry-run", action="store_true", help="只显示计划")
    k.add_argument("--allow-root", action="store_true")

    pr = sub.add_parser("protect", help="管理永久保护名单")
    pr.add_argument("action", choices=["list", "add", "rm"])
    pr.add_argument("pattern", nargs="?", default="")

    sub.add_parser("pressure", help="内存概况")

    sub.add_parser("purge", help="清理文件缓存（需要管理员授权）")

    w = sub.add_parser("web", help="启动本地 Web 仪表盘")
    w.add_argument("--port", type=int, default=8787, help="监听端口（默认 8787，0 为随机）")
    w.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    w.add_argument("--interval", type=float, default=2.0, help="后台采样间隔秒")
    w.add_argument("--allow-root", action="store_true", help="允许结束其他用户的进程")
    w.add_argument("--web-dir", default=None, help=argparse.SUPPRESS)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    args = build_parser().parse_args(argv)

    if args.cmd == "kill":
        return cmd_kill(args)
    if args.cmd == "protect":
        return cmd_protect(args)
    if args.cmd == "pressure":
        return cmd_pressure(args)
    if args.cmd == "purge":
        return cmd_purge(args)
    if args.cmd == "web":
        return cmd_web(args)

    if args.json:
        print(render_json(sample(True)))
        return 0
    if args.once or not sys.stdout.isatty():
        snap = sample(True)
        color = sys.stdout.isatty() and not args.no_color and os.environ.get("NO_COLOR") is None
        print(render_text(snap, limit=args.limit, tree=args.tree, color=color))
        return 0

    sampler = Sampler(args.interval, include_all=True, allow_root=args.allow_root)
    sampler.sample_now()
    sampler.start()
    ui = UI(sampler)
    try:
        curses.wrapper(ui.main)
    except KeyboardInterrupt:
        pass
    except Exception as e:  # 终端异常时不要留下坏掉的 tty
        print("memtop 出错：%s" % e, file=sys.stderr)
        return 1
    finally:
        sampler.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
