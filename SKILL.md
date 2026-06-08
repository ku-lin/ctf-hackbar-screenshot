---
name: ctf-hackbar-screenshot
description: Generate HackBar-style split-view screenshot pages for CTF and web-security writeups. Use when Codex needs to turn a raw HTTP request packet, Burp/Repeater request, or target URL plus body into a browser page whose upper half shows the target page and lower half shows a HackBar-like panel, then optionally capture that page as a PNG for a writeup.
---

# CTF HackBar Screenshot

Generate a reusable screenshot page bundle from a request packet, then optionally capture it with headless Edge or Chrome.

## Quick Start

Prefer `scripts/render_hackbar_screenshot.py`.

Raw request packet:

```powershell
python scripts/render_hackbar_screenshot.py `
  --request-file C:\path\to\request.txt `
  --output-dir C:\path\to\bundle `
  --screenshot C:\path\to\shot.png
```

URL only:

```powershell
python scripts/render_hackbar_screenshot.py `
  --target-url "http://127.0.0.1/sqli-labs/Less-1/?id=1" `
  --body "id=1" `
  --output-dir C:\path\to\bundle `
  --screenshot C:\path\to\shot.png
```

## Workflow

1. Prefer a raw HTTP request file when the user already has a Burp/Repeater packet.
2. Run `scripts/render_hackbar_screenshot.py` to generate a self-contained bundle.
3. If `--screenshot` is provided, let the script start a local HTTP server and capture the page with headless Edge or Chrome.
4. If the target blocks iframe embedding, keep the screenshot for the lower HackBar panel or swap the upper target to a local mirror/proxy page.

## Raw Request Format

Accept standard raw HTTP requests such as:

```http
POST /login HTTP/1.1
Host: target.local
User-Agent: Mozilla/5.0
Content-Type: application/x-www-form-urlencoded

username=admin&password=admin
```

The script derives:

- request method
- full URL
- header list
- body content

Pass `--target-url` only when the request packet lacks a usable absolute URL or `Host` header.

## Script Behavior

`scripts/render_hackbar_screenshot.py` does all of the following:

- copy the bundled template from `assets/template/`
- inject target URL, method, headers, and body into the split-view page
- generate a local preview bundle
- optionally launch headless Edge/Chrome and save a PNG

Useful flags:

- `--request-file`: preferred for Burp/Repeater packets
- `--target-url`: use when only a URL is available
- `--body`: manual body fallback
- `--output-dir`: required bundle directory
- `--screenshot`: optional PNG output path
- `--browser edge|chrome|auto`: choose capture browser
- `--window-size 1600,1200`: control screenshot dimensions
- `--port 8765`: local HTTP port

## Assets

`assets/template/` contains the portable split-view page and the bundled HackBar runtime used to render the lower panel.

Do not rewrite these assets unless the user asks to change the visual style or panel behavior. Prefer editing the generation script or injecting new request data.
