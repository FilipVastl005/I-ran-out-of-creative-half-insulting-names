#!/usr/bin/env python3
"""
Controll_Pannel - Main Server Control Dashboard
Features:
- System performance telemetry (RAM, CPU Load, Uptime, Temperatures, Disk)
- Hardware controls (Screen backlight on/off, Drop RAM caches, Reboot)
- Service supervisor: Start, stop, restart, and monitor services defined in /etc/server_services.json
- Interactive Web Terminal passthru with CWD tracking and command history
- Web-based config editor for /etc/server_services.json
"""

import glob
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

PORT = 8080
PRIMARY_CONFIG_PATH = "/etc/server_services.json"
FALLBACK_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "services.json")


def get_config_path():
    """Return the active services config file path."""
    if os.path.exists(PRIMARY_CONFIG_PATH):
        return PRIMARY_CONFIG_PATH
    if os.path.exists(FALLBACK_CONFIG_PATH):
        return FALLBACK_CONFIG_PATH
    return PRIMARY_CONFIG_PATH


def read_services_config():
    """Read service definitions from the configuration file."""
    path = get_config_path()
    if not os.path.exists(path):
        # Initialize default config if neither exists
        default_services = [
            {
                "id": "mateo",
                "name": "Mateo Media Server",
                "service": "AllInOneAdmin.service",
                "description": "Direct-play streaming, personal drive & library (Port 8000)",
                "url": "http://serverbitch:8000"
            },
            {
                "id": "transmission",
                "name": "Transmission BitTorrent",
                "service": "transmission-daemon.service",
                "description": "Background torrent ingestion & download daemon (Port 9091)",
                "url": "http://serverbitch:9091/transmission/web/"
            },
            {
                "id": "panel",
                "name": "Server Control Panel",
                "service": "AdminService.service",
                "description": "Hardware stats, service supervisor & web terminal (Port 8080)",
                "url": "http://serverbitch:8080"
            },
            {
                "id": "ssh",
                "name": "SSH Daemon",
                "service": "ssh.service",
                "description": "OpenSSH secure remote access server (Port 22)",
                "url": ""
            }
        ]
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default_services, f, indent=2)
            # Make sure non-root users can edit it if needed
            try:
                os.chmod(path, 0o666)
            except Exception:
                pass
            return default_services
        except Exception:
            return default_services

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return [{"id": "error", "name": f"Error loading {path}", "service": "", "description": str(e), "url": ""}]


def get_backlight_paths():
    devices = glob.glob("/sys/class/backlight/*")
    if not devices:
        return None, None
    dev = devices[0]
    return f"{dev}/brightness", f"{dev}/max_brightness"


def run_cmd(cmd, timeout=5):
    try:
        res = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return (res.stdout.strip() or res.stderr.strip())
    except Exception as e:
        return str(e)


def set_backlight(state: bool):
    b_path, m_path = get_backlight_paths()
    if not b_path:
        return "No backlight device found in /sys/class/backlight/"

    try:
        if state:
            with open(m_path, "r") as f:
                max_val = f.read().strip()
            val = max_val
        else:
            val = "0"

        with open(b_path, "w") as f:
            f.write(val)
        return f"Backlight set to {val}"
    except Exception as e:
        return f"Backlight error: {e}"


