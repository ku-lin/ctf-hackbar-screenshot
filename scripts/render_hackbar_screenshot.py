#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.server
import json
import shutil
import socketserver
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "assets" / "template"


@dataclass
class RequestData:
    method: str
    url: str
    body: str
    headers: list[dict[str, object]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate and optionally capture a HackBar-style split-view screenshot."
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
        help="Local HTTP port used for preview/capture.",
    )
    parser.add_argument(
        "--browser",
        choices=["auto", "edge", "chrome"],
        default="auto",
        help="Headless browser to use for screenshot capture.",
    )
    parser.add_argument(
        "--window-size",
        default="1600,1200",
        help="Headless browser window size, for example 1600,1200.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="Seconds to wait before capturing the screenshot.",
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

    headers = [
        {"enabled": True, "name": name, "value": value}
        for name, value in header_pairs
    ]
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
    return RequestData(
        method="GET",
        url=args.target_url,
        body=args.body,
        headers=default_headers,
    )


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


def find_browser(kind: str) -> str:
    candidates = {
        "edge": [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ],
        "chrome": [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ],
    }
    order = ["edge", "chrome"] if kind == "auto" else [kind]
    for browser_name in order:
        for raw_path in candidates[browser_name]:
            path = Path(raw_path)
            if path.exists():
                return str(path)
    raise FileNotFoundError("No supported browser found. Install Edge or Chrome, or omit --screenshot.")


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def serve_directory(directory: Path, port: int) -> tuple[ReusableTCPServer, threading.Thread]:
    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *args, directory=str(directory), **kwargs
    )
    httpd = ReusableTCPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def capture_screenshot(browser_path: str, url: str, screenshot: Path, window_size: str, delay: float) -> None:
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    time.sleep(delay)
    subprocess.run(
        [
            browser_path,
            "--headless",
            "--disable-gpu",
            f"--window-size={window_size}",
            f"--screenshot={screenshot}",
            url,
        ],
        check=True,
    )


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir).resolve()
    screenshot_path = Path(args.screenshot).resolve() if args.screenshot else None
    request = resolve_request(args)

    build_bundle(request, output_dir)
    page_url = f"http://127.0.0.1:{args.port}/hackbar-split-view.html"

    print(f"[+] Generated bundle: {output_dir}")
    print(f"[+] Preview URL: {page_url}")
    print(f"[+] Request URL: {request.url}")
    print(f"[+] Request Method: {request.method}")

    if screenshot_path is None:
        print("[+] Screenshot skipped.")
        return 0

    browser_path = find_browser(args.browser)
    httpd, thread = serve_directory(output_dir, args.port)
    try:
        capture_screenshot(browser_path, page_url, screenshot_path, args.window_size, args.delay)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=1)

    print(f"[+] Screenshot saved: {screenshot_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
