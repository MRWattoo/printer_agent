"""
print_agent.py
"""

import base64
import datetime
import logging
import socket
import threading
import sqlite3
import requests
from io import BytesIO
from PIL import Image

# Assuming local path
DB_PATH = "/var/lib/printer_app/printers.db"

def log_job_internal(printer_id: int, printer_name: str, status: str, reason: str = ""):
    try:
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


def _parse_ip_port(ip_str: str) -> tuple[str, int]:
    """Parse '192.168.1.100:9101' into ('192.168.1.100', 9101)."""
    if ":" in ip_str:
        try:
            ip, port = ip_str.split(":", 1)
            return ip, int(port)
        except ValueError:
            pass
    return ip_str, 9100


def check_printer_connectivity(printer_ip: str) -> bool:
    """
    Check if the printer is reachable. 
    Returns True if connected, False otherwise.
    """
    ip, port = _parse_ip_port(printer_ip)

    # 1. Basic IP format validation
    parts = ip.split('.')
    if len(parts) == 4:
        try:
            if not all(0 <= int(part) <= 255 for part in parts):
                 return False
        except ValueError:
            return False
    else:
        if not all(c.isalnum() or c in '.-' for c in ip):
            return False

    # 2. Try raw TCP connection
    s = None
    try:
        # Use a reasonable timeout for the initial connection
        s = socket.create_connection((ip, port), timeout=2.0)
        
        # 3. Best-effort Identity Check
        # We try to see if it responds like a printer, but we DON'T fail 
        # if it's silent, as some printers don't support DLE EOT over TCP.
        try:
            s.settimeout(1.0)
            s.sendall(b'\x10\x04\x01') # DLE EOT 1
            resp = s.recv(1)
            if resp:
                logger.debug("[%s] Verified as ESC/POS printer via DLE EOT", printer_ip)
        except Exception:
            # Silent printer or identity check not supported - this is OK
            logger.debug("[%s] Connected to %s:%s (Silent/No identity response)", printer_ip, ip, port)
        
        return True
            
    except (socket.timeout, socket.error, OSError) as e:
        logger.debug("[%s] Connection failed: %s", printer_ip, e)
        return False
    finally:
        if s:
            try:
                s.close()
            except:
                pass



def print_test(printer_ip: str) -> None:
    """
    Generate and print a test receipt.
    
    Raises:
        PrinterNotReachableError  – printer did not respond / timed out.
        PrinterHardwareError      – printer returned an error status (paper out, etc.).
        Exception                 – any other unexpected failure.
    """
    test_img_data = generate_test_receipt()
    print_receipt(printer_ip, test_img_data)


def print_receipt(printer_ip: str, img_data: str) -> None:
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

    ip, port = _parse_ip_port(printer_ip)
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
        # Network disappeared mid-print (e.g. printer powered off or paper jam
        # caused the printer to close the connection).
        raise PrinterNotReachableError(
            f"Lost connection to printer {printer_ip} during print: {exc}"
        ) from exc
    except Exception as exc:
        # Covers escpos internal errors, status-check errors (paper out, etc.)
        error_msg = str(exc).lower()
        
        # We only treat it as a warning if it contains 'near' or 'low'.
        # If it says 'out', 'empty', or 'end' without 'near', it's a hard error.
        if "near" in error_msg or "low" in error_msg:
             logger.warning("[%s] Printer reported a non-fatal warning during print: %s", printer_ip, exc)
             return

        if any(kw in error_msg for kw in ("paper", "cover", "status", "error", "roll")):
            raise PrinterHardwareError(
                f"Printer {printer_ip} hardware error: {exc}"
            ) from exc
        raise PrinterHardwareError(
            f"Printer {printer_ip} unexpected error: {exc}"
        ) from exc


    finally:
        try:
            printer.close()
        except Exception:
            pass  # ignore close errors — the print result is already determined



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
    odoo_url = settings["odoo_url"].rstrip("/")
    headers = {"Authorization": f"Bearer {settings['api_key']}"}
    company_id = settings["company_id"]

    logger.info("[%s] Polling thread started (IP=%s)", name, ip)

    if not odoo_url or not settings['api_key']:
        logger.error("[%s] Source URL or API Key not configured. Polling suspended.", name)
        while not stop_event.is_set():
            stop_event.wait(60)
        return

    while not stop_event.is_set():
        try:
            response = requests.get(
                f"{odoo_url}/odoo_pos/jobs",
                json={"printer_ip": ip, "company_id": company_id},
                headers=headers,
                timeout=10,
            )

            if response.status_code == 200:
                result = response.json().get("result", [])
                if result:
                    job = result[0]
                    job_id = job["id"]
                    logger.info("[%s] Attempting job %s", name, job_id)

                    try:
                        print_receipt(ip, job["data"])
                        # ---- Only reached if printing succeeded completely ----
                        confirmed = confirm_job(odoo_url, headers, job_id)
                        if confirmed:
                            logger.info("[%s] Job %s printed and confirmed", name, job_id)
                            log_job_internal(printer["id"], name, "success")
                        else:
                            logger.warning(
                                "[%s] Job %s printed but confirmation failed — "
                                "server may re-queue it",
                                name, job_id,
                            )
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
            else:
                logger.warning("[%s] HTTP %s from source", name, response.status_code)

        except requests.exceptions.Timeout:
            logger.warning("[%s] Fetch timed out — will retry next poll", name)
        except requests.exceptions.ConnectionError as e:
            logger.warning("[%s] Cannot reach source URL: %s — will retry", name, e)
        except Exception as e:
            logger.error("[%s] Poll error: %s", name, e)

        stop_event.wait(5)  # wait 5 s, but wake immediately if stop requested

    logger.info("[%s] Polling thread stopped", name)


class AgentManager:
    """
    Keeps track of one thread+stop_event per printer (keyed by printer DB id).
    Called by the Flask app whenever printers are added / updated / deleted.
    """

    def __init__(self):
        self._threads: dict[int, threading.Thread] = {}
        self._stop_events: dict[int, threading.Event] = {}
        self._lock = threading.Lock()

    def start(self, printer: dict, settings: dict):
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
