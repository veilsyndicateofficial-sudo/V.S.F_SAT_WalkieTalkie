#!/usr/bin/env python3
import argparse
import base64
import datetime
import hashlib
import itertools
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

TOOL_NAME = "V.S.F_SAT_WalkieTalkie ©"
COPYRIGHT_TEXT = "All rights reserved by V.S.F ©"

DEFAULT_FREQ = "146.520"
CONFIG_PATH = os.path.expanduser("~/.vsf_rf_walkie_config.json")
LOG_DIR = os.path.expanduser("~/.vsf_rf_walkie_logs")

# ANSI Color & Style Palette
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"


def now_ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def clear_screen():
    try:
        os.system("cls" if os.name == "nt" else "clear")
    except Exception:
        pass


def has_termux_audio() -> bool:
    return bool(shutil.which("termux-microphone-record") and shutil.which("termux-media-player"))


def normalize_freq(raw_freq: str) -> str:
    cleaned = "".join(ch for ch in (raw_freq or "").strip() if ch.isdigit() or ch == ".")
    if not cleaned:
        return DEFAULT_FREQ
    try:
        val = float(cleaned)
        if val <= 0:
            return DEFAULT_FREQ
        return f"{val:.3f}"
    except ValueError:
        return DEFAULT_FREQ


def tune_frequency(freq_str: str) -> tuple[str, int]:
    """Derive a deterministic multicast IPv4 group (239.x.y.z) and UDP port from frequency."""
    norm = normalize_freq(freq_str)
    digest = hashlib.sha256(f"VSF-RF::{norm}".encode("utf-8")).digest()
    b2 = 1 + (digest[0] % 250)
    b3 = digest[1]
    b4 = 1 + (digest[2] % 250)
    mcast_group = f"239.{b2}.{b3}.{b4}"
    udp_port = 40000 + ((digest[3] << 8 | digest[4]) % 20000)
    return mcast_group, udp_port


def squelch_digest(squelch_code: str) -> str:
    clean = (squelch_code or "").strip()
    if not clean:
        return ""
    return hashlib.sha256(f"VSF-SQL::{clean}".encode("utf-8")).hexdigest()


