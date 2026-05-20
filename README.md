# Printer Application

**Version: 2.0.0**

A Flask web application that manages multiple IP-based ESC/POS receipt printers
on your local network. Add printers in the browser; the app continuously polls
each printer for real-time status, fetches print jobs from an Odoo/HTTP source,
and prints them. Jobs are confirmed **only after a successful print** so any
failure leaves the job pending for retry.

No CUPS. No printer drivers. Talks raw ESC/POS over TCP.

---

## Install on Ubuntu (one command)

```bash
curl -fsSL https://raw.githubusercontent.com/MRWattoo/printer_agent/main/install.sh | sudo bash
```

The installer will:

1. Install system packages (`python3-venv`, `git`, `libjpeg`, etc.)
2. Create a dedicated system user `printer-app`
3. Clone the repo to `/opt/printer_application`
4. Install the package into `/opt/printer_application/.venv`
5. Create the data directory `/var/lib/printer_app` (SQLite DB lives here)
6. Install and start the `printer-app` systemd service
7. Install a daily auto-update timer (`printer-app-update.timer`) that pulls the latest version from GitHub each night at 03:00 and restarts the service

Open **http://\<server-ip\>:5000** in your browser.

---

## Features

### Printer management
- Browser UI — add / edit printers, with whole-row click on the dashboard to open Edit.
- **Disable / Delete** buttons live on the Edit page; the dashboard list stays clean.
- **Test Connection** and **Test Print** buttons on every row.
- **+ Add Printer** sits on the left of the header, **Items per page** on the right.

### Network-printer aware
- Per-printer **Port** field (default `9100`). The `ip:port` shorthand still works in the IP field.
- **Auto port detection** — the Probe button scans 18 common printer ports (9100, 9101, 9102, 4000, 6101, 515, 631, 80, 443, 8080, 21, 22, 23, 9220, 9290, 3910, 3911, 5358) in parallel and picks the first working ESC/POS port automatically. Open ports are shown as clickable chips so you can override the selection.

### Accurate status (the heart of the app)
- Real-time status via raw ESC/POS `DLE EOT 1/2/3/4` — decoded into exact reasons:
  *Cover open*, *Paper roll end*, *Paper near-end*, *Auto-cutter error*, *Unrecoverable error*, *Auto-recoverable error*, *Mechanical error*.
- **Response byte validated** against the spec's fixed bit pattern (`(b & 0x93) == 0x12`), so garbage bytes from the TCP buffer can't fake a "paper out" alarm.
- Receive buffer is drained before each query.
- **Locked-port detection** — when port 9100 refuses connections (Epson TM-series do this when out of paper), the manager probes LPD/HTTP/IPP/alt-RAW ports to confirm the printer is still on the network and labels the row **"Error"** with the cause hint, instead of plain "Offline".
- **Single-owner connection model** — the per-printer poll thread is the only thing talking to port 9100; the dashboard reads from an in-memory cache (12 s TTL). No connection races, no flicker.
- **Continuous polling** — status is refreshed every 5 s for every enabled printer, *independent of whether Odoo is configured*. Disable polling by disabling the printer.
- **`busy` flag** — while a job is in flight the dashboard shows "Printing…" instead of probing the print socket.

### Printer identity
- One-click **Probe Printer / Check Connection** button on Add and Edit pages, plus auto-trigger on IP-field blur (with 900 ms debounce). Edit page also auto-checks on load.
- Pulls **Manufacturer, Model, Firmware, Device code, Language, Hostname, MAC** via `GS I n` and ARP. Mapping verified live against Epson TM-T88VI and Rongta RP80:
  - `n=65 → firmware`, `n=66 → manufacturer`, `n=67 → model`, `n=68 → device code`, `n=69 → language`
- One TCP connection **per** `GS I n` query — cheap ESC/POS clones reorder responses on a shared socket; one-shot pairing guarantees correctness.
- **Last-known info is preserved** — a failed/offline probe never erases stored identity. Only the `last_probe` timestamp moves forward.

