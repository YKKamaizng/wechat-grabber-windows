from __future__ import annotations

import csv
import ctypes
import ctypes.wintypes
import json
import os
import platform
import re
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

APP_NAME = "微信抢图助手"
SYSTEM = platform.system()
BEIJING_TZ = timezone(timedelta(hours=8))
NTP_SERVER = "ntp.aliyun.com"
NTP_PORT = 123
NTP_DELTA = 2208988800
SYNC_INTERVAL = 30
NTP_SAMPLES = 3

SCRIPT_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
LOG_FILE = SCRIPT_DIR / "app_log.txt"

TEMPLATES = {
    "纯编号": "{numbers}",
    "编号": "编号：{numbers}",
    "要": "要 {numbers}",
    "我想要": "我想要 {numbers}",
    "老板": "老板 {numbers} 谢谢",
}


def fetch_ntp_time(timeout: float = 1.0) -> tuple[float, float]:
    """Return (unix_time, rtt_seconds)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    packet = b"\x1b" + 47 * b"\0"
    try:
        t0 = time.time()
        sock.sendto(packet, (NTP_SERVER, NTP_PORT))
        data, _ = sock.recvfrom(1024)
        t1 = time.time()
        int_part, frac_part = struct.unpack("!II", data[40:48])
        ntp_sec = int_part - NTP_DELTA + frac_part / 4294967296.0
        return ntp_sec, t1 - t0
    finally:
        sock.close()


def fetch_http_time(timeout: float = 2.0) -> float:
    url = "https://worldtimeapi.org/api/timezone/Asia/Shanghai"
    req = urllib.request.urlopen(url, timeout=timeout)
    return float(json.loads(req.read())["unixtime"])


class TimeManager:
    def __init__(self):
        self._offset = 0.0
        self._synced = False
        self._status = "同步中…"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._listeners: list[callable] = []

    @property
    def synced(self) -> bool:
        with self._lock:
            return self._synced

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    def add_listener(self, callback):
        self._listeners.append(callback)

    def _notify(self):
        status = self.status
        for cb in list(self._listeners):
            try:
                cb(status)
            except Exception:
                pass

    def sync(self) -> str:
        errors = []
        samples: list[tuple[float, float]] = []
        for _ in range(NTP_SAMPLES):
            try:
                ntp_utc, rtt = fetch_ntp_time(1.0)
                # Approximate server time at receive moment by adding half RTT.
                sys_utc = time.time()
                offset = (ntp_utc + rtt / 2.0) - sys_utc
                samples.append((offset, rtt))
            except Exception as e:
                errors.append(str(e))
            time.sleep(0.05)

        if samples:
            # Prefer low-RTT samples, then median their offsets for stability.
            samples.sort(key=lambda x: x[1])
            chosen = samples[: min(3, len(samples))]
            offset = statistics.median(v[0] for v in chosen)
            rtt_ms = statistics.median(v[1] for v in chosen) * 1000
            with self._lock:
                self._offset = offset
                self._synced = True
                self._status = f"✓ 阿里云 NTP，偏差 {offset * 1000:+.1f}ms，RTT {rtt_ms:.0f}ms · 30秒自动同步"
            self._notify()
            return self.status

        try:
            http_utc = fetch_http_time(2.0)
            offset = http_utc - time.time()
            with self._lock:
                self._offset = offset
                self._synced = True
                self._status = f"✓ HTTP 备用时间源，偏差 {offset * 1000:+.1f}ms · 30秒自动重试 NTP"
            self._notify()
            return self.status
        except Exception as e:
            errors.append(str(e))
            with self._lock:
                # Keep previous valid offset when refresh fails.
                if self._synced:
                    self._status = "⚠ 本轮校时失败，继续使用上次有效偏差 · 30秒后重试"
                else:
                    self._status = "⚠ 校时失败，暂用系统时间 · 30秒后重试"
            self._notify()
            return self.status

    def start_auto_sync(self):
        def worker():
            self.sync()
            while not self._stop.wait(SYNC_INTERVAL):
                self.sync()
        threading.Thread(target=worker, daemon=True, name="ntp-auto-sync").start()

    def stop_auto_sync(self):
        self._stop.set()

    def now(self) -> float:
        with self._lock:
            return time.time() + self._offset

    def now_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.now(), BEIJING_TZ)

    def format_now(self) -> str:
        dt = self.now_datetime()
        return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"

    def target_timestamp(self, hour: int, minute: int, second: float) -> float:
        now = self.now_datetime()
        whole = int(second)
        micros = int(round((second - whole) * 1_000_000))
        if micros >= 1_000_000:
            whole += 1
            micros -= 1_000_000
        dt = now.replace(hour=hour, minute=minute, second=whole, microsecond=micros)
        return dt.timestamp()


def _win_copy_clipboard(text: str) -> None:
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    data = (text + "\0").encode("utf-16-le")
    h_global = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h_global:
        raise RuntimeError("无法分配剪贴板内存")
    ptr = kernel32.GlobalLock(h_global)
    ctypes.memmove(ptr, data, len(data))
    kernel32.GlobalUnlock(h_global)
    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(h_global)
        raise RuntimeError("无法打开剪贴板")
    try:
        user32.EmptyClipboard()
        user32.SetClipboardData(CF_UNICODETEXT, h_global)
        h_global = None
    finally:
        user32.CloseClipboard()
        if h_global:
            kernel32.GlobalFree(h_global)


def _wechat_pids() -> set[int]:
    if SYSTEM != "Windows":
        return set()
    names = {"wechat.exe", "wechatapp.exe", "weixin.exe"}
    pids: set[int] = set()
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"], text=True, errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=5
        )
        for row in csv.reader(out.splitlines()):
            if len(row) >= 2 and row[0].lower() in names:
                try:
                    pids.add(int(row[1]))
                except ValueError:
                    pass
    except Exception:
        pass
    return pids


def _win_activate_wechat() -> bool:
    user32 = ctypes.windll.user32
    pids = _wechat_pids()
    if not pids:
        return False
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @WNDENUMPROC
    def enum_proc(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                title = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, title, length + 1)
                rect = ctypes.wintypes.RECT() if hasattr(ctypes, "wintypes") else None
                area = 0
                if rect is not None and user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
                found.append((hwnd, title.value, area))
        return True

    user32.EnumWindows(enum_proc, 0)
    if not found:
        # Legacy class fallback.
        hwnd = user32.FindWindowW("WeChatMainWndForPC", None)
        if hwnd:
            found.append((hwnd, "WeChat", 1))
    if not found:
        return False
    # Prefer the largest visible WeChat/Weixin window; avoids choosing a tiny helper window.
    found.sort(key=lambda x: x[2], reverse=True)
    hwnd = found[0][0]
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


def _win_key(vk: int):
    u = ctypes.windll.user32
    KEYUP = 0x0002
    u.keybd_event(vk, 0, 0, 0)
    u.keybd_event(vk, 0, KEYUP, 0)


def _win_paste():
    u = ctypes.windll.user32
    KEYUP = 0x0002
    u.keybd_event(0x11, 0, 0, 0)  # Ctrl down
    u.keybd_event(0x56, 0, 0, 0)  # V down
    u.keybd_event(0x56, 0, KEYUP, 0)
    u.keybd_event(0x11, 0, KEYUP, 0)


def _win_play_sound():
    try:
        import winsound
        winsound.Beep(1200, 100)
    except Exception:
        pass


class WeChatSender:
    def prepare(self, message: str):
        if SYSTEM != "Windows":
            raise RuntimeError("此重建版当前只面向 Windows")
        _win_copy_clipboard(message)
        if not _win_activate_wechat():
            raise RuntimeError("没有找到微信窗口，请先打开并登录微信")
        time.sleep(0.06)

    @classmethod
    def paste_only(cls):
        _win_paste()

    @classmethod
    def enter_only(cls):
        _win_key(0x0D)

    @classmethod
    def play_sound(cls):
        _win_play_sound()


def is_admin() -> bool:
    if SYSTEM != "Windows":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin_calibrate() -> bool:
    if SYSTEM != "Windows":
        return False
    try:
        if getattr(sys, "frozen", False):
            executable = sys.executable
            params = "--calibrate"
        else:
            executable = sys.executable
            params = f'"{Path(__file__).resolve()}" --calibrate'
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, str(SCRIPT_DIR), 1)
        return rc > 32
    except Exception:
        return False


def get_wechat_ports() -> list[int]:
    """Get local TCP ports belonging to WeChat/Weixin processes."""
    if SYSTEM != "Windows":
        return []
    pids = _wechat_pids()
    if not pids:
        return []
    ports: set[int] = set()
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"], text=True, errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=5
        )
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid not in pids:
                continue
            state = parts[-2].upper()
            if state not in {"ESTABLISHED", "SYN_SENT"}:
                continue
            local = parts[1]
            m = re.search(r":(\d+)$", local)
            if m:
                ports.add(int(m.group(1)))
    except Exception:
        pass
    return sorted(ports)


class PacketCapture:
    """Windows built-in PktMon real-time capture; no WinDump/Npcap install needed."""

    TS_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.(\d+)")

    def __init__(self, ports: list[int]):
        self._ports = ports[:32]
        self._proc: subprocess.Popen | None = None
        self._lines: list[tuple[float, str]] = []
        self._lock = threading.Lock()

    def _run_pktmon(self, args: list[str], check=True):
        return subprocess.run(
            ["pktmon", *args], capture_output=True, text=True, errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=8, check=check
        )

    def start(self):
        if SYSTEM != "Windows":
            raise RuntimeError("PktMon 仅适用于 Windows")
        if not is_admin():
            raise RuntimeError("抓包校准需要管理员权限")
        if not self._ports:
            raise RuntimeError("未检测到微信 TCP 端口")

        # Ensure no stale capture from this app. PktMon filters are system-global.
        self._run_pktmon(["stop"], check=False)
        self._run_pktmon(["filter", "remove"], check=False)
        for i, port in enumerate(self._ports):
            r = self._run_pktmon(["filter", "add", f"WeChat{i+1}", "-t", "TCP", "-p", str(port)], check=False)
            if r.returncode != 0:
                raise RuntimeError(f"添加 PktMon 端口过滤失败: {port}\n{r.stderr or r.stdout}")

        cmd = ["pktmon", "start", "--capture", "--comp", "nics", "--log-mode", "real-time", "--flags", "0x010"]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="ignore",
            bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        threading.Thread(target=self._read, daemon=True, name="pktmon-reader").start()
        time.sleep(0.8)
        if self._proc.poll() is not None:
            raise RuntimeError("PktMon 未能启动，请确认使用管理员权限运行")

    def _line_to_epoch(self, line: str) -> float | None:
        m = self.TS_RE.match(line.strip())
        if not m:
            return None
        hh, mm, ss, frac = m.groups()
        frac_sec = float("0." + frac)
        now = datetime.now().astimezone()
        dt = now.replace(hour=int(hh), minute=int(mm), second=int(ss), microsecond=int(frac_sec * 1_000_000))
        epoch = dt.timestamp()
        # Handle midnight boundary.
        now_epoch = time.time()
        if epoch - now_epoch > 12 * 3600:
            epoch -= 86400
        elif now_epoch - epoch > 12 * 3600:
            epoch += 86400
        return epoch

    def _read(self):
        try:
            assert self._proc and self._proc.stdout
            for line in self._proc.stdout:
                ts = self._line_to_epoch(line)
                if ts is not None:
                    with self._lock:
                        self._lines.append((ts, line.rstrip()))
                        if len(self._lines) > 5000:
                            self._lines = self._lines[-3000:]
        except Exception:
            pass

    def first_packet_after(self, since_ts: float, max_delay: float = 1.5) -> tuple[float | None, str | None]:
        deadline = time.time() + max_delay
        while time.time() < deadline:
            with self._lock:
                candidates = [(ts, line) for ts, line in self._lines if ts >= since_ts - 0.005]
            if candidates:
                ts, line = min(candidates, key=lambda x: x[0])
                delay_ms = (ts - since_ts) * 1000.0
                if -5 <= delay_ms <= max_delay * 1000:
                    return delay_ms, line
            time.sleep(0.02)
        return None, None

    def stop(self):
        try:
            subprocess.run(["pktmon", "stop"], capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=5)
        except Exception:
            pass
        try:
            subprocess.run(["pktmon", "filter", "remove"], capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=5)
        except Exception:
            pass
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None


class GrabberTab(ttk.Frame):
    def __init__(self, parent, time_mgr: TimeManager):
        super().__init__(parent)
        self.time_mgr = time_mgr
        self.sender = WeChatSender()
        self._target_ts = 0.0
        self._running = False
        self._triggered = False
        self._warn_at = 3.0
        self._pre_paste = 0.5
        self._pre_fire_ms = 0
        self._build_ui()
        self._tick()

    def _build_ui(self):
        pad = 10
        tf = ttk.LabelFrame(self, text="北京时间")
        tf.pack(fill="x", padx=pad, pady=(pad, 5))
        self._clock = tk.Label(tf, text="00:00:00.000", font=("Consolas", 22, "bold"), fg="#222222")
        self._clock.pack(pady=(6, 0))
        self._sync_status = tk.Label(tf, text=self.time_mgr.status, fg="gray")
        self._sync_status.pack(pady=(0, 6))
        self.time_mgr.add_listener(lambda s: self.after(0, lambda: self._sync_status.config(text=s, fg="green" if s.startswith("✓") else "#aa6600")))

        cf = ttk.LabelFrame(self, text="倒计时")
        cf.pack(fill="x", padx=pad, pady=5)
        self._countdown = tk.Label(cf, text="-- : -- : -- . ---", font=("Consolas", 18, "bold"), fg="#007700")
        self._countdown.pack(pady=8)
        self._status_label = tk.Label(cf, text="等待设定…", fg="gray")
        self._status_label.pack(pady=(0, 6))

        sf = ttk.LabelFrame(self, text="抢图设置")
        sf.pack(fill="x", padx=pad, pady=5)

        row = ttk.Frame(sf); row.pack(fill="x", padx=8, pady=4)
        ttk.Label(row, text="目标时间：").pack(side="left")
        now = self.time_mgr.now_datetime()
        self._hour_var = tk.StringVar(value=f"{now.hour:02d}")
        self._min_var = tk.StringVar(value=f"{now.minute:02d}")
        self._sec_var = tk.StringVar(value="00")
        for var, width, suffix in [(self._hour_var, 4, "时"), (self._min_var, 4, "分"), (self._sec_var, 7, "秒")]:
            ttk.Entry(row, textvariable=var, width=width).pack(side="left", padx=(2, 0))
            ttk.Label(row, text=suffix).pack(side="left", padx=(1, 5))

        row = ttk.Frame(sf); row.pack(fill="x", padx=8, pady=4)
        self._warn_var = tk.StringVar(value="3")
        self._pre_paste_var = tk.StringVar(value="0.5")
        self._pre_fire_var = tk.StringVar(value="0")
        ttk.Label(row, text="提前提醒：").pack(side="left")
        ttk.Entry(row, textvariable=self._warn_var, width=5).pack(side="left")
        ttk.Label(row, text="秒   提前粘贴：").pack(side="left")
        ttk.Entry(row, textvariable=self._pre_paste_var, width=5).pack(side="left")
        ttk.Label(row, text="秒   提前发射：").pack(side="left")
        ttk.Entry(row, textvariable=self._pre_fire_var, width=6).pack(side="left")
        ttk.Label(row, text="ms").pack(side="left")

        row = ttk.Frame(sf); row.pack(fill="x", padx=8, pady=4)
        ttk.Label(row, text="模板：").pack(side="left")
        self._template_var = tk.StringVar(value="纯编号")
        cb = ttk.Combobox(row, textvariable=self._template_var, values=list(TEMPLATES), state="readonly", width=12)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._update_preview())
        ttk.Label(row, text="编号：").pack(side="left", padx=(10, 2))
        self._numbers_var = tk.StringVar()
        ent = ttk.Entry(row, textvariable=self._numbers_var)
        ent.pack(side="left", fill="x", expand=True)
        ent.bind("<KeyRelease>", lambda e: self._update_preview())

        self._preview = tk.Label(sf, text="预览：", anchor="w", fg="#555")
        self._preview.pack(fill="x", padx=8, pady=(2, 6))

        bf = ttk.Frame(self); bf.pack(fill="x", padx=pad, pady=7)
        self._start_btn = ttk.Button(bf, text="▶  开始抢图", command=self._start)
        self._start_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._stop_btn = ttk.Button(bf, text="■ 停止", command=self._stop, state="disabled")
        self._stop_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))

        lf = ttk.LabelFrame(self, text="发送日志"); lf.pack(fill="both", expand=True, padx=pad, pady=(0, pad))
        self._logbox = tk.Text(lf, height=8, bg="#fafafa", state="disabled")
        self._logbox.pack(fill="both", expand=True, padx=5, pady=5)

    def _tick(self):
        dt = self.time_mgr.now_datetime()
        self._clock.config(text=dt.strftime("%H:%M:%S.") + f"{dt.microsecond//1000:03d}")
        if self._running and self._target_ts:
            remaining = self._target_ts - self.time_mgr.now()
            if remaining > 0:
                ms_total = int(remaining * 1000)
                h, rem = divmod(ms_total, 3600000)
                m, rem = divmod(rem, 60000)
                s, ms = divmod(rem, 1000)
                color = "#cc0000" if remaining <= 3 else ("#cc6600" if remaining <= 10 else "#007700")
                self._countdown.config(text=f"{h:02d} : {m:02d} : {s:02d} . {ms:03d}", fg=color)
        self.after(20, self._tick)

    def _build_message(self) -> str:
        numbers = self._numbers_var.get().strip()
        return TEMPLATES[self._template_var.get()].format(numbers=numbers)

    def _update_preview(self):
        self._preview.config(text="预览：" + self._build_message())

    def _start(self):
        numbers = self._numbers_var.get().strip()
        if not numbers:
            messagebox.showwarning("提示", "请输入编号")
            return
        try:
            h = int(self._hour_var.get()); m = int(self._min_var.get()); s = float(self._sec_var.get())
            self._warn_at = max(0.0, float(self._warn_var.get()))
            self._pre_paste = max(0.0, float(self._pre_paste_var.get()))
            self._pre_fire_ms = int(float(self._pre_fire_var.get()))
            target_ts = self.time_mgr.target_timestamp(h, m, s)
        except Exception:
            messagebox.showwarning("提示", "时间格式错误")
            return
        if target_ts <= self.time_mgr.now():
            if messagebox.askyesno("时间已过", f"{h:02d}:{m:02d} 已过，推到明天？"):
                target_ts += 86400
            else:
                return
        self._target_ts = target_ts
        self._running = True
        self._triggered = False
        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        dt = datetime.fromtimestamp(target_ts, BEIJING_TZ)
        message = self._build_message()
        self._status_label.config(text="开始倒计时 → " + dt.strftime("%H:%M:%S"))
        self._log(f"目标: {dt.strftime('%H:%M:%S')}  消息: 「{message}」")
        threading.Thread(target=self._trigger_loop, args=(message,), daemon=True).start()

    def _stop(self):
        self._running = False
        self._status_label.config(text="已停止", fg="gray")
        self._start_btn.config(state="normal")
        self._stop_btn.config(state="disabled")

    def _trigger_loop(self, message: str):
        pre_fire_sec = self._pre_fire_ms / 1000.0
        warned = prepared = pasted = False
        prepare_lead = max(self._pre_paste + 0.3, 0.5)
        while self._running and not self._triggered:
            remaining = self._target_ts - self.time_mgr.now()
            if not prepared and remaining <= prepare_lead:
                try:
                    self.sender.prepare(message)
                    prepared = True
                    self.after(0, lambda: self._status_label.config(text="⚡ 微信已激活…", fg="#cc0000"))
                    self._log("[准备] 剪贴板 + 激活微信")
                except Exception as e:
                    self._log(f"[准备失败] {e}")
            if not pasted and remaining <= self._pre_paste:
                try:
                    self.sender.paste_only(); pasted = True
                    self._log("[粘贴] 内容已粘入微信输入框")
                except Exception as e:
                    self._log(f"[粘贴失败] {e}")
            if not warned and remaining <= self._warn_at:
                warned = True
                WeChatSender.play_sound()
                self._log(f"[提醒] 还有 {max(0, remaining):.1f} 秒！")
            if remaining <= pre_fire_sec:
                self._triggered = True
                self._fire_enter_only(message)
                break
            if remaining > 1:
                time.sleep(0.05)
            elif remaining > 0.1:
                time.sleep(0.01)
            else:
                time.sleep(0.001)

    def _fire_enter_only(self, message: str):
        try:
            self.sender.enter_only(); ok = True
        except Exception:
            ok = False
        now_str = self.time_mgr.format_now()
        def upd():
            self._status_label.config(text="已发送！" if ok else "发送失败", fg="#cc0000")
            self._log(f"[{now_str}] {'✓ 已发送' if ok else '✗ 失败'}（提前 {self._pre_fire_ms}ms） 内容: 「{message}」")
            self._running = False
            self._start_btn.config(state="normal")
            self._stop_btn.config(state="disabled")
        self.after(0, upd)

    def _log(self, msg: str):
        now_str = self.time_mgr.format_now()
        line = f"[{now_str}] {msg}\n"
        try:
            with LOG_FILE.open("a", encoding="utf-8") as f: f.write(line)
        except Exception:
            pass
        def write():
            self._logbox.config(state="normal"); self._logbox.insert("end", line); self._logbox.see("end"); self._logbox.config(state="disabled")
        self.after(0, write)


class CalibrateTab(ttk.Frame):
    def __init__(self, parent, time_mgr: TimeManager, grabber: GrabberTab):
        super().__init__(parent)
        self.time_mgr = time_mgr
        self.sender = WeChatSender()
        self.grabber = grabber
        self._running = False
        self._results: list[float] = []
        self._recommended_ms = 0
        self._wechat_ports: list[int] = []
        self._capture: PacketCapture | None = None
        self._build_ui()
        self._detect_ports()
        self._tick()

    def _build_ui(self):
        pad = 10
        tf = ttk.LabelFrame(self, text="北京时间"); tf.pack(fill="x", padx=pad, pady=(pad, 5))
        self._clock = tk.Label(tf, text="00:00:00.000", font=("Consolas", 20, "bold")); self._clock.pack(pady=6)

        settings = ttk.LabelFrame(self, text="测试设置"); settings.pack(fill="x", padx=pad, pady=5)
        r = ttk.Frame(settings); r.pack(fill="x", padx=8, pady=4)
        ttk.Label(r, text="抓包端口：").pack(side="left")
        self._port_label = tk.Label(r, text="检测中…", fg="gray"); self._port_label.pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="重新检测", command=self._detect_ports).pack(side="right")

        r = ttk.Frame(settings); r.pack(fill="x", padx=8, pady=4)
        self._rounds_var = tk.StringVar(value="10"); self._drop_var = tk.StringVar(value="2"); self._msg_var = tk.StringVar(value="测")
        ttk.Label(r, text="测试轮数：").pack(side="left"); ttk.Entry(r, textvariable=self._rounds_var, width=5).pack(side="left")
        ttk.Label(r, text="   丢弃前").pack(side="left"); ttk.Entry(r, textvariable=self._drop_var, width=4).pack(side="left"); ttk.Label(r, text="轮").pack(side="left")
        ttk.Label(r, text="   测试消息：").pack(side="left"); ttk.Entry(r, textvariable=self._msg_var, width=8).pack(side="left")

        sf = ttk.LabelFrame(self, text="运行状态"); sf.pack(fill="x", padx=pad, pady=5)
        self._status = tk.Label(sf, text="准备就绪", fg="#007700"); self._status.pack(pady=5)
        self._countdown = tk.Label(sf, text="--", font=("Consolas", 16, "bold")); self._countdown.pack(pady=(0, 5))
        admin_text = "✓ 当前已是管理员权限" if is_admin() else "抓包时会请求一次管理员权限（不安装任何驱动/软件）"
        tk.Label(sf, text=admin_text, fg="green" if is_admin() else "#666").pack(pady=(0, 5))

        bf = ttk.Frame(self); bf.pack(fill="x", padx=pad, pady=7)
        self._start_btn = ttk.Button(bf, text="▶  开始 PktMon 抓包测试", command=self._start); self._start_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._stop_btn = ttk.Button(bf, text="■ 停止", command=self._stop, state="disabled"); self._stop_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))

        rf = ttk.LabelFrame(self, text="检测记录 & 结论"); rf.pack(fill="both", expand=True, padx=pad, pady=5)
        self._result_text = tk.Text(rf, height=12, bg="#fafafa"); self._result_text.pack(fill="both", expand=True, padx=5, pady=5)
        self._conclusion = tk.Label(rf, text="", fg="#007700", font=("", 11, "bold")); self._conclusion.pack(pady=(0, 5))

        br = ttk.Frame(self); br.pack(fill="x", padx=pad, pady=(0, pad))
        self._copy_btn = ttk.Button(br, text="📋 复制建议值", command=self._copy_result, state="disabled"); self._copy_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._apply_btn = ttk.Button(br, text="→ 应用到抢图页", command=self._apply, state="disabled"); self._apply_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))

    def _tick(self):
        dt = self.time_mgr.now_datetime(); self._clock.config(text=dt.strftime("%H:%M:%S.") + f"{dt.microsecond//1000:03d}")
        self.after(20, self._tick)

    def _detect_ports(self):
        self._wechat_ports = get_wechat_ports()
        if self._wechat_ports:
            self._port_label.config(text=", ".join(map(str, self._wechat_ports)), fg="green")
        else:
            self._port_label.config(text="未检测到（请打开并登录 Windows 微信/Weixin）", fg="red")

    def _start(self):
        if not is_admin():
            if messagebox.askyesno("需要管理员权限", "Windows 自带 PktMon 抓包需要管理员权限。\n\n不会安装 Wireshark、Npcap、WinDump 或任何驱动。\n\n现在以管理员身份重新打开到“校准”页？"):
                if relaunch_as_admin_calibrate():
                    self.master.winfo_toplevel().after(300, self.master.winfo_toplevel().destroy)
            return
        self._detect_ports()
        if not self._wechat_ports:
            messagebox.showwarning("提示", "未检测到微信连接，请打开微信、登录，并让微信保持联网后重试")
            return
        try:
            self._total = max(3, int(self._rounds_var.get())); self._drop = max(0, int(self._drop_var.get()))
        except ValueError:
            messagebox.showwarning("提示", "测试轮数格式错误"); return
        self._results = []; self._recommended_ms = 0; self._running = True
        self._result_text.delete("1.0", "end")
        self._result_text.insert("end", f"抓包测试 {self._total} 轮（前 {self._drop} 轮热身）\n监控端口: {', '.join(map(str, self._wechat_ports))}\n抓包后端: Windows 内置 PktMon\n\n")
        self._start_btn.config(state="disabled"); self._stop_btn.config(state="normal")
        self._copy_btn.config(state="disabled"); self._apply_btn.config(state="disabled")
        try:
            self._capture = PacketCapture(self._wechat_ports); self._capture.start()
        except Exception as e:
            self._running = False; self._start_btn.config(state="normal"); self._stop_btn.config(state="disabled")
            messagebox.showerror("启动抓包失败", str(e)); return
        threading.Thread(target=self._run_rounds, daemon=True).start()

    def _run_rounds(self):
        msg = self._msg_var.get().strip() or "测"
        for r in range(1, self._total + 1):
            if not self._running: break
            self.after(0, lambda r=r: self._status.config(text=f"第 {r}/{self._total} 轮", fg="#007700"))
            target_go = time.time() + 3.0
            while self._running and time.time() < target_go:
                rem = target_go - time.time()
                self.after(0, lambda rem=rem: self._countdown.config(text=f"{max(0, rem):.2f}s", fg="#cc6600"))
                time.sleep(0.05)
            if not self._running: break
            try:
                self.sender.prepare(msg); time.sleep(0.15); self.sender.paste_only(); time.sleep(0.08)
                fire_time = time.time()  # same OS wall clock as PktMon timestamp
                self.sender.enter_only()
            except Exception as e:
                self._append(f"#{r:02d}: 发送失败: {e}\n"); continue
            self.after(0, lambda r=r: self._status.config(text=f"第 {r}/{self._total} 轮 — 等待网络包…", fg="#cc6600"))
            delay_ms, _line = self._capture.first_packet_after(fire_time, 1.5) if self._capture else (None, None)
            if delay_ms is not None:
                self._results.append(delay_ms)
                tag = "计入" if r > self._drop else "热身"
                self._append(f"#{r:02d}: {delay_ms:6.1f}ms  ← {tag}\n")
            else:
                self._append(f"#{r:02d}: 未找到发送包\n")
            time.sleep(1.0)
        if self._capture: self._capture.stop()
        if self._running: self._show_summary()
        self._running = False
        self.after(0, lambda: (self._start_btn.config(state="normal"), self._stop_btn.config(state="disabled")))

    def _show_summary(self):
        valid = self._results[self._drop:] if len(self._results) > self._drop else []
        if len(valid) < 2:
            self.after(0, lambda: self._conclusion.config(text="有效数据不足，请增加轮数或重新检测微信端口")); return
        avg = statistics.mean(valid); stdev = statistics.stdev(valid) if len(valid) > 1 else 0.0
        mn, mx = min(valid), max(valid); sr = sorted(valid)
        p80_idx = min(len(sr) - 1, max(0, int(round(0.8 * len(sr) + 0.4999)) - 1))
        p80 = sr[p80_idx]
        # Keep old program's idea: use P80 as a conservative advance suggestion.
        self._recommended_ms = max(0, int(round(p80 / 10.0) * 10))
        text = ("\n────────── 抓包统计 ──────────\n"
                f"有效轮数: {len(valid)}  丢弃前 {self._drop} 轮\n"
                f"平均: {avg:.1f}ms  标准差: {stdev:.1f}ms\n"
                f"最小: {mn:.1f}ms  最大: {mx:.1f}ms\n"
                f"P80:  {p80:.1f}ms\n"
                "──────────────────────────────\n")
        self._append(text)
        def upd():
            self._conclusion.config(text=f"🎯 建议「提前发射」= {self._recommended_ms} ms")
            self._status.config(text="✅ 校准完成！", fg="#007700")
            self._copy_btn.config(state="normal"); self._apply_btn.config(state="normal")
        self.after(0, upd)

    def _stop(self):
        self._running = False
        if self._capture: self._capture.stop()
        self._status.config(text="已停止", fg="gray")
        self._start_btn.config(state="normal"); self._stop_btn.config(state="disabled")

    def _copy_result(self):
        _win_copy_clipboard(str(self._recommended_ms)); self._conclusion.config(text=f"✓ 已复制：{self._recommended_ms} ms")

    def _apply(self):
        self.grabber._pre_fire_var.set(str(self._recommended_ms))
        self._conclusion.config(text=f"✓ 已应用到抢图页：{self._recommended_ms} ms")

    def _append(self, text: str):
        self.after(0, lambda: (self._result_text.insert("end", text), self._result_text.see("end")))


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_NAME)
        root.geometry("600x790")
        root.minsize(540, 700)
        root.attributes("-topmost", True)

        self.time_mgr = TimeManager()
        title = ttk.Frame(root); title.pack(fill="x", padx=12, pady=(10, 3))
        ttk.Label(title, text="🕐 微信抢图助手", font=("", 18, "bold")).pack(side="left")
        tk.Label(title, text="🪟 Windows · 单EXE免安装", fg="gray").pack(side="right")

        nb = ttk.Notebook(root); nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.grabber = GrabberTab(nb, self.time_mgr)
        self.calibrate = CalibrateTab(nb, self.time_mgr, self.grabber)
        nb.add(self.grabber, text="🎯 抢图")
        nb.add(self.calibrate, text="🔧 校准")
        if "--calibrate" in sys.argv:
            nb.select(self.calibrate)

        self.time_mgr.start_auto_sync()
        root.protocol("WM_DELETE_WINDOW", self._close)

    def _close(self):
        self.time_mgr.stop_auto_sync()
        try:
            self.calibrate._stop()
        except Exception:
            pass
        self.root.destroy()


def main():
    if SYSTEM != "Windows":
        print("This build is intended for Windows.")
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