def load_config() -> dict:
    cfg = {
        "callsign": "RF-UNIT-01",
        "freq": DEFAULT_FREQ,
        "squelch": "",
        "auto_reply": False,
        "auto_reply_text": "Copy that. Automated RF beacon active.",
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for k in cfg:
                    if k in saved:
                        cfg[k] = saved[k]
        except Exception:
            pass
    return cfg


def save_config(cfg: dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def log_event(line: str):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        day = datetime.datetime.now().strftime("%Y-%m-%d")
        p = os.path.join(LOG_DIR, f"rf_session_{day}.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now().isoformat()}] {line}\n")
    except Exception:
        pass


class TunerSpinner:
    def __init__(self, label: str, color: str = CYAN):
        self.label = label
        self.color = color
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        for ch in itertools.cycle(frames):
            if self._stop.is_set():
                break
            try:
                sys.stdout.write(f"\r  {self.color}{BOLD}{ch}{RESET} {self.label}   ")
                sys.stdout.flush()
            except Exception:
                break
            time.sleep(0.08)

    def start(self):
        try:
            self._thread.start()
        except Exception:
            pass

    def stop(self, ok: bool = True, note: str = "Locked"):
        self._stop.set()
        try:
            self._thread.join(timeout=0.25)
        except Exception:
            pass
        badge = f"{GREEN}{BOLD}[LOCKED]{RESET}" if ok else f"{YELLOW}{BOLD}[FALLBACK]{RESET}"
        col = GREEN if ok else YELLOW
        try:
            sys.stdout.write(f"\r  {badge} {self.label} -> {col}{BOLD}{note}{RESET}          \n")
            sys.stdout.flush()
        except Exception:
            pass


def animate_ptt_carrier(callsign: str, freq: str):
    bars = ["▁", "▂", "▃", "▄", "▅", "▆", "▇", "█", "▇", "▅", "▃", "▂"]
    try:
        for i in range(8):
            meter = "".join(bars[(i + j) % len(bars)] for j in range(12))
            sys.stdout.write(f"\r  {GREEN}{BOLD}PTT CARRIER [{meter}]{RESET} {YELLOW}{freq} MHz{RESET} {DIM}({callsign}){RESET}")
            sys.stdout.flush()
            time.sleep(0.035)
        sys.stdout.write("\r" + " " * 78 + "\r")
        sys.stdout.flush()
    except Exception:
        pass


def animate_mic_tx(seconds: int):
    for s in range(1, seconds + 1):
        pct = int((s / seconds) * 100)
        filled = int((s / seconds) * 20)
        bar = f"{RED}{'█' * filled}{DIM}{'░' * (20 - filled)}{RESET}"
        try:
            sys.stdout.write(f"\r  {RED}{BOLD}● PTT MIC TX{RESET} [{bar}] {BOLD}{pct:3d}%{RESET} ({s}/{seconds}s)")
            sys.stdout.flush()
        except Exception:
            pass
        time.sleep(1.0)
    print()


def typewriter(text: str, delay: float = 0.004):
    try:
        for ch in text:
            sys.stdout.write(ch)
            sys.stdout.flush()
            time.sleep(delay)
        print()
    except Exception:
        print(text)


def render_tactical_hud(
    callsign: str,
    freq: str,
    mcast_ip: str,
    port: int,
    tx_cnt: int = 0,
    rx_cnt: int = 0,
    squelch_on: bool = False,
    auto_reply: bool = False,
    peers_count: int = 0,
    bus_mode: str = "MCAST+BCAST",
):
    audio_tag = f"{GREEN}MIC-READY{RESET}" if has_termux_audio() else f"{YELLOW}TEXT-PTT{RESET}"
    sql_tag = f"{MAGENTA}SQL-LOCK{RESET}" if squelch_on else f"{DIM}OPEN{RESET}"
    ar_tag = f"{CYAN}AUTO-ON{RESET}" if auto_reply else f"{DIM}OFF{RESET}"

    print(f"""
{CYAN}{BOLD}╔════════════════════════════════════════════════════════════════════╗
║  ██╗   ██╗███████╗███████╗     ██████╗  █████╗ ████████╗    ██████╗  ║
║  ██║   ██║██╔════╝██╔════╝    ██╔════╝ ██╔══██╗╚══██╔══╝    ██╔══██╗ ║
║  ██║   ██║███████╗█████╗      ╚█████╗  ███████║   ██║       ██████╔╝ ║
║  ╚██╗ ██╔╝╚════██║██╔══╝       ╚═══██╗ ██╔══██║   ██║       ██╔═══╝  ║
║   ╚████╔╝ ███████║██╗         ██████╔╝ ██║  ██║   ██║       ██║      ║
║    ╚═══╝  ╚══════╝╚═╝         ╚═════╝  ╚═╝  ╚═╝   ╚═╝       ╚═╝      ║
║                        {MAGENTA}{BOLD}{TOOL_NAME}{CYAN}                        ║
║                   {DIM}{YELLOW}{COPYRIGHT_TEXT}{RESET}{CYAN}{BOLD}                    ║
╠════════════════════════════════════════════════════════════════════╣
║  FREQ     : {YELLOW}{BOLD}{freq:<11} MHz{RESET}{CYAN}{BOLD} │ CALLSIGN : {WHITE}{BOLD}{callsign:<19}{RESET}{CYAN}{BOLD} ║
║  RF BUS   : {GREEN}{bus_mode:<14}{RESET}{CYAN}{BOLD} │ SQUELCH  : {sql_tag:<18} ║
║  CHANNEL  : {DIM}{mcast_ip}:{port:<12}{RESET}{CYAN}{BOLD}│ STATIONS : {GREEN}{BOLD}{peers_count:<2d} ACTIVE{RESET}{CYAN}{BOLD} │ {audio_tag:<9} ║
║  RESPONDER: {ar_tag:<14} │ TX PKTS  : {GREEN}{BOLD}{tx_cnt:<4d}{RESET}{CYAN}{BOLD} │ RX PKTS  : {CYAN}{BOLD}{rx_cnt:<4d}{RESET}{CYAN}{BOLD} ║
╚════════════════════════════════════════════════════════════════════╝{RESET}""")


def record_voice_clip(duration_sec: int = 4):
    if not has_termux_audio():
        return None
    fd, tmp_path = tempfile.mkstemp(prefix="vsf_rf_tx_", suffix=".m4a")
    os.close(fd)
    try:
        subprocess.run(
            ["termux-microphone-record", "-f", tmp_path, "-l", str(duration_sec)],
            check=False,
            capture_output=True,
        )
        animate_mic_tx(duration_sec)
        subprocess.run(["termux-microphone-record", "-q"], check=False, capture_output=True)
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            with open(tmp_path, "rb") as f:
                return f.read()
    except Exception:
        return None
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
    return None


def play_voice_clip(audio_bytes: bytes):
    if not audio_bytes or not has_termux_audio():
        return
    fd, tmp_path = tempfile.mkstemp(prefix="vsf_rf_rx_", suffix=".m4a")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as f:
            f.write(audio_bytes)
        subprocess.run(["termux-media-player", "play", tmp_path], check=False, capture_output=True)
    except Exception:
        pass
    finally:
        threading.Timer(6.0, lambda: os.path.exists(tmp_path) and os.remove(tmp_path)).start()


# ---------------- 0-ERROR RESILIENT FREQUENCY WALKIE BOT ----------------

class FrequencyWalkieBot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.callsign = str(cfg.get("callsign") or "RF-UNIT-01").strip()[:24]
        self.freq = normalize_freq(cfg.get("freq") or DEFAULT_FREQ)
        self.squelch = str(cfg.get("squelch") or "").strip()
        self.auto_reply = bool(cfg.get("auto_reply", False))
        self.auto_reply_text = str(cfg.get("auto_reply_text") or "Copy that. Automated RF beacon active.")

        self.mcast_ip, self.port = tune_frequency(self.freq)
        self.bus_mode = "BCAST+LOOP"
        self.rx_sock = None
        self.tx_sock = None
        self.running = True
        self.tx_count = 0
        self.rx_count = 0
        self.last_auto_ts = 0.0
        self.seen_peers: dict[str, float] = {}
        self.recent_ids: list[str] = []
        self.io_lock = threading.Lock()
        self.sock_lock = threading.Lock()

    def _is_duplicate(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        if msg_id in self.recent_ids:
            return True
        self.recent_ids.append(msg_id)
        if len(self.recent_ids) > 120:
            self.recent_ids = self.recent_ids[-60:]
        return False

    def _active_peer_count(self) -> int:
        now = time.monotonic()
        stale = [k for k, t in self.seen_peers.items() if now - t > 45.0]
        for k in stale:
            self.seen_peers.pop(k, None)
        return len(self.seen_peers)

    def _print_async(self, msg: str):
        with self.io_lock:
            try:
                sys.stdout.write("\r" + " " * 78 + "\r")
                print(msg)
                sys.stdout.write(f"  {YELLOW}{BOLD}RF[{self.freq} MHz]>{RESET} ")
                sys.stdout.flush()
            except Exception:
                pass

    def _close_sockets(self):
        with self.sock_lock:
            for s in (self.rx_sock, self.tx_sock):
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass
            self.rx_sock = None
            self.tx_sock = None

    def _create_rx_socket(self, port: int, mcast_ip: str):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except Exception:
                pass
        try:
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass

        bound = False
        for bind_addr in ("0.0.0.0", "127.0.0.1", ""):
            try:
                rx.bind((bind_addr, port))
                bound = True
                break
            except Exception:
                continue

        if not bound:
            # Fallback to ephemeral port if port is blocked
            rx.bind(("127.0.0.1", 0))
            port = rx.getsockname()[1]

        mcast_joined = False
        try:
            mreq = struct.pack("4s4s", socket.inet_aton(mcast_ip), socket.inet_aton("0.0.0.0"))
            rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            mcast_joined = True
        except Exception:
            mcast_joined = False

        rx.settimeout(0.8)
        mode_str = "MCAST+BCAST" if mcast_joined else "BCAST+LOOP"
        return rx, port, mode_str

    def _create_tx_socket(self):
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass
        try:
            tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 2))
        except Exception:
            pass
        try:
            tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        except Exception:
            pass
        return tx

    def tune(self, new_freq: str, show_spinner: bool = True):
        clean_f = normalize_freq(new_freq)
        mcast_ip, base_port = tune_frequency(clean_f)

        sp = TunerSpinner(f"Tuning RF transceiver to {clean_f} MHz", color=YELLOW) if show_spinner else None
        if sp:
            sp.start()

        self._close_sockets()
        self.freq = clean_f
        self.mcast_ip = mcast_ip
        self.seen_peers.clear()

        try:
            rx, actual_port, mode_str = self._create_rx_socket(base_port, mcast_ip)
            tx = self._create_tx_socket()
            self.port = actual_port
            self.bus_mode = mode_str

            with self.sock_lock:
                self.rx_sock = rx
                self.tx_sock = tx

            if sp:
                sp.stop(True, f"{mode_str} @ {self.port}")
            self.cfg["freq"] = self.freq
            save_config(self.cfg)
            log_event(f"TUNED {self.callsign} -> {self.freq} MHz ({self.bus_mode}:{self.port})")
            self._broadcast_raw({
                "type": "beacon",
                "id": f"{self.callsign}-{time.monotonic_ns()}",
                "from": self.callsign,
                "freq": self.freq,
                "sql": squelch_digest(self.squelch),
            })
        except Exception as exc:
            self.bus_mode = "SAFE-STANDBY"
            if sp:
                sp.stop(False, f"Recovered safely ({exc})")

    def _broadcast_raw(self, payload: dict):
        with self.sock_lock:
            tx = self.tx_sock
        if not tx:
            return
        try:
            raw = json.dumps(payload).encode("utf-8")[:62000]
        except Exception:
            return

        targets = [
            (self.mcast_ip, self.port),
            ("255.255.255.255", self.port),
            ("127.0.0.1", self.port),
        ]
        for host_t, port_t in targets:
            try:
                tx.sendto(raw, (host_t, port_t))
            except Exception:
                continue

    def _maybe_auto_reply(self, sender: str, text: str):
        if not self.auto_reply or not text:
            return
        if "[AUTO-RF]" in text or "[RF-PING" in text:
            return
        now = time.monotonic()
        if now - self.last_auto_ts < 5.0:
            return
        self.last_auto_ts = now
        pkt = {
            "type": "ptt",
            "id": f"{self.callsign}-ar-{time.monotonic_ns()}",
            "mode": "text",
            "from": self.callsign,
            "freq": self.freq,
            "ts": now_ts(),
            "sql": squelch_digest(self.squelch),
            "text": f"[AUTO-RF -> {sender}] {self.auto_reply_text}",
        }
        self._broadcast_raw(pkt)
        self.tx_count += 1
        self._print_async(f"  {MAGENTA}[{now_ts()}] ↻ RF AUTO-REPLY{RESET} -> {sender}: {self.auto_reply_text}")

    def _rx_loop(self):
        last_beacon = 0.0
        while self.running:
            now = time.monotonic()
            if now - last_beacon > 12.0:
                last_beacon = now
                self._broadcast_raw({
                    "type": "beacon",
                    "id": f"{self.callsign}-b-{time.monotonic_ns()}",
                    "from": self.callsign,
                    "freq": self.freq,
                    "sql": squelch_digest(self.squelch),
                })

            with self.sock_lock:
                cur_rx = self.rx_sock
            if not cur_rx:
                time.sleep(0.25)
                continue

            try:
                data, _addr = cur_rx.recvfrom(65535)
                pkt = json.loads(data.decode("utf-8", errors="ignore"))
            except socket.timeout:
                continue
            except Exception:
                if self.running:
                    time.sleep(0.15)
                continue

            if not isinstance(pkt, dict):
                continue

            sender = str(pkt.get("from", "UNKNOWN"))
            if sender == self.callsign:
                continue

            if str(pkt.get("freq", "")) != self.freq:
                continue

            expected_sql = squelch_digest(self.squelch)
            if expected_sql and str(pkt.get("sql", "")) != expected_sql:
                continue

            msg_id = str(pkt.get("id", ""))
            if self._is_duplicate(msg_id):
                continue

            self.seen_peers[sender] = time.monotonic()
            ptype = pkt.get("type")

            if ptype == "beacon":
                continue
            elif ptype == "ptt":
                self.rx_count += 1
                ts = str(pkt.get("ts") or now_ts())
                mode = pkt.get("mode", "text")
                if mode == "voice" and pkt.get("audio_b64"):
                    self._print_async(
                        f"  {CYAN}{BOLD}[{ts}] ◀ RX VOICE [{self.freq} MHz]{RESET} from {BOLD}{sender}{RESET} {DIM}(playing clip){RESET}"
                    )
                    log_event(f"RX_VOICE {sender} @ {self.freq}MHz")
                    try:
                        audio_bytes = base64.b64decode(pkt["audio_b64"])
                        threading.Thread(target=play_voice_clip, args=(audio_bytes,), daemon=True).start()
                    except Exception:
                        pass
                else:
                    txt = str(pkt.get("text", ""))
                    self._print_async(
                        f"  {CYAN}{BOLD}[{ts}] ◀ RX PTT [{self.freq} MHz]{RESET} {BOLD}{sender}{RESET}: {WHITE}{txt}{RESET}"
                    )
                    log_event(f"RX_TEXT {sender} @ {self.freq}MHz: {txt}")
                    self._maybe_auto_reply(sender, txt)

    def send_text(self, text: str):
        animate_ptt_carrier(self.callsign, self.freq)
        pkt = {
            "type": "ptt",
            "id": f"{self.callsign}-{time.monotonic_ns()}",
            "mode": "text",
            "from": self.callsign,
            "freq": self.freq,
            "ts": now_ts(),
            "sql": squelch_digest(self.squelch),
            "text": text,
        }
        self._broadcast_raw(pkt)
        self.tx_count += 1
        log_event(f"TX_TEXT {self.callsign} @ {self.freq}MHz: {text}")
        print(f"  {GREEN}[{now_ts()}] ▶ PTT SENT{RESET} {DIM}({self.freq} MHz | #{self.tx_count}){RESET}: {text}")

    def send_voice(self, seconds: int = 4):
        if not has_termux_audio():
            print(f"  {YELLOW}[{now_ts()}] [!]{RESET} Termux audio tools not detected. Install termux-api or send text PTT.")
            return
        clip = record_voice_clip(seconds)
        if not clip:
            print(f"  {RED}[{now_ts()}] [!]{RESET} No audio recorded.")
            return
        animate_ptt_carrier(self.callsign, self.freq)
        pkt = {
            "type": "ptt",
            "id": f"{self.callsign}-v-{time.monotonic_ns()}",
            "mode": "voice",
            "from": self.callsign,
            "freq": self.freq,
            "ts": now_ts(),
            "sql": squelch_digest(self.squelch),
            "audio_b64": base64.b64encode(clip).decode("ascii"),
        }
        self._broadcast_raw(pkt)
        self.tx_count += 1
        log_event(f"TX_VOICE {self.callsign} @ {self.freq}MHz ({len(clip)}B)")
        print(f"  {GREEN}[{now_ts()}] ▶ VOICE PTT SENT{RESET} ({len(clip)} bytes @ {self.freq} MHz)")

    def scan_preset_frequencies(self):
        presets = ["146.520", "146.940", "433.500", "446.006", "462.562"]
        orig_freq = self.freq
        print(f"\n  {CYAN}{BOLD}--- FREQUENCY SCANNER ---{RESET}")
        for f_val in presets:
            m_ip, p_num = tune_frequency(f_val)
            sp = TunerSpinner(f"Listening {f_val} MHz", color=YELLOW)
            sp.start()
            hits = []
            probe = None
            try:
                probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("0.0.0.0", p_num))
                try:
                    mreq = struct.pack("4s4s", socket.inet_aton(m_ip), socket.inet_aton("0.0.0.0"))
                    probe.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                except Exception:
                    pass
                probe.settimeout(0.35)
                deadline = time.monotonic() + 0.65
                while time.monotonic() < deadline:
                    try:
                        raw, _ = probe.recvfrom(4096)
                        obj = json.loads(raw.decode("utf-8", errors="ignore"))
                        if isinstance(obj, dict) and obj.get("from") and obj.get("from") != self.callsign:
                            hits.append(str(obj.get("from")))
                    except socket.timeout:
                        continue
                    except Exception:
                        break
            except Exception:
                pass
            finally:
                if probe:
                    try:
                        probe.close()
                    except Exception:
                        pass

            if hits:
                sp.stop(True, f"Active: {', '.join(sorted(set(hits)))}")
            else:
                sp.stop(False, "Quiet")

        self.tune(orig_freq, show_spinner=False)
        print(f"  {GREEN}[✔]{RESET} Returned to {BOLD}{self.freq} MHz{RESET}\n")

    def show_panel(self):
        print(f"""
  {CYAN}{BOLD}┌────────────────── SERVERLESS RF WALKIE BOT PANEL ──────────────────┐{RESET}
    {GREEN}[1] talk <msg>{RESET}   - Transmit PTT text on tuned frequency (no server/API)
    {GREEN}[2] voice [sec]{RESET}  - Record & transmit voice clip (1-15s, Termux API)
    {GREEN}[3] freq <mhz>{RESET}   - Tune to a new frequency (e.g. freq 433.500)
    {GREEN}[4] scan{RESET}         - Scan common walkie frequencies for active stations
    {GREEN}[5] beep{RESET}         - Send Roger/Ping tone on tuned frequency
    {GREEN}[6] auto{RESET}         - Toggle Bot Auto-Responder mode
    {GREEN}[7] sql <pin|off>{RESET}- Set/clear CTCSS/Squelch privacy code
    {BLUE}[8] peers / hud{RESET}  - Show active stations or redraw HUD
    {RED}[0] exit{RESET}         - Power down walkie bot
  {DIM}  Tip: Type any text directly to transmit PTT immediately.{RESET}
  {CYAN}{BOLD}└────────────────────────────────────────────────────────────────────┘{RESET}
""")

    def redraw(self):
        clear_screen()
        render_tactical_hud(
            callsign=self.callsign,
            freq=self.freq,
            mcast_ip=self.mcast_ip,
            port=self.port,
            tx_cnt=self.tx_count,
            rx_cnt=self.rx_count,
            squelch_on=bool(self.squelch),
            auto_reply=self.auto_reply,
            peers_count=self._active_peer_count(),
            bus_mode=self.bus_mode,
        )
        self.show_panel()

    def run(self):
        self.tune(self.freq, show_spinner=True)
        threading.Thread(target=self._rx_loop, daemon=True).start()
        time.sleep(0.25)
        self.redraw()

        while self.running:
            try:
                line = input(f"  {YELLOW}{BOLD}RF[{self.freq} MHz]>{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue

            low = line.lower()
            if low in ("0", "exit", "quit", "/quit"):
                break
            elif low in ("help", "/help", "?"):
                self.show_panel()
            elif low in ("8", "hud", "clear", "/clear"):
                self.redraw()
            elif low in ("peers", "/peers"):
                cnt = self._active_peer_count()
                names = ", ".join(sorted(self.seen_peers.keys())) if cnt else "None detected yet"
                print(f"  {BLUE}[i]{RESET} Active stations on {self.freq} MHz ({cnt}): {names}")
            elif low in ("5", "beep", "/beep"):
                self.send_text("[RF-PING // ROGER BEEP]")
            elif low in ("6", "auto", "/auto"):
                self.auto_reply = not self.auto_reply
                self.cfg["auto_reply"] = self.auto_reply
                save_config(self.cfg)
                st = f"{GREEN}ACTIVE{RESET}" if self.auto_reply else f"{YELLOW}OFF{RESET}"
                print(f"  {MAGENTA}[{now_ts()}] [BOT]{RESET} Auto-Responder -> {BOLD}{st}{RESET}")
            elif low in ("4", "scan", "/scan"):
                self.scan_preset_frequencies()
            elif low.startswith(("7 ", "sql ", "/sql ", "sql", "/sql")):
                parts = line.split(None, 1)
                val = parts[1].strip() if len(parts) == 2 else input(f"  {CYAN}Squelch code (or 'off'):{RESET} ").strip()
                self.squelch = "" if val.lower() in ("off", "none", "clear", "0") else val
                self.cfg["squelch"] = self.squelch
                save_config(self.cfg)
                print(f"  {MAGENTA}[{now_ts()}] [SQL]{RESET} Squelch -> {BOLD}{'LOCKED' if self.squelch else 'OPEN'}{RESET}")
            elif low.startswith(("3 ", "freq ", "/freq ", "ch ")):
                parts = line.split(None, 1)
                nf = parts[1].strip() if len(parts) == 2 else input(f"  {CYAN}New Frequency (MHz):{RESET} ").strip()
                if nf:
                    self.tune(nf, show_spinner=True)
            elif low.startswith(("2 ", "voice", "/v")):
                parts = line.split()
                sec = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 4
                self.send_voice(max(1, min(15, sec)))
            elif low.startswith(("1 ", "talk ")):
                parts = line.split(None, 1)
                msg = parts[1].strip() if len(parts) == 2 else input(f"  {CYAN}PTT Message:{RESET} ").strip()
                if msg:
                    self.send_text(msg)
            else:
                self.send_text(line)

        self.running = False
        self._close_sockets()
        log_event(f"OFFLINE {self.callsign}")
        print()
        typewriter(f"  {YELLOW}{COPYRIGHT_TEXT}{RESET} — {TOOL_NAME} powered down.", 0.005)


def main():
    cfg = load_config()
    parser = argparse.ArgumentParser(description=f"{TOOL_NAME} - 0-Error Frequency Walkie Bot")
    parser.add_argument("--callsign", default="", help="Your unit callsign")
    parser.add_argument("--freq", default="", help="Frequency in MHz (e.g. 146.520)")
    args = parser.parse_args()

    clear_screen()
    init_freq = normalize_freq(args.freq or cfg["freq"])
    m_ip, p_num = tune_frequency(init_freq)
    render_tactical_hud(
        callsign=args.callsign or cfg["callsign"],
        freq=init_freq,
        mcast_ip=m_ip,
        port=p_num,
    )

    callsign = args.callsign.strip() or input(f"  {CYAN}Your Callsign{RESET} [{cfg['callsign']}]: ").strip() or cfg["callsign"]
    freq_in = args.freq.strip() or input(f"  {CYAN}Frequency (MHz){RESET} [{cfg['freq']}]: ").strip() or cfg["freq"]

    cfg["callsign"] = callsign
    cfg["freq"] = normalize_freq(freq_in)
    save_config(cfg)

    bot = FrequencyWalkieBot(cfg)
    bot.run()


if __name__ == "__main__":
    main()