### Dashboard
- Color-coded badges: 🟢 Online · 🟡 Warning · 🟠 Error · 🔵 Printing… · 🔴 Offline · ⚪ Disabled · 🔴 Agent stopped
- Connectivity column distinguishes *Connected* / *On network (printer in error)* / *Disconnected*.
- Reason column color-coded to severity.

### Print path
- Pre-print check: paper status (`2`=adequate, `1`=near-end warning, `0`=empty → abort).
- Jobs confirmed **only after** `print_receipt()` returns without raising.
- One background thread per printer — polls `/odoo_pos/jobs` every 5 s when Odoo is configured.
- Tall receipts auto-sliced into chunks so ESC/POS can handle them.

### API
- `GET /api/status` — JSON for every printer (status, errors, warnings, busy flag, identity).
- `POST /api/check/<id>` — full probe of a stored printer, persists fresh info.
- `POST /api/probe?ip=&port=&printer_id=` — ad-hoc probe (used by the Add/Edit form).

### Operations
- Daily auto-update from GitHub via systemd timer.
- `update.sh` now syncs the systemd unit file when it changes upstream and runs `daemon-reload` automatically — no more "unit file changed on disk" warnings.

---

## Auto-updates

```bash
sudo systemctl status printer-app-update.timer   # timer status
sudo journalctl -u printer-app-update            # update logs
sudo /opt/printer_application/update.sh          # manual update
sudo /opt/printer_application/update.sh --force  # ignore the auto_update DB flag
```

The update script:
- Checks whether new commits exist on `main`. If nothing changed, exits immediately.
- Pulls source → upgrades the package → installs the new `printer-app.service` if the unit file changed → `daemon-reload` → restart.

---

## Useful commands

```bash
sudo systemctl status printer-app          # service status
sudo journalctl -u printer-app -f          # live logs
sudo systemctl restart printer-app         # manual restart
```

---

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/MRWattoo/printer_agent/main/install.sh | sudo bash -s -- --uninstall
```

Or, if the source is still on disk:

```bash
sudo bash /opt/printer_application/install.sh --uninstall
```

Removes the service, timer, venv, source clone, data directory, and system user.

---

## Manual / development install

```bash
git clone https://github.com/MRWattoo/printer_agent.git
cd printer_agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
printer-app
```

Default login: **`wattoo` / `3r6&&$u63r!0r##`** — change it from the Users page.

---

## Configuration

Environment variables (set in `/etc/systemd/system/printer-app.service`, then `daemon-reload` + `restart`):

| Variable             | Default                | Description                          |
|----------------------|------------------------|--------------------------------------|
| `PRINTER_APP_PORT`   | `5000`                 | Port the web UI listens on           |
| `PRINTER_APP_HOST`   | `0.0.0.0`              | Bind address                         |
| `PRINTER_APP_DATA`   | `~/.printer_app`       | Directory where `printers.db` lives  |
| `FLASK_SECRET_KEY`   | *(dev fallback)*       | Session signing key — set in prod    |

Per-printer Odoo source URL / API key / company ID are configured under **Settings** in the UI.

---

## How status accuracy works

ESC/POS `DLE EOT n` returns one byte whose bits encode different state groups.
Every response byte has a fixed pattern (`bit0=0, bit1=1, bit4=1, bit7=0`)
which we validate before decoding — anything else is rejected as noise.

| Query    | Tells us                                                                |
|----------|-------------------------------------------------------------------------|
| `DLE EOT 1` | Online/offline, drawer pin                                            |
| `DLE EOT 2` | Cover open · Paper end · Generic error                                |
| `DLE EOT 3` | Mechanical error · Auto-cutter error · Recoverable / Unrecoverable    |
| `DLE EOT 4` | Paper near-end · Paper end (roll sensor)                              |

When port 9100 itself is refused (typical Epson behavior in error state), the
manager falls back to TCP-probing standard admin ports (515, 80, 443, 631, 9101,
9102, 4000) — if any answers, the device is flagged **`error_state`** with a
human-readable cause hint rather than misreported as "Offline".

