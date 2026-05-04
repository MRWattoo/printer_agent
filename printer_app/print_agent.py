"""
print_agent.py
Manages one polling thread per printer. Each thread continuously fetches
print jobs from the configured source URL and sends them to the IP printer.

Job confirmation policy:
  - A job is confirmed (marked done on the server) ONLY when the printer
    acknowledges the full print without any error.
  - Any failure — printer unreachable, timeout, paper-out / hardware error,
    or any other exception — leaves the job unconfirmed so the server keeps
    it in the pending list and the next poll will re-fetch and retry it.
"""

import base64
import datetime
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


def check_printer_connectivity(printer_ip: str) -> bool:
    """
    Check if the printer is reachable and online.
    Returns True if connected, False otherwise.
    """
    if not ESCPOS_AVAILABLE:
        return False
    
    try:
        printer = Network(printer_ip, timeout=5)
        is_online = printer.is_online()
        printer.close()
        return is_online
    except (socket.timeout, socket.error, OSError):
        return False
    except Exception:
        return False


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

    imgs = imgcrop(Image.open(BytesIO(base64.b64decode(img_data))))

    # --- connect ---------------------------------------------------------
    try:
        printer = Network(printer_ip, timeout=10)
    except (socket.timeout, socket.error, OSError) as exc:
        raise PrinterNotReachableError(
            f"Cannot connect to printer {printer_ip}: {exc}"
        ) from exc

    # --- pre-print status checks -----------------------------------------
    try:
        # 1. Online / ready check
        if not printer.is_online():
            raise PrinterNotReachableError(
                f"Printer {printer_ip} is offline or not ready"
            )

        # 2. Paper / roll check
        #    paper_status() returns: 2 = adequate, 1 = near-end, 0 = out
        paper = printer.paper_status()
        if paper == 0:
            raise PrinterHardwareError(
                f"Printer {printer_ip}: paper roll is empty — cannot print"
            )
        if paper == 1:
            raise PrinterHardwareError(
                f"Printer {printer_ip}: paper roll is near-end — "
                "replace roll before printing to avoid a partial receipt"
            )
    except (PrinterNotReachableError, PrinterHardwareError):
        raise  # re-raise our own typed errors unchanged
    except (socket.timeout, socket.error, OSError) as exc:
        raise PrinterNotReachableError(
            f"Status query failed for printer {printer_ip}: {exc}"
        ) from exc
    except Exception as exc:
        # Some printers don't support DLE-EOT status queries; if the status
        # check itself errors we log a warning and attempt to print anyway
        # rather than blocking all jobs on unsupported hardware.
        logger.warning(
            "[%s] Status query raised an unexpected error (%s) — "
            "proceeding with print attempt",
            printer_ip, exc,
        )

    # --- send data -------------------------------------------------------
    try:
        for img in imgs:
            printer.image(img)
        printer.cut()
    except (socket.timeout, socket.error, OSError) as exc:
        # Network disappeared mid-print (e.g. printer powered off or paper jam
        # caused the printer to close the connection).
        raise PrinterNotReachableError(
            f"Lost connection to printer {printer_ip} during print: {exc}"
        ) from exc
    except Exception as exc:
        # Covers escpos internal errors, status-check errors (paper out, etc.)
        error_msg = str(exc).lower()
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

def poll_printer(printer: dict, stop_event: threading.Event):
    """
    Runs in a dedicated thread.
    printer dict keys: id, name, ip, odoo_url, api_key, company_id

    Confirmation policy enforced here:
      - print_receipt() succeeds  → confirm_job()
      - print_receipt() raises    → log the error, do NOT confirm
                                    job stays pending; next poll re-fetches it
    """
    name = printer["name"]
    ip = printer["ip"]
    odoo_url = printer["odoo_url"].rstrip("/")
    headers = {"Authorization": f"Bearer {printer['api_key']}"}
    company_id = printer["company_id"]

    logger.info("[%s] Polling thread started (IP=%s)", name, ip)

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
                        else:
                            logger.warning(
                                "[%s] Job %s printed but confirmation failed — "
                                "server may re-queue it",
                                name, job_id,
                            )

                    except PrinterNotReachableError as e:
                        logger.error(
                            "[%s] Job %s NOT confirmed — printer unreachable: %s. "
                            "Will retry next poll.",
                            name, job_id, e,
                        )
                    except PrinterHardwareError as e:
                        logger.error(
                            "[%s] Job %s NOT confirmed — printer hardware error "
                            "(paper out / roll missing / device error): %s. "
                            "Will retry next poll.",
                            name, job_id, e,
                        )
                    except Exception as e:
                        logger.error(
                            "[%s] Job %s NOT confirmed — unexpected print error: %s. "
                            "Will retry next poll.",
                            name, job_id, e,
                        )
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

    def start(self, printer: dict):
        pid = printer["id"]
        with self._lock:
            self._stop_existing(pid)
            stop_event = threading.Event()
            t = threading.Thread(
                target=poll_printer,
                args=(printer, stop_event),
                name=f"printer-{pid}",
                daemon=True,
            )
            self._threads[pid] = t
            self._stop_events[pid] = stop_event
            t.start()

    def stop(self, printer_id: int):
        with self._lock:
            self._stop_existing(printer_id)

    def restart(self, printer: dict):
        self.start(printer)

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