def get_system_stats():
    # Temperatures
    temp_raw = run_cmd("sensors 2>/dev/null | grep -E '(Core 0|temp1|Tctl)' | head -n 2")
    if not temp_raw or "N/A" in temp_raw:
        # Fallback to thermal zone
        tz = glob.glob("/sys/class/thermal/thermal_zone*/temp")
        if tz:
            try:
                with open(tz[0], "r") as f:
                    temp_raw = f"{int(f.read().strip()) / 1000.0:.1f} °C"
            except Exception:
                temp_raw = "N/A"
        else:
            temp_raw = "N/A"

    # CPU & RAM summary
    load = run_cmd("cat /proc/loadavg | awk '{print $1, $2, $3}'")
    mem = run_cmd("free -h | awk '/^Mem:/ {print $3 \" / \" $2}'")
    uptime = run_cmd("uptime -p")

    # Disk usage (Root & External Storage)
    disk_root = run_cmd("df -h / | awk 'NR==2 {print $3 \" / \" $2 \" (\" $5 \")\"}'")
    disk_ext = run_cmd("df -h /mnt/sdb3 2>/dev/null | awk 'NR==2 {print $3 \" / \" $2 \" (\" $5 \")\"}'")

    # Backlight state
    b_path, m_path = get_backlight_paths()
    screen_state = "Not Supported"
    if b_path and os.path.exists(b_path):
        try:
            with open(b_path, "r") as f:
                cur = f.read().strip()
            screen_state = "OFF" if cur == "0" else f"ON ({cur})"
        except Exception:
            screen_state = "Error"

    return {
        "screen": screen_state,
        "load": load or "N/A",
        "memory": mem or "N/A",
        "uptime": uptime or "N/A",
        "disk_root": disk_root or "N/A",
        "disk_ext": disk_ext if disk_ext else "Offline",
        "temperature": temp_raw or "N/A"
    }


