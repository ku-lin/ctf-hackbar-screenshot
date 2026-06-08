#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from websocket import create_connection


ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "assets" / "template"
RUNTIME_DIR = ROOT / ".runtime"
SERVER_META = RUNTIME_DIR / "server.json"
BROWSER_META = RUNTIME_DIR / "browser.json"


@dataclass
class RequestData:
    method: str
    url: str
    body: str
    headers: list[dict[str, object]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate and capture a HackBar-style split-view screenshot."
    )
    parser.add_argument("--target-url", help="Target page shown in the upper frame.")
    parser.add_argument(
        "--request-file",
        help="Raw HTTP request file. Preferred when you want the HackBar UI derived from a request packet.",
    )
    parser.add_argument(
        "--scheme",
        default="http",
        choices=["http", "https"],
        help="Scheme used when deriving a URL from a raw HTTP request with a Host header.",
    )
    parser.add_argument(
        "--body",
        default="id=1",
        help="Initial POST body content shown in the HackBar panel.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory where the generated page bundle will be written.",
    )
    parser.add_argument(
        "--screenshot",
        help="Output PNG path. When omitted, only the page bundle is generated.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Persistent local HTTP port used for preview/capture.",
    )
    parser.add_argument(
        "--browser",
        choices=["edge"],
        default="edge",
        help="Browser used for screenshot capture.",
    )
    parser.add_argument(
        "--browser-port",
        type=int,
        default=9222,
        help="Remote debugging port of the persistent Edge instance.",
    )
    parser.add_argument(
        "--window-size",
        default="1600,1200",
        help="Capture viewport size, for example 1600,1200.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.8,
        help="Small post-ready settle delay before capture.",
    )
    parser.add_argument(
        "--wait-selector",
        default="body[data-capture-ready='1']",
        help="DOM selector used to decide when the split-view page is ready for capture.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Maximum seconds to wait for the capture-ready selector.",
    )
    parser.add_argument(
        "--keep-target-open",
        action="store_true",
        help="Keep the created DevTools target open after capture.",
    )
    return parser.parse_args()


def parse_raw_request(raw_text: str, fallback_url: str | None, scheme: str) -> RequestData:
    text = raw_text.lstrip("\ufeff").replace("\r\n", "\n")
    if "\n\n" in text:
        head, body = text.split("\n\n", 1)
    else:
        head, body = text, ""

    lines = [line for line in head.split("\n") if line.strip()]
    if not lines:
        raise ValueError("Raw request is empty.")

    request_line = lines[0].strip()
    parts = request_line.split()
    if len(parts) < 2:
        raise ValueError(f"Invalid request line: {request_line}")

    method = parts[0].upper()
    path = parts[1]
    header_pairs: list[tuple[str, str]] = []
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        header_pairs.append((name.strip(), value.strip()))

    url = fallback_url
    if path.startswith("http://") or path.startswith("https://"):
        url = path
    elif url is None:
        host = next((value for name, value in header_pairs if name.lower() == "host"), None)
        if not host:
            raise ValueError("Raw request has no absolute URL and no Host header. Pass --target-url.")
        prefix = host if "://" in host else f"{scheme}://{host}"
        url = prefix.rstrip("/") + path

    headers = [{"enabled": True, "name": name, "value": value} for name, value in header_pairs]
    return RequestData(method=method, url=url, body=body, headers=headers)


