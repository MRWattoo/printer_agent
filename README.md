# Printer Application

A Flask web application that manages multiple IP-based ESC/POS receipt printers.
Configure printers in the browser; the app continuously polls each printer's
Odoo/API source, checks printer and paper status, and sends jobs to the correct
network printer. Jobs are confirmed only after a successful print.

---

## Install on Ubuntu (one command)

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/MRWattoo/printer_agent/main/install.sh)
```

> Requires root. Run on your Ubuntu server.

The installer will:

1. Install system packages (`python3-venv`, `git`, `libusb`, `libjpeg`, etc.)
2. Create a dedicated system user `printer-app`
3. Clone the repository to `/opt/printer_application`
4. Install the package into `/opt/printer_application/.venv`
5. Create data directory `/var/lib/printer_app` (SQLite DB lives here)
6. Install and start the `printer-app` systemd service
7. Install a **daily auto-update timer** (`printer-app-update.timer`) that pulls the latest version from GitHub every night at 03:00 and restarts the service automatically

Open **http://\<server-ip\>:5000** in your browser.

---

## Auto-updates

Once installed, the app updates itself automatically every night.

```bash
# Check timer status
sudo systemctl status printer-app-update.timer

# View update logs
sudo journalctl -u printer-app-update

# Trigger a manual update at any time
sudo /opt/printer_application/update.sh
```

The update script:
- Checks whether new commits exist on `main`
- If nothing changed, exits immediately (no restart)
- If an update is found: pulls source → upgrades the package → restarts the service

---

## Useful commands

```bash
sudo systemctl status printer-app        # service status
sudo journalctl -u printer-app -f        # live logs
sudo systemctl restart printer-app       # manual restart
```

---

## Uninstall

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

---

## Configuration

Set environment variables in `/etc/systemd/system/printer-app.service`, then
`sudo systemctl daemon-reload && sudo systemctl restart printer-app`.

| Variable             | Default                | Description                         |
|----------------------|------------------------|-------------------------------------|
| `PRINTER_APP_PORT`   | `5000`                 | Port the web UI listens on          |
| `PRINTER_APP_HOST`   | `0.0.0.0`              | Bind address                        |
| `PRINTER_APP_DATA`   | `/var/lib/printer_app` | Directory where `printers.db` lives |

---

## Features

- Browser UI — add / edit / delete / enable / disable printers
- Per-printer: **Name**, **IP**, **Source URL**, **API Key**, **Company ID**
- Pre-print checks: verifies printer is **online** and **paper roll is adequate** before printing
- Jobs confirmed **only after successful print** — any failure leaves the job pending for automatic retry
- One background thread per printer — polls `/odoo_pos/jobs` every 5 s
- Live status badges auto-refresh every 5 s
- `GET /api/status` — JSON status of all printers
- Daily auto-update from GitHub via systemd timer

---

## Project structure

```
printer_application/
├── install.sh              ← Ubuntu one-command installer (run as root)
├── update.sh               ← Pull latest from GitHub + restart service
├── printer_app.service     ← Reference systemd unit file
├── pyproject.toml
├── requirements.txt
└── printer_app/
    ├── __init__.py
    ├── app.py              ← Flask app + CLI entry point
    ├── print_agent.py      ← Polling threads, status checks, ESC/POS printing
    ├── templates/
    │   ├── index.html
    │   └── form.html
    └── static/
```