def query_services_status():
    """Query systemd status for each configured service."""
    services = read_services_config()
    results = []

    for item in services:
        unit = item.get("service", "").strip()
        if not unit:
            continue

        # Check is-active
        active_proc = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=5
        )
        is_active = active_proc.stdout.strip() or active_proc.stderr.strip()

        # Check is-enabled
        enabled_proc = subprocess.run(
            ["systemctl", "is-enabled", unit],
            capture_output=True, text=True, timeout=5
        )
        is_enabled = enabled_proc.stdout.strip() or enabled_proc.stderr.strip()

        results.append({
            "id": item.get("id", unit),
            "name": item.get("name", unit),
            "service": unit,
            "description": item.get("description", ""),
            "url": item.get("url", ""),
            "status": is_active,
            "enabled": is_enabled,
        })

    return {
        "services": results,
        "config_path": get_config_path()
    }


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Server Control Panel</title>
<style>
  :root {
    --bg: #0d0f17;
    --card: #151824;
    --card-header: #1c2030;
    --border: #262b3d;
    --text: #e2e8f0;
    --text-dim: #94a3b8;
    --accent: #38bdf8;
    --accent-hover: #0ea5e9;
    --danger: #ef4444;
    --danger-hover: #dc2626;
    --success: #10b981;
    --success-hover: #059669;
    --warning: #f59e0b;
    --term-bg: #08090f;
  }
  * { box-sizing: border-box; }
  body {
    font-family: 'JetBrains Mono', 'Fira Code', 'Courier New', Courier, monospace;
    background: var(--bg);
    color: var(--text);
    margin: 0;
    padding: 16px;
    display: flex;
    justify-content: center;
  }
  .container { max-width: 860px; width: 100%; }
  
  /* Header */
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--border);
    padding-bottom: 12px;
    margin-bottom: 16px;
  }
  .header h1 { font-size: 1.35rem; margin: 0; color: #fff; display: flex; align-items: center; gap: 8px; }
  .header .badge {
    background: rgba(56, 189, 248, 0.15);
    color: var(--accent);
    padding: 3px 8px;
    border-radius: 4px;
    font-size: 0.75rem;
    border: 1px solid rgba(56, 189, 248, 0.3);
  }

  /* Cards */
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 16px;
    box-shadow: 0 4px 10px rgba(0,0,0,0.3);
  }
  .card-title {
    font-size: 0.95rem;
    font-weight: bold;
    color: #fff;
    margin-bottom: 12px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .card-subtitle {
    font-size: 0.75rem;
    color: var(--text-dim);
    font-weight: normal;
  }

  /* Stats Grid */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 10px;
  }
  .stat-box {
    background: #0f121d;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 12px;
  }
  .stat-label { font-size: 0.72rem; color: var(--text-dim); text-transform: uppercase; margin-bottom: 4px; }
  .stat-value { font-size: 0.92rem; font-weight: bold; color: var(--accent); }

  /* Quick Actions */
  .grid-actions {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 10px;
    margin-top: 10px;
  }
  button {
    background: #1e2336;
    color: var(--text);
    border: 1px solid var(--border);
    padding: 10px 14px;
    border-radius: 6px;
    font-family: inherit;
    font-size: 0.82rem;
    font-weight: bold;
    cursor: pointer;
    transition: all 0.15s ease;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
  }
  button:hover { background: #293049; border-color: var(--accent); }
  button:active { transform: scale(0.98); }
  button.danger { border-color: rgba(239, 68, 68, 0.4); color: #fca5a5; }
  button.danger:hover { background: var(--danger); color: #fff; border-color: var(--danger); }
  button.success { border-color: rgba(16, 185, 129, 0.4); color: #6ee7b7; }
  button.success:hover { background: var(--success); color: #fff; border-color: var(--success); }
  button.sm { padding: 5px 10px; font-size: 0.75rem; border-radius: 4px; }

  /* Services Table / Items */
  .service-item {
    background: #0f121d;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px 14px;
    margin-bottom: 8px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
  }
  .service-info { flex: 1; min-width: 220px; }
  .service-header-line { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
  .service-name { font-weight: bold; font-size: 0.95rem; color: #fff; }
  .service-unit { font-size: 0.72rem; color: var(--text-dim); }
  .service-desc { font-size: 0.78rem; color: #a0aec0; }
  .status-tag {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 0.72rem;
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: bold;
    text-transform: uppercase;
  }
  .status-active { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }
  .status-inactive { background: rgba(148, 163, 184, 0.15); color: #94a3b8; border: 1px solid rgba(148, 163, 184, 0.3); }
  .status-failed { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); }
  .service-controls { display: flex; align-items: center; gap: 6px; }

  /* Terminal Window */
  .terminal-box {
    background: var(--term-bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
  }
  .terminal-toolbar {
    background: #111420;
    padding: 6px 12px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 6px;
  }
  .terminal-chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .term-chip {
    background: #1c2133;
    border: 1px solid #2d3550;
    color: var(--text-dim);
    font-size: 0.7rem;
    padding: 2px 7px;
    border-radius: 3px;
    cursor: pointer;
  }
  .term-chip:hover { color: #fff; border-color: var(--accent); background: #252c42; }
  .terminal-output {
    height: 240px;
    overflow-y: auto;
    padding: 10px 12px;
    font-size: 0.8rem;
    line-height: 1.4;
    white-space: pre-wrap;
    word-break: break-all;
    color: #e2e8f0;
  }
  .terminal-input-bar {
    display: flex;
    background: #0d101a;
    border-top: 1px solid var(--border);
    padding: 8px 10px;
    align-items: center;
    gap: 8px;
  }
  .cwd-prompt { font-size: 0.78rem; color: #34d399; font-weight: bold; white-space: nowrap; }
  .term-input {
    flex: 1;
    background: transparent;
    border: none;
    color: #fff;
    font-family: inherit;
    font-size: 0.82rem;
    outline: none;
  }

  /* Modal */
  .modal-overlay {
    display: none;
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.75);
    backdrop-filter: blur(2px);
    z-index: 1000;
    justify-content: center;
    align-items: center;
    padding: 16px;
  }
  .modal-box {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    max-width: 650px;
    width: 100%;
    padding: 18px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .modal-textarea {
    width: 100%;
    height: 280px;
    background: #090b12;
    border: 1px solid var(--border);
    color: #38bdf8;
    font-family: inherit;
    font-size: 0.8rem;
    padding: 10px;
    border-radius: 6px;
    resize: vertical;
    outline: none;
  }
  #status-bar {
    position: fixed;
    bottom: 12px;
    right: 12px;
    background: #1e2336;
    border: 1px solid var(--border);
    color: var(--text);
    padding: 8px 14px;
    border-radius: 6px;
    font-size: 0.78rem;
    box-shadow: 0 4px 12px rgba(0,0,0,0.4);
    display: none;
    z-index: 900;
  }
</style>
</head>
<body>
<div class="container">
  
  <div class="header">
    <h1>
      <span>⚡ Server Node Control</span>
      <span class="badge">PROD</span>
    </h1>
    <div>
      <span id="clock" style="font-size: 0.8rem; color: var(--text-dim);"></span>
    </div>
  </div>

  <!-- System Telemetry -->
  <div class="card">
    <div class="card-title">
      <span>System Telemetry</span>
      <span class="card-subtitle" id="uptime-val">uptime: ...</span>
    </div>
    <div class="stats-grid">
      <div class="stat-box">
        <div class="stat-label">CPU Load Avg</div>
        <div class="stat-val" id="stat-load">...</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Memory (Used/Total)</div>
        <div class="stat-val" id="stat-mem">...</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Internal Disk (Root)</div>
        <div class="stat-val" id="stat-disk-root">...</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">External Drive (/mnt/sdb3)</div>
        <div class="stat-val" id="stat-disk-ext">...</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">CPU Temperature</div>
        <div class="stat-val" id="stat-temp">...</div>
      </div>
      <div class="stat-box">
        <div class="stat-label">Display Backlight</div>
        <div class="stat-val" id="stat-screen">...</div>
      </div>
    </div>
  </div>

  <!-- Hardware Controls -->
  <div class="card">
    <div class="card-title">Hardware & Maintenance Controls</div>
    <div class="grid-actions">
      <button class="danger" onclick="sendAction('screen_off')">🌙 Screen OFF</button>
      <button class="success" onclick="sendAction('screen_on')">☀️ Screen ON</button>
      <button onclick="sendAction('drop_caches')">🧹 Clear RAM Caches</button>
      <button class="danger" onclick="if(confirm('Are you sure you want to REBOOT the server?')) sendAction('reboot')">🔄 Reboot Server</button>
    </div>
  </div>

  <!-- Service Manager -->
  <div class="card">
    <div class="card-title">
      <span>Managed Services</span>
      <button class="sm" onclick="openConfigModal()">⚙️ Edit Config</button>
    </div>
    <div style="font-size: 0.72rem; color: var(--text-dim); margin-bottom: 12px;" id="config-path-label">
      Config file: loading...
    </div>
    <div id="services-list">
      <div style="text-align:center; padding: 20px; color: var(--text-dim);">Loading services status...</div>
    </div>
  </div>

  <!-- Terminal Passthru -->
  <div class="card">
    <div class="card-title">
      <span>Terminal Passthru (Web Shell)</span>
      <span class="card-subtitle">Real-time root execution</span>
    </div>
    <div class="terminal-box">
      <div class="terminal-toolbar">
        <div class="terminal-chips">
          <span class="term-chip" onclick="quickCmd('df -h')">df -h</span>
          <span class="term-chip" onclick="quickCmd('free -h')">free -h</span>
          <span class="term-chip" onclick="quickCmd('uptime')">uptime</span>
          <span class="term-chip" onclick="quickCmd('journalctl -u AllInOneAdmin -n 25 --no-pager')">Mateo Logs</span>
          <span class="term-chip" onclick="quickCmd('journalctl -u transmission-daemon -n 25 --no-pager')">Torrent Logs</span>
          <span class="term-chip" onclick="quickCmd('journalctl -u AdminService -n 25 --no-pager')">Panel Logs</span>
        </div>
        <div>
          <button class="sm" onclick="clearTerm()" style="padding: 2px 8px;">Clear</button>
        </div>
      </div>
      <div class="terminal-output" id="term-out">Antigravity Web Terminal Passthru ready.\nType commands below or use quick actions.\n\n</div>
      <div class="terminal-input-bar">
        <span class="cwd-prompt" id="term-cwd">~#</span>
        <input type="text" class="term-input" id="term-in" placeholder="Enter shell command (e.g. ls -la, systemctl status...)" autocomplete="off" spellcheck="false">
        <button class="sm" onclick="execTerm()" style="background: var(--accent); color: #000; border: none;">Run</button>
      </div>
    </div>
  </div>

</div>

<!-- Status Toast -->
<div id="status-bar"></div>

<!-- Config Modal -->
<div class="modal-overlay" id="config-modal">
  <div class="modal-box">
    <div style="font-weight: bold; font-size: 0.95rem; color: #fff;">
      Edit Services Configuration (<span id="modal-cfg-path"></span>)
    </div>
    <div style="font-size: 0.75rem; color: var(--text-dim);">
      Add, edit, or remove services here. Each entry requires "name" and "service" (systemd unit name).
    </div>
    <textarea class="modal-textarea" id="modal-cfg-text" spellcheck="false"></textarea>
    <div style="display:flex; justify-content: flex-end; gap: 8px;">
      <button class="sm" onclick="closeConfigModal()">Cancel</button>
      <button class="sm success" onclick="saveConfigModal()">Save & Apply</button>
    </div>
  </div>
</div>

<script>
let currentCwd = "/home/user";
let cmdHistory = [];
let historyIndex = -1;

function showToast(msg, duration = 3000) {
  const bar = document.getElementById('status-bar');
  bar.innerText = msg;
  bar.style.display = 'block';
  setTimeout(() => { bar.style.display = 'none'; }, duration);
}

// Clock
setInterval(() => {
  document.getElementById('clock').innerText = new Date().toLocaleTimeString();
}, 1000);

// Telemetry Polling
async function refreshStats() {
  try {
    const res = await fetch('/api/stats');
    const d = await res.json();
    document.getElementById('uptime-val').innerText = d.uptime;
    document.getElementById('stat-load').innerText = d.load;
    document.getElementById('stat-mem').innerText = d.memory;
    document.getElementById('stat-disk-root').innerText = d.disk_root;
    document.getElementById('stat-disk-ext').innerText = d.disk_ext;
    document.getElementById('stat-temp').innerText = d.temperature;
    document.getElementById('stat-screen').innerText = d.screen;
  } catch(e) {}
}

// Services Polling
async function refreshServices() {
  try {
    const res = await fetch('/api/services');
    const data = await res.json();
    document.getElementById('config-path-label').innerText = 'Config file: ' + data.config_path;
    document.getElementById('modal-cfg-path').innerText = data.config_path;
    
    const list = document.getElementById('services-list');
    if (!data.services || data.services.length === 0) {
      list.innerHTML = '<div style="color:var(--text-dim); text-align:center; padding:15px;">No services configured in ' + data.config_path + '</div>';
      return;
    }

    let html = '';
    data.services.forEach(s => {
      const isActive = s.status === 'active';
      const isFailed = s.status === 'failed';
      let tagClass = 'status-inactive';
      let tagLabel = '○ ' + s.status;
      if (isActive) {
        tagClass = 'status-active';
        tagLabel = '● ACTIVE';
      } else if (isFailed) {
        tagClass = 'status-failed';
        tagLabel = '⚠ FAILED';
      }

      html += `
        <div class="service-item">
          <div class="service-info">
            <div class="service-header-line">
              <span class="service-name">${escapeHtml(s.name)}</span>
              <span class="status-tag ${tagClass}">${tagLabel}</span>
              ${s.url ? `<a href="${s.url}" target="_blank" style="color:var(--accent); font-size:0.75rem; text-decoration:none;">↗ Open Web</a>` : ''}
            </div>
            <div class="service-unit">${escapeHtml(s.service)} (${escapeHtml(s.enabled)})</div>
            ${s.description ? `<div class="service-desc">${escapeHtml(s.description)}</div>` : ''}
          </div>
          <div class="service-controls">
            ${isActive
              ? `<button class="sm danger" onclick="controlService('${s.service}', 'stop')">Stop</button>
                 <button class="sm" onclick="controlService('${s.service}', 'restart')">Restart</button>`
              : `<button class="sm success" onclick="controlService('${s.service}', 'start')">Start</button>`
            }
          </div>
        </div>
      `;
    });
    list.innerHTML = html;
  } catch(e) {
    console.error(e);
  }
}

async function controlService(service, action) {
  showToast(`Executing ${action} on ${service}...`);
  try {
    const res = await fetch('/api/service/control', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({service, action})
    });
    const d = await res.json();
    if (d.success) {
      showToast(`${action.toUpperCase()} successful: ${service}`);
    } else {
      showToast(`Error: ${d.error || 'Failed'}`);
    }
    setTimeout(refreshServices, 1000);
  } catch(e) {
    showToast('Request failed');
  }
}

async function sendAction(act) {
  showToast(`Running ${act}...`);
  try {
    const res = await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: act})
    });
    const d = await res.json();
    showToast(d.result || d.error);
    setTimeout(refreshStats, 800);
  } catch(e) {
    showToast('Action failed');
  }
}

// Terminal
async function execTerm() {
  const input = document.getElementById('term-in');
  const cmd = input.value.trim();
  if (!cmd) return;

  cmdHistory.push(cmd);
  historyIndex = cmdHistory.length;
  input.value = '';

  const out = document.getElementById('term-out');
  out.innerText += `[${currentCwd}]# ${cmd}\n`;
  out.scrollTop = out.scrollHeight;

  try {
    const res = await fetch('/api/terminal', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({command: cmd, cwd: currentCwd})
    });
    const d = await res.json();
    if (d.cwd) {
      currentCwd = d.cwd;
      document.getElementById('term-cwd').innerText = (currentCwd.length > 25 ? '...' + currentCwd.slice(-22) : currentCwd) + '#';
    }
    if (d.stdout) out.innerText += d.stdout + '\n';
    if (d.stderr) out.innerText += `[STDERR]: ` + d.stderr + '\n';
    if (d.returncode !== 0 && !d.stderr && !d.stdout) {
      out.innerText += `[Exit code: ${d.returncode}]\n`;
    }
    out.innerText += '\n';
    out.scrollTop = out.scrollHeight;
  } catch(e) {
    out.innerText += `[Error communicating with server]\n\n`;
    out.scrollTop = out.scrollHeight;
  }
}

function quickCmd(cmd) {
  document.getElementById('term-in').value = cmd;
  execTerm();
}

function clearTerm() {
  document.getElementById('term-out').innerText = '';
}

// Command history navigation with ArrowUp/ArrowDown
document.getElementById('term-in').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    execTerm();
  } else if (e.key === 'ArrowUp') {
    if (historyIndex > 0) {
      historyIndex--;
      e.target.value = cmdHistory[historyIndex];
    }
    e.preventDefault();
  } else if (e.key === 'ArrowDown') {
    if (historyIndex < cmdHistory.length - 1) {
      historyIndex++;
      e.target.value = cmdHistory[historyIndex];
    } else {
      historyIndex = cmdHistory.length;
      e.target.value = '';
    }
    e.preventDefault();
  }
});

// Config Modal
async function openConfigModal() {
  try {
    const res = await fetch('/api/services/config');
    const d = await res.json();
    document.getElementById('modal-cfg-text').value = JSON.stringify(d.services, null, 2);
    document.getElementById('config-modal').style.display = 'flex';
  } catch(e) {
    showToast('Failed to load config');
  }
}

function closeConfigModal() {
  document.getElementById('config-modal').style.display = 'none';
}

async function saveConfigModal() {
  const text = document.getElementById('modal-cfg-text').value;
  try {
    const parsed = JSON.parse(text);
    const res = await fetch('/api/services/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({config: parsed})
    });
    const d = await res.json();
    if (d.success) {
      showToast('Config saved successfully');
      closeConfigModal();
      refreshServices();
    } else {
      alert('Save failed: ' + (d.error || 'Unknown error'));
    }
  } catch(err) {
    alert('Invalid JSON syntax: ' + err.message);
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/[&<>"']/g, function(m) {
    return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[m];
  });
}