def resolve_request(args: argparse.Namespace) -> RequestData:
    if args.request_file:
        raw_text = Path(args.request_file).read_text(encoding="utf-8")
        return parse_raw_request(raw_text, args.target_url, args.scheme)

    if not args.target_url:
        raise ValueError("Pass --target-url or --request-file.")

    default_headers = [
        {"enabled": True, "name": "User-Agent", "value": "Mozilla/5.0"},
        {"enabled": True, "name": "Accept", "value": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
    ]
    return RequestData("GET", args.target_url, args.body, default_headers)


def escape_js_string(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def build_bundle(request: RequestData, output_dir: Path) -> Path:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    shutil.copytree(TEMPLATE_DIR, output_dir)

    split_view = output_dir / "hackbar-split-view.html"
    mock_main = output_dir / "HackBar-chrome" / "mock-main.html"
    root_assets = output_dir / "assets"
    replacements = {
        "__TARGET_URL__": request.url,
        "__REQUEST_METHOD__": request.method,
        "__BODY_CONTENT__": escape_js_string(request.body),
        "__HEADERS_JSON__": json.dumps(request.headers, ensure_ascii=False),
    }

    split_view_text = split_view.read_text(encoding="utf-8")
    mock_main_text = mock_main.read_text(encoding="utf-8")
    for key, value in replacements.items():
        split_view_text = split_view_text.replace(key, value)
        mock_main_text = mock_main_text.replace(key, value)

    split_view.write_text(split_view_text, encoding="utf-8")
    mock_main.write_text(mock_main_text, encoding="utf-8")
    shutil.copytree(output_dir / "HackBar-chrome" / "assets", root_assets, dirs_exist_ok=True)
    return split_view


def read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def is_pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def http_json(url: str, method: str = "GET") -> dict | list:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def edge_path() -> str:
    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("Microsoft Edge was not found.")


def server_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1):
            return True
    except Exception:
        return False


def ensure_server(root_dir: Path, port: int) -> None:
    meta = read_json(SERVER_META) or {}
    expected_root = str(root_dir.resolve())
    if meta.get("root_dir") == expected_root and server_alive(port) and is_pid_running(meta.get("pid")):
        return

    if meta.get("pid") and is_pid_running(meta.get("pid")):
        try:
            os.kill(meta["pid"], 15)
        except OSError:
            pass
        time.sleep(0.3)

    command = [
        "python",
        "-m",
        "http.server",
        str(port),
        "--bind",
        "127.0.0.1",
    ]
    process = subprocess.Popen(
        command,
        cwd=str(root_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        if server_alive(port):
            write_json(SERVER_META, {"pid": process.pid, "port": port, "root_dir": expected_root})
            return
        time.sleep(0.1)
    raise RuntimeError("Failed to start the persistent local HTTP server.")


def browser_version(port: int) -> dict | None:
    try:
        return http_json(f"http://127.0.0.1:{port}/json/version")
    except Exception:
        return None


def ensure_browser(port: int, window_size: str) -> None:
    meta = read_json(BROWSER_META) or {}
    version = browser_version(port)
    if version and is_pid_running(meta.get("pid")):
        return

    if meta.get("pid") and is_pid_running(meta.get("pid")):
        try:
            os.kill(meta["pid"], 15)
        except OSError:
            pass
        time.sleep(0.5)

    user_data_dir = RUNTIME_DIR / "edge-profile"
    user_data_dir.mkdir(parents=True, exist_ok=True)
    browser = edge_path()
    process = subprocess.Popen(
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-allow-origins=*",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 8
    while time.time() < deadline:
        version = browser_version(port)
        if version:
            write_json(
                BROWSER_META,
                {
                    "pid": process.pid,
                    "port": port,
                    "browser_path": browser,
                    "user_data_dir": str(user_data_dir),
                    "window_size": window_size,
                },
            )
            return
        time.sleep(0.1)
    raise RuntimeError("Failed to start the persistent Edge instance.")


class CDPSession:
    def __init__(self, websocket_url: str):
        self.ws = create_connection(websocket_url, timeout=5, suppress_origin=True)
        self._id = 0

    def close(self) -> None:
        self.ws.close()

    def send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload = {"id": self._id, "method": method}
        if params:
            payload["params"] = params
        self.ws.send(json.dumps(payload))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self._id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} failed: {message['error']}")
                return message.get("result", {})

    def wait_for_selector(self, selector: str, timeout: float, settle_delay: float) -> None:
        deadline = time.time() + timeout
        expression = f"""
(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el) return false;
  const style = window.getComputedStyle(el);
  return style && style.display !== 'none' && style.visibility !== 'hidden';
}})()
"""
        while time.time() < deadline:
            result = self.send(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            )
            if result.get("result", {}).get("value") is True:
                time.sleep(settle_delay)
                return
            time.sleep(0.1)
        raise TimeoutError(f"Timed out waiting for selector: {selector}")


def create_target(port: int, page_url: str) -> dict:
    encoded = urllib.parse.quote(page_url, safe=":/?&=%-._~")
    try:
        result = http_json(f"http://127.0.0.1:{port}/json/new?{encoded}", method="PUT")
    except urllib.error.HTTPError:
        result = http_json(f"http://127.0.0.1:{port}/json/new?{encoded}")
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected DevTools target creation response.")
    return result


def close_target(port: int, target_id: str) -> None:
    try:
        http_json(f"http://127.0.0.1:{port}/json/close/{target_id}", method="PUT")
    except Exception:
        try:
            http_json(f"http://127.0.0.1:{port}/json/close/{target_id}")
        except Exception:
            pass


def prewarm(browser_port: int, prewarm_url: str, window_size: str) -> None:
    target = create_target(browser_port, prewarm_url)
    session = CDPSession(target["webSocketDebuggerUrl"])
    try:
        width, height = [int(x) for x in window_size.split(",", 1)]
        session.send("Page.enable")
        session.send("Runtime.enable")
        session.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "mobile": False,
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
            },
        )
        session.wait_for_selector("body", 5, 0.1)
    finally:
        session.close()
        close_target(browser_port, target["id"])


def capture_screenshot(
    browser_port: int,
    page_url: str,
    screenshot: Path,
    window_size: str,
    wait_selector: str,
    timeout: float,
    delay: float,
    keep_target_open: bool,
) -> None:
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    target = create_target(browser_port, page_url)
    session = CDPSession(target["webSocketDebuggerUrl"])
    try:
        width, height = [int(x) for x in window_size.split(",", 1)]
        session.send("Page.enable")
        session.send("Runtime.enable")
        session.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "mobile": False,
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
            },
        )
        session.wait_for_selector(wait_selector, timeout, delay)
        result = session.send(
            "Page.captureScreenshot",
            {
                "format": "png",
                "captureBeyondViewport": True,
                "fromSurface": True,
            },
        )
        screenshot.write_bytes(base64.b64decode(result["data"]))
    finally:
        session.close()
        if not keep_target_open:
            close_target(browser_port, target["id"])


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir).resolve()
    screenshot_path = Path(args.screenshot).resolve() if args.screenshot else None
    request = resolve_request(args)

    build_bundle(request, output_dir)
    ensure_server(output_dir.parent, args.port)
    ensure_browser(args.browser_port, args.window_size)

    page_url = f"http://127.0.0.1:{args.port}/{output_dir.name}/hackbar-split-view.html"
    prewarm_url = f"http://127.0.0.1:{args.port}/{output_dir.name}/HackBar-chrome/mock-main.html"

    print(f"[+] Generated bundle: {output_dir}")
    print(f"[+] Preview URL: {page_url}")
    print(f"[+] Request URL: {request.url}")
    print(f"[+] Request Method: {request.method}")
    print(f"[+] HTTP server: 127.0.0.1:{args.port}")
    print(f"[+] Edge debugger: 127.0.0.1:{args.browser_port}")

    prewarm(args.browser_port, prewarm_url, args.window_size)

    if screenshot_path is None:
        print("[+] Screenshot skipped.")
        return 0

    capture_screenshot(
        args.browser_port,
        page_url,
        screenshot_path,
        args.window_size,
        args.wait_selector,
        args.timeout,
        args.delay,
        args.keep_target_open,
    )
    print(f"[+] Screenshot saved: {screenshot_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
