"""
print_agent.py
"""

import base64
import datetime
import logging
import socket
import threading
import time
import sqlite3
import requests
from io import BytesIO
from PIL import Image

# Assuming local path
DB_PATH = "/var/lib/printer_app/printers.db"

def log_job_internal(printer_id: int, printer_name: str, status: str, reason: str = ""):
    try:
        logging.info(f"Logging job: Printer={printer_name}, Status={status}, Reason={reason}")
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO print_logs (printer_id, printer_name, status, reason) VALUES (?, ?, ?, ?)",
            (printer_id, printer_name, status, reason)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error("Failed to log job: %s", e)

import logging
import socket
import threading
from io import BytesIO

import requests
from PIL import Image, ImageDraw, ImageFont

try:
    from escpos.printer import Network
    ESCPOS_AVAILABLE = True
except ImportError:
    ESCPOS_AVAILABLE = False
    logging.warning("python-escpos not installed — printing will be simulated only.")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exceptions — make failure reasons explicit in logs
# ---------------------------------------------------------------------------

class PrinterNotReachableError(IOError):
    """Raised when the network printer does not respond / connection refused."""


class PrinterHardwareError(IOError):
    """Raised for hardware-level errors such as paper-out or cover-open."""


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def imgcrop(im: Image.Image):
    """Slice a tall receipt image into chunks so ESC/POS can handle it."""
    ret = []
    imgwidth, imgheight = im.size
    yPieces = max(1, imgheight // 20)
    height = imgheight // yPieces
    for i in range(yPieces):
        top = i * height
        bottom = imgheight if i == yPieces - 1 else (top + height)
        ret.append(im.crop((0, top, imgwidth, bottom)))
    return ret


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

def generate_test_receipt() -> str:
    """
    Generate a simple test receipt image and return it as base64 string.
    """
    # Create a simple receipt image
    width = 400
    height = 300
    
    # Create image with white background
    img = Image.new('RGB', (width, height), color='white')
    draw = ImageDraw.Draw(img)
    
    # Try to use a default font, fall back to default if not available
    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()
    
    # Draw test receipt content
    y_offset = 20
    
    # Title
    draw.text((width//2, y_offset), "TEST PRINT", fill='black', anchor='mt', font=font_large)
    y_offset += 50
    
    # Separator line
    draw.line([(20, y_offset), (width-20, y_offset)], fill='black', width=2)
    y_offset += 30
    
    # Test information
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    draw.text((30, y_offset), f"Date: {timestamp}", fill='black', font=font_small)
    y_offset += 30
    
    draw.text((30, y_offset), "Printer Test", fill='black', font=font_small)
    y_offset += 30
    
    draw.text((30, y_offset), "Status: OK", fill='black', font=font_small)
    y_offset += 30
    
    # Separator line
    draw.line([(20, y_offset), (width-20, y_offset)], fill='black', width=2)
    y_offset += 30
    
    draw.text((width//2, y_offset), "End of Test", fill='black', anchor='mt', font=font_small)
    
    # Convert to base64
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    img_str = base64.b64encode(buffer.getvalue()).decode('utf-8')
    
    return img_str


def _parse_ip_port(ip_str: str, default_port: int = 9100) -> tuple[str, int]:
    """Parse '192.168.1.100:9101' into ('192.168.1.100', 9101)."""
    if ip_str and ":" in ip_str:
        try:
            ip, port = ip_str.rsplit(":", 1)
            return ip.strip(), int(port)
        except ValueError:
            pass
    return ip_str, default_port


def _validate_host(ip: str) -> tuple[bool, str]:
    parts = ip.split('.')
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        try:
            if not all(0 <= int(part) <= 255 for part in parts):
                return False, "Invalid IP format (octet out of range)"
            return True, ""
        except ValueError:
            return False, "Invalid IP format"
    if not ip or not all(c.isalnum() or c in '.-_' for c in ip):
        return False, "Invalid IP/Hostname"
    return True, ""


# ---------------------------------------------------------------------------
# ESC/POS real-time status decoding (DLE EOT n)
# ---------------------------------------------------------------------------
#
# DLE EOT n response is a single byte where bit 4 is always 0 and bit 0/1 are
# fixed (b0=0, b1=1). Each n returns a different status group:
#   n=1 Printer status
#   n=2 Offline cause
#   n=3 Error cause
#   n=4 Paper roll sensor
# ---------------------------------------------------------------------------

def _is_valid_dle_eot_byte(b: int) -> bool:
    """
    Per ESC/POS spec, every DLE EOT n response byte has a fixed bit pattern:
      bit 0 = 0 (LSB)
      bit 1 = 1
      bit 4 = 1
      bit 7 = 0
    So  (b & 0b10010011) must equal 0b00010010 = 0x12.
    Reject anything else — it's almost certainly noise / leftover print data.
    """
    return (b & 0x93) == 0x12


def _drain(sock: socket.socket, timeout: float = 0.05) -> None:
    """Read any pending bytes (from a previous command, banner, etc.) and discard them."""
    try:
        sock.settimeout(timeout)
        while True:
            chunk = sock.recv(64)
            if not chunk:
                break
    except Exception:
        pass


def _query_dle_eot(sock: socket.socket, n: int, timeout: float = 1.5) -> int | None:
    """
    Send DLE EOT n and read back one *validated* status byte.
    Loops up to ~10 bytes to skip any leftover garbage in the recv buffer
    before declaring the printer silent. Returns None if no valid byte arrives.
    """
    try:
        _drain(sock)
        sock.settimeout(timeout)
        sock.sendall(bytes([0x10, 0x04, n]))
        deadline = time.time() + timeout
        for _ in range(10):
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            sock.settimeout(max(0.05, remaining))
            try:
                resp = sock.recv(1)
            except socket.timeout:
                return None
            if not resp:
                return None
            b = resp[0]
            if _is_valid_dle_eot_byte(b):
                return b
            # else: junk byte — keep reading
        return None
    except Exception:
        return None


def _decode_status_bytes(s1: int | None, s2: int | None, s3: int | None, s4: int | None) -> tuple[list[str], list[str], dict]:
    """Decode DLE EOT responses into (errors, warnings, flags_dict)."""
    errors: list[str] = []
    warnings: list[str] = []
    flags: dict = {}

    if s1 is not None:
        flags["online"] = not bool(s1 & 0x08)        # bit 3 set => offline
        flags["drawer_open"] = not bool(s1 & 0x04)   # bit 2: drawer kick connector pin (0=high)
        if s1 & 0x20:
            warnings.append("Paper fed by FEED button")
    if s2 is not None:
        if s2 & 0x04:
            errors.append("Cover is open")
            flags["cover_open"] = True
        if s2 & 0x08:
            warnings.append("Paper being fed by FEED button")
        if s2 & 0x20:
            errors.append("Paper roll end (out of paper)")
            flags["paper_out"] = True
        if s2 & 0x40:
            errors.append("Error condition active")
    if s3 is not None:
        if s3 & 0x04:
            errors.append("Mechanical error (cutter / carriage)")
        if s3 & 0x08:
            errors.append("Auto-cutter error")
        if s3 & 0x20:
            errors.append("Unrecoverable error — restart printer")
        if s3 & 0x40:
            errors.append("Auto-recoverable error (overheating / voltage)")
    if s4 is not None:
        near_end = bool(s4 & 0x0C)   # bits 2-3
        end = bool(s4 & 0x60)        # bits 5-6
        if end:
            errors.append("Paper roll sensor: paper END")
            flags["paper_out"] = True
        elif near_end:
            warnings.append("Paper roll near-end (low paper)")
            flags["paper_low"] = True

    flags.setdefault("online", True)
    return errors, warnings, flags


def _network_alive_fallback(ip: str, timeout: float = 1.0) -> str | None:
    """
    Called when port 9100 is unreachable. Probes a small set of standard
    network-printer admin ports to decide whether the printer itself is alive
    (just locked on the print port) or fully offline.

    Returns a short label (e.g. "LPD/515", "HTTP/80") if any port answers,
    otherwise None.
    """
    for p, label in [(515, "LPD/515"),
                     (80,  "HTTP/80"),
                     (443, "HTTPS/443"),
                     (631, "IPP/631"),
                     (9101, "RAW/9101"),
                     (9102, "RAW/9102"),
                     (4000, "RAW/4000")]:
        if _tcp_open(ip, p, timeout=timeout):
            return label
    return None


def query_printer_status(ip: str, port: int = 9100, timeout: float = 3.0) -> dict:
    """
    Open a TCP connection and pull real-time status via DLE EOT n.
    Returns: {reachable, online, errors[], warnings[], flags{}, reason, raw{},
              error_state}.
    """
    result = {
        "reachable": False,
        "online": False,
        "errors": [],
        "warnings": [],
        "flags": {},
        "reason": "",
        "raw": {},
        "error_state": False,   # True = printer is on the network but in fault
    }

    ok, msg = _validate_host(ip)
    if not ok:
        result["reason"] = msg
        return result

    s = None
    connect_error = None
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        result["reachable"] = True

        s1 = _query_dle_eot(s, 1)
        s2 = _query_dle_eot(s, 2)
        s3 = _query_dle_eot(s, 3)
        s4 = _query_dle_eot(s, 4)
        result["raw"] = {"s1": s1, "s2": s2, "s3": s3, "s4": s4}

        errors, warnings, flags = _decode_status_bytes(s1, s2, s3, s4)
        result["errors"] = errors
        result["warnings"] = warnings
        result["flags"] = flags
        result["online"] = flags.get("online", True) and not errors

        if errors:
            result["reason"] = "; ".join(errors)
        elif warnings:
            result["reason"] = "; ".join(warnings)
        elif s1 is None and s2 is None and s3 is None and s4 is None:
            result["reason"] = ("Connected but no ESC/POS status response "
                                "(printer may be busy or non-ESC/POS)")
        return result

    except socket.timeout:
        connect_error = f"Connection timed out to {ip}:{port}"
    except ConnectionRefusedError:
        connect_error = f"Connection refused on {ip}:{port} (port closed)"
    except OSError as e:
        m = e.strerror or str(e) or e.__class__.__name__
        connect_error = f"Host unreachable ({ip}:{port}): {m}"
    except Exception as e:
        connect_error = f"{type(e).__name__}: {e}"
    finally:
        if s:
            try: s.close()
            except: pass

    # ----------------------------------------------------------------------
    # Fallback path — the print port did not respond. Many ESC/POS printers
    # (Epson TM-series in particular) LOCK port 9100 when in an error state
    # (paper out, cover open, queue paused). If the printer answers on any
    # standard admin port, we know the device itself is alive — the problem
    # is at the printer, not the network.
    # ----------------------------------------------------------------------
    alive_on = _network_alive_fallback(ip)
    if alive_on:
        result["reachable"]    = True
        result["error_state"]  = True
        result["online"]       = False
        result["errors"]       = [
            f"Printer in error state — port {port} is locked. "
            f"Common cause: out of paper, cover open, or print queue paused. "
            f"(Device is on the network, responding on {alive_on}.)"
        ]
        result["reason"]       = result["errors"][0]
    else:
        result["reason"] = connect_error or "Unreachable"

    return result


def check_printer_connectivity(printer_ip: str) -> tuple[bool, str]:
    """
    Backwards-compatible wrapper used by app.py /api/status.
    Returns (reachable_and_no_errors, reason).
    """
    ip, port = _parse_ip_port(printer_ip)
    st = query_printer_status(ip, port)
    if not st["reachable"]:
        return False, st["reason"] or "Unreachable"
    if st["errors"]:
        return False, st["reason"]
    if st["warnings"]:
        return True, st["reason"]
    return True, ""


# ---------------------------------------------------------------------------
# ESC/POS GS I n — printer identification (model, firmware, vendor, serial)
# ---------------------------------------------------------------------------

def _read_gs_i_text(sock: socket.socket, n: int, timeout: float = 1.5) -> str | None:
    """
    GS I n  →  variable-length text response.
    Frame format (Epson ESC/POS): 0x5F <data...> 0x00
    Some firmwares omit the 0x5F header; some send extra trailing bytes.
    """
    try:
        _drain(sock)
        sock.settimeout(timeout)
        sock.sendall(bytes([0x1D, 0x49, n]))
        buf = b""
        end = time.time() + timeout
        while time.time() < end:
            try:
                chunk = sock.recv(64)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if b"\x00" in buf:
                break
            if len(buf) > 256:
                break
        if not buf:
            return None
        # Cut at NUL terminator
        buf = buf.split(b"\x00", 1)[0]
        # Strip the standard 0x5F header byte if present
        if buf.startswith(b"\x5F"):
            buf = buf[1:]
        # Decode ASCII, keep only printable chars, drop framing remnants
        text = buf.decode("latin-1", errors="ignore")
        text = "".join(c for c in text if c.isprintable())
        text = text.strip()
        return text or None
    except Exception:
        return None


def _read_gs_i_byte(sock: socket.socket, n: int, timeout: float = 1.2) -> int | None:
    try:
        sock.settimeout(timeout)
        sock.sendall(bytes([0x1D, 0x49, n]))
        resp = sock.recv(1)
        return resp[0] if resp else None
    except Exception:
        return None


def _gs_i_text_oneshot(ip: str, port: int, n: int, timeout: float = 2.0) -> str | None:
    """One fresh TCP connection per GS I n — guarantees response/command pairing
    on cheap ESC/POS clones that don't tag their replies."""
    s = None
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        return _read_gs_i_text(s, n, timeout=timeout)
    except Exception:
        return None
    finally:
        if s:
            try: s.close()
            except: pass


def _gs_i_byte_oneshot(ip: str, port: int, n: int, timeout: float = 1.5) -> int | None:
    s = None
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        return _read_gs_i_byte(s, n, timeout=timeout)
    except Exception:
        return None
    finally:
        if s:
            try: s.close()
            except: pass


def query_printer_info(ip: str, port: int = 9100, timeout: float = 2.0) -> dict:
    """
    Pull every identification field exposed by ESC/POS GS I n.
    Empirically validated mapping across Epson TM-T88VI and Rongta RP80:
        n=65 → manufacturer  ("EPSON", "RONGTA")
        n=66 → model         ("TM-T88VI", "RP80")
        n=67 → firmware      ("40.53A ESC/POS", "2.38")
        n=68 → device code   ("X6XF003010", "2012-05-30")
        n=69 → locale        ("CHINA GB18030")
        n=70 → extras
    Each query uses its own TCP connection so responses can't get mis-paired.
    """
    info = {
        "model_id": None,
        "type_id": None,
        "rom_version": None,
        "manufacturer": None,
        "model": None,
        "firmware": None,
        "language": None,
        "serial": None,
        "additional": None,
        "vendor": None,
    }
    # Sequential one-shot queries. Parallelism overwhelms cheap ESC/POS firmware
    # (Rongta clones drop connections, Epson serves only one TCP slot at a time).
    # Field mapping verified live against Epson TM-T88VI and Rongta RP80:
    #   65 → firmware, 66 → manufacturer, 67 → model,
    #   68 → device code, 69 → language, 70 → extras
    try:
        info["model_id"]     = _gs_i_byte_oneshot(ip, port, 1)
        info["type_id"]      = _gs_i_byte_oneshot(ip, port, 2)
        info["rom_version"]  = _gs_i_byte_oneshot(ip, port, 3)
        info["firmware"]     = _gs_i_text_oneshot(ip, port, 65)
        info["manufacturer"] = _gs_i_text_oneshot(ip, port, 66)
        info["model"]        = _gs_i_text_oneshot(ip, port, 67)
        info["serial"]       = _gs_i_text_oneshot(ip, port, 68)
        info["language"]     = _gs_i_text_oneshot(ip, port, 69)
        info["additional"]   = _gs_i_text_oneshot(ip, port, 70)
        info["vendor"]       = info["manufacturer"]   # legacy alias
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def _reverse_dns(ip: str) -> str | None:
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return None


def _lookup_mac(ip: str) -> str | None:
    """Best-effort MAC lookup via /proc/net/arp (Linux only, no privileges needed)."""
    try:
        import os
        if not os.path.exists("/proc/net/arp"):
            return None
        with open("/proc/net/arp") as f:
            next(f, None)  # skip header
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == ip and parts[3] != "00:00:00:00:00:00":
                    return parts[3].upper()
    except Exception:
        pass
    return None


# Common network-printer ports — scanned in priority order.
# (port, label, is_escpos_candidate)
COMMON_PRINTER_PORTS: list[tuple[int, str, bool]] = [
    (9100, "RAW / JetDirect",     True),
    (9101, "RAW (port 2)",        True),
    (9102, "RAW (port 3)",        True),
    (4000, "RAW (alt)",           True),
    (6101, "RAW (alt)",           True),
    (515,  "LPD / LPR",           False),
    (631,  "IPP (CUPS)",          False),
    (80,   "HTTP admin",          False),
    (443,  "HTTPS admin",         False),
    (8080, "HTTP admin (alt)",    False),
    (21,   "FTP",                 False),
    (23,   "Telnet",              False),
    (22,   "SSH",                 False),
    (9220, "HP scan",             False),
    (9290, "HP status",           False),
    (3910, "WSD print",           False),
    (3911, "WSD print",           False),
    (5358, "WSD",                 False),
]


def _tcp_open(ip: str, port: int, timeout: float = 0.6) -> bool:
    s = None
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        return True
    except Exception:
        return False
    finally:
        if s:
            try: s.close()
            except: pass


def scan_printer_ports(ip: str, timeout: float = 0.6, ports: list[int] | None = None) -> list[dict]:
    """
    Probe every common printer port concurrently. Returns list of:
      [{port, label, open, escpos_candidate}]
    Only ports that are open are returned.
    """
    import concurrent.futures as cf

    candidates = ports or [p for p, _, _ in COMMON_PRINTER_PORTS]
    label_map = {p: (lbl, esc) for p, lbl, esc in COMMON_PRINTER_PORTS}

    open_ports: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=min(16, len(candidates))) as ex:
        futs = {ex.submit(_tcp_open, ip, p, timeout): p for p in candidates}
        for fut in cf.as_completed(futs):
            p = futs[fut]
            try:
                if fut.result():
                    label, esc = label_map.get(p, (f"port {p}", False))
                    open_ports.append({
                        "port": p,
                        "label": label,
                        "open": True,
                        "escpos_candidate": esc,
                    })
            except Exception:
                pass

    open_ports.sort(key=lambda x: (not x["escpos_candidate"], x["port"]))
    return open_ports


def _http_banner(ip: str, port: int = 80, timeout: float = 1.5) -> str | None:
    """Some network printers expose a web UI — grab <title> as a vendor hint."""
    try:
        import re
        resp = requests.get(f"http://{ip}:{port}/", timeout=timeout)
        m = re.search(r"<title>([^<]+)</title>", resp.text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        srv = resp.headers.get("Server")
        if srv:
            return srv
    except Exception:
        return None
    return None


def probe_printer(ip_or_host: str, port: int | None = None) -> dict:
    """
    Comprehensive probe for the add/edit UI.
    Combines connectivity + status + identification + DNS/MAC/HTTP fingerprint.
    """
    ip, parsed_port = _parse_ip_port(ip_or_host)
    if port is None:
        port = parsed_port

    result: dict = {
        "ip": ip,
        "port": port,
        "reachable": False,
        "online": False,
        "error_state": False,
        "errors": [],
        "warnings": [],
        "reason": "",
        "hostname": None,
        "mac": None,
        "vendor": None,
        "model": None,
        "manufacturer": None,
        "firmware": None,
        "language": None,
        "serial": None,
        "model_id": None,
        "type_id": None,
        "rom_version": None,
        "additional": None,
        "web_banner": None,
        "raw_status": {},
        "open_ports": [],
        "tried_ports": [],
        "auto_detected_port": False,
    }

    # 1) Scan every common printer port (fast, parallel).
    open_ports = scan_printer_ports(ip)
    result["open_ports"] = open_ports

    # 2) Try the user-provided port first; if it isn't open or no ESC/POS reply,
    #    fall through to other ESC/POS-candidate ports that *are* open.
    open_set = {p["port"] for p in open_ports}
    candidate_ports: list[int] = []
    if port:
        candidate_ports.append(port)
    for p in open_ports:
        if p["escpos_candidate"] and p["port"] not in candidate_ports:
            candidate_ports.append(p["port"])

    status = None
    chosen_port = port
    for cp in candidate_ports:
        result["tried_ports"].append(cp)
        st = query_printer_status(ip, cp)
        if st["reachable"]:
            status = st
            chosen_port = cp
            break
        # remember the *first* failure reason in case nothing works
        if status is None:
            status = st

    if status is None:
        status = {"reachable": False, "online": False, "errors": [],
                  "warnings": [], "reason": "No common printer port responded",
                  "raw": {}}

    if chosen_port != port:
        result["auto_detected_port"] = True
    result["port"] = chosen_port

    result.update({
        "reachable":  status["reachable"],
        "online":     status["online"],
        "errors":     status["errors"],
        "warnings":   status["warnings"],
        "reason":     status["reason"],
        "raw_status": status["raw"],
    })

    # Only run the heavy GS I identity queries when the ESC/POS port is
    # actually serving requests. If we already know it's locked (error state),
    # there's no point spending 12+ seconds on guaranteed timeouts.
    if status["reachable"] and not status.get("error_state"):
        info = query_printer_info(ip, chosen_port)
        for k in ("vendor", "model", "manufacturer", "firmware", "language",
                  "serial", "model_id", "type_id", "rom_version", "additional"):
            result[k] = info.get(k)

    result["error_state"] = status.get("error_state", False)

    result["hostname"]    = _reverse_dns(ip)
    result["mac"]         = _lookup_mac(ip)
    result["web_banner"]  = _http_banner(ip)

    return result



def print_test(printer_ip: str, port: int | None = None) -> None:
    """
    Generate and print a test receipt.
    """
    test_img_data = generate_test_receipt()
    print_receipt(printer_ip, test_img_data, port=port)


def print_receipt(printer_ip: str, img_data: str, port: int | None = None) -> None:
    """
    Decode base64 image and send to ESC/POS network printer.

    Raises:
        PrinterNotReachableError  – printer did not respond / timed out.
        PrinterHardwareError      – printer returned an error status (paper out, etc.).
        Exception                 – any other unexpected failure.

    The caller must NOT confirm the job unless this function returns without
    raising.
    """
    if not ESCPOS_AVAILABLE:
        # Treat missing library as a hard failure so we don't silently skip jobs.
        raise PrinterHardwareError(
            f"python-escpos is not installed; cannot print to {printer_ip}"
        )

    ip, parsed_port = _parse_ip_port(printer_ip)
    if port is None:
        port = parsed_port
    imgs = imgcrop(Image.open(BytesIO(base64.b64decode(img_data))))

    # --- connect ---------------------------------------------------------
    try:
        printer = Network(ip, port=port, timeout=10)
    except (socket.timeout, socket.error, OSError) as exc:
        raise PrinterNotReachableError(
            f"Cannot connect to printer {ip} on port {port}: {exc}. "
            "Ensure the printer is powered on and reachable from this server."
        ) from exc


    # --- pre-print status checks (Best Effort) ---------------------------
    try:
        # Many printers do not support DLE-EOT status queries over TCP.
        # We attempt them but don't abort unless we get a CLEAR hardware error.
        
        try:
            # 1. Online / ready check
            if not printer.is_online():
                logger.warning("[%s] Printer reported offline status via is_online() — proceeding anyway", printer_ip)

            # 2. Paper / roll check
            #    paper_status() returns: 
            #    2 = adequate (OK)
            #    1 = near-end (Low / Half-roll) -> We warn but print.
            #    0 = out (Empty / Almost end) -> We must STOP.
            paper = printer.paper_status()
            if paper == 0:
                raise PrinterHardwareError(
                    f"Printer {printer_ip}: paper roll is EMPTY or ALMOST END — please replace roll"
                )
            if paper == 1:
                # User reported half-roll gives "near-end", so we only log a warning.
                logger.warning("[%s] Printer paper roll is getting low (near-end)", printer_ip)
        except PrinterHardwareError:
            raise
        except Exception as status_exc:
            # If the status query itself fails (timeout, etc.), we assume the 
            # printer is just "silent" and proceed with the print attempt.
            logger.debug("[%s] Status query failed (%s) — ignoring", printer_ip, status_exc)

    except PrinterHardwareError:
        # Re-raise explicit hardware errors (like paper out)
        try:
            printer.close()
        except:
            pass
        raise
    except Exception as exc:
        logger.warning(
            "[%s] Unexpected error during status check (%s) — proceeding to print",
            printer_ip, exc,
        )

    # --- send data -------------------------------------------------------
    try:
        for img in imgs:
            printer.image(img)
        # Add feed lines before cut to ensure clean cutting
        printer._raw(b'\n\n\n')
        # Ensure all data is sent before cutting
        printer.cut(mode='full')
    except (socket.timeout, socket.error, OSError) as exc:
        # Network disappeared mid-print
        raise PrinterNotReachableError(
            f"Lost connection to printer {printer_ip} during print: {exc}"
        ) from exc
    # Removing the catch-all 'except Exception' block that was causing 
    # false-positive PrinterHardwareErrors on successful prints.
    finally:
        try:
            printer.close()
        except Exception:
            pass



# ---------------------------------------------------------------------------
# Job confirmation
# ---------------------------------------------------------------------------

def confirm_job(odoo_url: str, headers: dict, job_id: int) -> bool:
    """
    Mark a job as done on the server.
    Returns True on success, False if the request failed.
    A failed confirmation does NOT cause the job to be retried — the server
    may or may not re-queue it depending on its own timeout/logic.
    """
    try:
        resp = requests.post(
            f"{odoo_url}/odoo_pos/jobs/{job_id}",
            json={"status": "done"},
            headers=headers,
            timeout=10,
        )
        if not resp.ok:
            logger.warning(
                "Confirm job %s returned HTTP %s", job_id, resp.status_code
            )
            return False
        return True
    except Exception as e:
        logger.error("Failed to confirm job %s: %s", job_id, e)
        return False


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

def poll_printer(printer: dict, settings: dict, stop_event: threading.Event):
    """
    Runs in a dedicated thread.
    printer dict keys: id, name, ip
    settings dict keys: odoo_url, api_key, company_id

    Confirmation policy enforced here:
      - print_receipt() succeeds  → confirm_job()
      - print_receipt() raises    → log the error, do NOT confirm
                                    job stays pending; next poll re-fetches it
    """
    name = printer["name"]
    ip = printer["ip"]
    printer_port = printer.get("port") if isinstance(printer, dict) else None
    odoo_url = settings["odoo_url"].rstrip("/")
    headers = {"Authorization": f"Bearer {settings['api_key']}"}
    company_id = settings["company_id"]

    logging.error(f"DEBUG: poll_printer entry point: name={name}, ip={ip}")
    logger.info("[%s] Configuration: URL=%s, Key=%s, Company=%s",
                name, odoo_url,
                (settings.get('api_key') or 'N/A')[:5] + '...',
                company_id)

    odoo_configured = bool(odoo_url and settings.get('api_key'))
    if not odoo_configured:
        logger.warning("[%s] Odoo URL / API key not configured — running in "
                       "status-only mode (no job polling).", name)

    # Status refresh interval (continuous) vs Odoo job-poll interval.
    STATUS_INTERVAL = 5
    JOB_INTERVAL    = 5

    last_status_ts = 0.0
    last_job_ts    = 0.0

    while not stop_event.is_set():
        now = time.time()

        # ------------------------------------------------------------------
        # 1) Continuous status refresh — runs EVERY iteration, independent
        #    of Odoo configuration. Owned by the poll thread so we never
        #    fight a second TCP connection. Skipped only while a print job
        #    is actively in flight (busy flag handles that).
        # ------------------------------------------------------------------
        if now - last_status_ts >= STATUS_INTERVAL:
            cached = agent_manager.get_status(printer["id"])
            is_busy = cached["busy"] if cached else False
            if not is_busy:
                try:
                    st = query_printer_status(ip, printer_port or 9100)
                    agent_manager.set_status(printer["id"], st, busy=False)
                except Exception as e:
                    logger.debug("[%s] status refresh failed: %s", name, e)
            last_status_ts = now

        # ------------------------------------------------------------------
        # 2) Job polling — only when Odoo is configured.
        # ------------------------------------------------------------------
        if not odoo_configured:
            stop_event.wait(STATUS_INTERVAL)
            continue

        if now - last_job_ts < JOB_INTERVAL:
            stop_event.wait(1)
            continue
        last_job_ts = now

        try:
            logger.debug("[%s] Requesting jobs from %s", name, odoo_url)
            response = requests.get(
                f"{odoo_url}/odoo_pos/jobs",
                json={"printer_ip": ip, "company_id": company_id},
                headers=headers,
                timeout=10,
            )
            logger.debug("[%s] Received response: %s", name, response.status_code)

            if response.status_code == 200:
                result = response.json().get("result", [])
                if result:
                    job = result[0]
                    job_id = job["id"]
                    logger.info("[%s] Attempting job %s", name, job_id)

                    agent_manager.mark_busy(printer["id"], True)
                    try:
                        print_receipt(ip, job["data"], port=printer_port)
                        confirmed = confirm_job(odoo_url, headers, job_id)
                        if confirmed:
                            logger.info("[%s] Job %s printed and confirmed", name, job_id)
                            log_job_internal(printer["id"], name, "success")
                        else:
                            logger.warning("[%s] Job %s printed but confirmation failed", name, job_id)
                            log_job_internal(printer["id"], name, "failed", "Confirmation failed")

                    except PrinterNotReachableError as e:
                        logger.error("[%s] Job %s NOT confirmed — printer unreachable: %s", name, job_id, e)
                        log_job_internal(printer["id"], name, "failed", "Printer unreachable")
                    except PrinterHardwareError as e:
                        logger.error("[%s] Job %s NOT confirmed — printer hardware error: %s", name, job_id, e)
                        log_job_internal(printer["id"], name, "failed", str(e))
                    except Exception as e:
                        logger.error("[%s] Job %s NOT confirmed — unexpected print error: %s", name, job_id, e)
                        log_job_internal(printer["id"], name, "failed", str(e))
                    finally:
                        agent_manager.mark_busy(printer["id"], False)
            else:
                logger.warning("[%s] HTTP %s from source", name, response.status_code)

        except requests.exceptions.Timeout:
            logger.warning("[%s] Fetch timed out — will retry next poll", name)
        except requests.exceptions.ConnectionError as e:
            logger.warning("[%s] Cannot reach source URL: %s — will retry", name, e)
        except Exception as e:
            logger.error("[%s] Poll error: %s", name, type(e).__name__ + ": " + str(e))

        # Short tick — the per-section throttles (STATUS_INTERVAL, JOB_INTERVAL)
        # decide whether each block actually runs this iteration.
        stop_event.wait(1)

    logger.info("[%s] Polling thread stopped", name)


class AgentManager:
    """
    Keeps track of one thread+stop_event per printer (keyed by printer DB id).
    Also caches the most-recent status dict reported by the poll thread so
    /api/status does not race the poll thread for the printer's single TCP slot.
    """

    # Status entries older than this are treated as stale.
    STATUS_TTL = 12.0

    def __init__(self):
        self._threads: dict[int, threading.Thread] = {}
        self._stop_events: dict[int, threading.Event] = {}
        self._status: dict[int, dict] = {}          # pid -> {ts, status_dict, busy}
        self._lock = threading.Lock()

    def set_status(self, printer_id: int, status: dict, busy: bool = False):
        with self._lock:
            self._status[printer_id] = {
                "ts": time.time(),
                "status": status,
                "busy": busy,
            }

    def get_status(self, printer_id: int) -> dict | None:
        with self._lock:
            entry = self._status.get(printer_id)
        if not entry:
            return None
        age = time.time() - entry["ts"]
        return {
            "status": entry["status"],
            "busy":   entry["busy"],
            "age":    age,
            "fresh":  age <= self.STATUS_TTL,
        }

    def mark_busy(self, printer_id: int, busy: bool):
        with self._lock:
            entry = self._status.get(printer_id)
            if entry:
                entry["busy"] = busy
                entry["ts"] = time.time()

    def start(self, printer: dict, settings: dict):
        logging.error("DEBUG: agent_manager.start called for " + str(printer["id"]))
        pid = printer["id"]
        with self._lock:
            self._stop_existing(pid)
            stop_event = threading.Event()
            t = threading.Thread(
                target=poll_printer,
                args=(printer, settings, stop_event),
                name=f"printer-{pid}",
                daemon=True,
            )
            self._threads[pid] = t
            self._stop_events[pid] = stop_event
            t.start()

    def stop(self, printer_id: int):
        with self._lock:
            self._stop_existing(printer_id)

    def restart(self, printer: dict, settings: dict):
        self.start(printer, settings)

    def is_alive(self, printer_id: int) -> bool:
        t = self._threads.get(printer_id)
        return t is not None and t.is_alive()

    def _stop_existing(self, printer_id: int):
        ev = self._stop_events.get(printer_id)
        if ev:
            ev.set()
        t = self._threads.get(printer_id)
        if t and t.is_alive():
            t.join(timeout=8)
        self._threads.pop(printer_id, None)
        self._stop_events.pop(printer_id, None)

    def stop_all(self):
        with self._lock:
            for pid in list(self._threads):
                self._stop_existing(pid)


# Singleton used by app.py
agent_manager = AgentManager()