// Initial Run
refreshStats();
refreshServices();
setInterval(refreshStats, 3000);
setInterval(refreshServices, 5000);
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ["/", "/index.html"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
        elif path == "/api/stats":
            self.send_json(get_system_stats())
        elif path == "/api/services":
            self.send_json(query_services_status())
        elif path == "/api/services/config":
            self.send_json({
                "config_path": get_config_path(),
                "services": read_services_config()
            })
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            data = {}

        if path == "/api/action":
            act = data.get("action")
            res_msg = "Unknown action"
            if act == "screen_off":
                res_msg = set_backlight(False)
            elif act == "screen_on":
                res_msg = set_backlight(True)
            elif act == "drop_caches":
                run_cmd("sync; echo 3 > /proc/sys/vm/drop_caches")
                res_msg = "RAM caches cleared successfully"
            elif act == "reboot":
                subprocess.Popen(["reboot"])
                res_msg = "Server reboot initiated..."

            self.send_json({"result": res_msg})

        elif path == "/api/service/control":
            service = data.get("service", "").strip()
            action = data.get("action", "").strip().lower()

            # Security validation: alphanumeric + . _ - @
            if not re.match(r"^[a-zA-Z0-9_\-\.@]+$", service):
                self.send_json({"success": False, "error": "Invalid service unit name"}, 400)
                return

            if action not in ["start", "stop", "restart", "enable", "disable"]:
                self.send_json({"success": False, "error": "Invalid action"}, 400)
                return

            try:
                proc = subprocess.run(
                    ["systemctl", action, service],
                    capture_output=True, text=True, timeout=15
                )
                if proc.returncode == 0:
                    self.send_json({"success": True, "result": proc.stdout.strip() or f"{action} completed"})
                else:
                    self.send_json({
                        "success": False,
                        "error": proc.stderr.strip() or f"systemctl exited with {proc.returncode}"
                    })
            except subprocess.TimeoutExpired:
                self.send_json({"success": False, "error": "Command timed out after 15 seconds"}, 504)
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)

        elif path == "/api/terminal":
            cmd = data.get("command", "").strip()
            cwd = data.get("cwd", "/home/user").strip()

            if not os.path.isdir(cwd):
                cwd = "/home/user" if os.path.isdir("/home/user") else "/"

            if not cmd:
                self.send_json({"stdout": "", "stderr": "", "returncode": 0, "cwd": cwd})
                return

            # Special handling for 'cd'
            cd_match = re.match(r"^cd(?:\s+(.*))?$", cmd)
            if cd_match:
                target = cd_match.group(1)
                if not target or target.strip() == "~":
                    new_cwd = "/home/user" if os.path.isdir("/home/user") else "/root"
                else:
                    target = target.strip()
                    if target.startswith("~"):
                        target = os.path.expanduser(target)
                    new_cwd = os.path.abspath(os.path.join(cwd, target))

                if os.path.isdir(new_cwd):
                    self.send_json({"stdout": "", "stderr": "", "returncode": 0, "cwd": new_cwd})
                else:
                    self.send_json({
                        "stdout": "",
                        "stderr": f"cd: no such file or directory: {target}",
                        "returncode": 1,
                        "cwd": cwd
                    })
                return

            # Execute command in specified cwd
            try:
                res = subprocess.run(
                    cmd, shell=True, cwd=cwd,
                    capture_output=True, text=True, timeout=30
                )
                self.send_json({
                    "stdout": res.stdout,
                    "stderr": res.stderr,
                    "returncode": res.returncode,
                    "cwd": cwd
                })
            except subprocess.TimeoutExpired:
                self.send_json({
                    "stdout": "",
                    "stderr": "Command timed out after 30 seconds",
                    "returncode": 124,
                    "cwd": cwd
                })
            except Exception as e:
                self.send_json({
                    "stdout": "",
                    "stderr": str(e),
                    "returncode": 1,
                    "cwd": cwd
                })

        elif path == "/api/services/config":
            config_data = data.get("config")
            if not isinstance(config_data, list):
                self.send_json({"success": False, "error": "Config must be a JSON array of services"}, 400)
                return

            target_path = PRIMARY_CONFIG_PATH
            try:
                with open(target_path, "w", encoding="utf-8") as f:
                    json.dump(config_data, f, indent=2)
                try:
                    os.chmod(target_path, 0o666)
                except Exception:
                    pass
                self.send_json({"success": True, "saved_to": target_path})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Keep logs clean
        return


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Controll_Pannel server supervisor running on port {PORT}...")
    server.serve_forever()
