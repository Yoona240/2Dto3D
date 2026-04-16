#!/usr/bin/env python3
"""
WebGL Render Module - HTTP Server Version

Renders 3D models using headless Chrome with model-viewer.
Uses local HTTP server to avoid file:// protocol restrictions.

Requirements:
    pip install playwright
    playwright install chromium

Usage:
    from scripts.webgl_render import run_webgl_render
    run_webgl_render(glb_path, output_dir, render_config)
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import http.server
import socketserver
from pathlib import Path
from typing import Optional

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load config early so we can use workspace.playwright_browsers_path before
# importing Playwright (Playwright reads PLAYWRIGHT_BROWSERS_PATH at import time).
from utils.config import load_config

_config = load_config()
# Use a local-disk path to avoid OSS/SeaweedFS mmap issues (SIGBUS BUS_ADRERR).
# Configured via workspace.playwright_browsers_path in config/config.yaml.
_PLAYWRIGHT_BROWSERS_PATH = Path(_config.workspace.playwright_browsers_path)
if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_PLAYWRIGHT_BROWSERS_PATH)

from core.render.webgl_script import generate_render_html


def safe_print(text):
    """Safely print text handling potential console encoding issues."""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        try:
            sys.stdout.buffer.write(
                text.encode(sys.stdout.encoding or "utf-8", errors="replace")
            )
            sys.stdout.buffer.write(b"\n")
            sys.stdout.buffer.flush()
        except Exception:
            pass


def _capture_command_output(command: list[str]) -> Optional[str]:
    """Best-effort shell capture for debug logging."""
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return None
    except Exception as exc:
        return f"<failed to run {' '.join(command)}: {exc}>"

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if stdout:
        return stdout
    if stderr:
        return f"<stderr> {stderr}"
    return "<no output>"


def _log_gpu_snapshot(label: str) -> None:
    """Log coarse GPU state into the main render log for postmortem analysis."""
    safe_print(f"[WebGL Render][GPU] Snapshot: {label}")

    query_output = _capture_command_output(
        [
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,memory.used,utilization.gpu,utilization.memory",
            "--format=csv,noheader,nounits",
        ]
    )
    if query_output is None:
        safe_print("[WebGL Render][GPU] nvidia-smi not found")
        return
    safe_print("[WebGL Render][GPU] --query-gpu")
    for line in query_output.splitlines():
        safe_print(f"[WebGL Render][GPU] {line}")

    pmon_output = _capture_command_output(["nvidia-smi", "pmon", "-c", "1"])
    if pmon_output:
        safe_print("[WebGL Render][GPU] pmon")
        for line in pmon_output.splitlines():
            safe_print(f"[WebGL Render][GPU] {line}")


def _make_cors_handler(directory: str):
    """Create an HTTP handler that serves *directory* with CORS headers."""

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

        def log_message(self, format, *args):
            pass

    return Handler


def start_http_server(directory: str, port: int = 0) -> tuple:
    """Start a local HTTP server with CORS support (thread-safe, no chdir)."""
    handler = _make_cors_handler(directory)
    server = socketserver.TCPServer(("", port), handler)
    actual_port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    return server, actual_port


def run_webgl_render(
    glb_path: str,
    output_dir: str,
    render_config=None,
    chrome_path: Optional[str] = None,
    fixed_radius: Optional[float] = None,
    fixed_center: Optional[dict] = None,
    output_params_file: Optional[str] = None,
):
    """
    Render a GLB model using WebGL (headless Chrome + model-viewer).

    Args:
        glb_path: Path to the GLB file
        output_dir: Directory to save rendered images
        render_config: RenderConfig object (if None, loads from config.yaml)
        chrome_path: Path to Chrome/Chromium executable (overrides config)

    Raises:
        RuntimeError: If rendering fails
        ImportError: If playwright is not installed

    Returns:
        None always (webgl_params written to output_params_file when provided)
    """
    if fixed_radius is not None and fixed_center is None:
        raise ValueError("fixed_center must be provided when fixed_radius is set")
    if fixed_center is not None and fixed_radius is None:
        raise ValueError("fixed_radius must be provided when fixed_center is set")
    # Load config if not provided
    if render_config is None:
        config = load_config()
        render_config = config.render

    # Validate backend
    if render_config.backend != "webgl":
        raise ValueError(
            f"WebGL render called but backend is set to: {render_config.backend}"
        )

    # Check if playwright is installed
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "playwright is required for WebGL rendering. "
            "Install with: pip install playwright && playwright install chromium"
        )

    # Create output directory
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Get WebGL config
    webgl_config = render_config.webgl
    chrome_exe = chrome_path or webgl_config.chrome_path

    # Copy Draco decoder files alongside GLB so the HTTP server can serve them
    try:
        _draco_dir = load_config().workspace.draco_decoder_dir
    except Exception:
        _draco_dir = ""
    if _draco_dir and Path(_draco_dir).is_dir():
        for fn in Path(_draco_dir).iterdir():
            dest = Path(glb_path).parent / fn.name
            if not dest.exists():
                shutil.copy2(str(fn), str(dest))

    # Start HTTP server for GLB file
    glb_file = Path(glb_path)
    server_dir = glb_file.parent
    server, port = start_http_server(str(server_dir))

    try:
        safe_print(f"[WebGL Render] Starting render for: {glb_path}")
        safe_print(f"[WebGL Render] Output directory: {output_dir}")
        safe_print(
            f"[WebGL Render] Image size: {render_config.image_size}x{render_config.image_size}"
        )
        safe_print(f"[WebGL Render] HTTP server on port: {port}")
        if webgl_config.use_gpu:
            _log_gpu_snapshot("before_browser_launch")

        # Generate HTML content with HTTP URL
        http_glb_url = f"http://localhost:{port}/{glb_file.name}"

        html_content = generate_render_html(
            glb_url=http_glb_url,
            image_size=render_config.image_size,
            environment_image=webgl_config.environment_image,
            shadow_intensity=webgl_config.shadow_intensity,
        )

        # Save HTML to temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            f.write(html_content)
            html_path = f.name

        try:
            # Launch browser and render
            with sync_playwright() as p:
                context = None
                page = None
                # Build Chrome flags based on GPU setting
                if webgl_config.use_gpu:
                    launch_args = [
                        "--use-gl=egl",
                        "--enable-webgl",
                        "--ignore-gpu-blocklist",
                        "--enable-gpu-rasterization",
                    ]
                else:
                    launch_args = [
                        "--disable-gpu",
                        "--enable-unsafe-swiftshader",
                    ]

                if chrome_exe:
                    safe_print(f"[WebGL Render] Using Chrome: {chrome_exe}")
                    browser = p.chromium.launch(
                        headless=True,
                        args=launch_args,
                        executable_path=chrome_exe,
                    )
                else:
                    safe_print("[WebGL Render] Using playwright-managed Chromium")
                    browser = p.chromium.launch(
                        headless=True,
                        args=launch_args,
                    )
                if webgl_config.use_gpu:
                    _log_gpu_snapshot("after_browser_launch")

                try:
                    # Create context and page
                    context = browser.new_context(
                        viewport={
                            "width": render_config.image_size,
                            "height": render_config.image_size,
                        }
                    )
                    page = context.new_page()

                    # Capture browser console for debugging
                    page.on(
                        "console",
                        lambda msg: safe_print(f"[Chrome] {msg.text}"),
                    )

                    html_url = f"file://{html_path}"
                    safe_print("[WebGL Render] Loading page...")

                    glb_size_mb = Path(glb_path).stat().st_size / (1024 * 1024)
                    load_timeout = max(60000, int(glb_size_mb * 10000))
                    safe_print(
                        f"[WebGL Render] File size: {glb_size_mb:.1f}MB, timeout: {load_timeout}ms"
                    )

                    try:
                        page.goto(
                            html_url, timeout=load_timeout, wait_until="networkidle"
                        )
                    except Exception as e:
                        # networkidle may time out on large pages; fall back to
                        # domcontentloaded and let wait_for_function handle readiness.
                        # Only catches TimeoutError-like situations — any other error
                        # (e.g. file not found) will surface in the second goto.
                        from playwright.sync_api import TimeoutError as PWTimeoutError

                        if not isinstance(e, PWTimeoutError):
                            raise
                        page.goto(
                            html_url,
                            timeout=load_timeout,
                            wait_until="domcontentloaded",
                        )

                    # Wait for model-viewer to load model and compute bounding box
                    safe_print("[WebGL Render] Waiting for model to load...")
                    page.wait_for_function(
                        "() => window._mvReady === true",
                        timeout=load_timeout,
                    )
                    safe_print("[WebGL Render] Model ready")

                    # Inject fixed camera params (target) or read+save params (source)
                    if fixed_radius is not None:
                        cx = fixed_center["x"]
                        cy = fixed_center["y"]
                        cz = fixed_center["z"]
                        page.evaluate(f"window._safeRadius = {fixed_radius}")
                        page.evaluate(
                            f"window._center = {{x: {cx}, y: {cy}, z: {cz}}}"
                        )
                        page.evaluate(
                            f"document.getElementById('viewer').cameraTarget = "
                            f"'{cx}m {cy}m {cz}m'"
                        )
                        safe_print(
                            f"[WebGL渲染] 注入固定相机参数 radius={fixed_radius:.4f} "
                            f"center=({cx:.4f},{cy:.4f},{cz:.4f})"
                        )
                    elif output_params_file is not None:
                        import json as _json

                        safe_radius = page.evaluate("() => window._safeRadius")
                        center = page.evaluate("() => window._center")
                        webgl_params = {
                            "webgl_safe_radius": safe_radius,
                            "webgl_center": {
                                "x": center["x"],
                                "y": center["y"],
                                "z": center["z"],
                            },
                        }
                        Path(output_params_file).write_text(_json.dumps(webgl_params))
                        safe_print(
                            f"[WebGL渲染] 相机参数已写入: {output_params_file}"
                        )

                    # Build view list with rotation_z applied
                    from core.render.webgl_script import DEFAULT_VIEWS

                    rot_z = render_config.rotation_z
                    views = [
                        (name, theta + rot_z, phi) for name, theta, phi in DEFAULT_VIEWS
                    ]

                    saved_count = 0
                    for name, theta, phi in views:
                        # Set camera via JS helper, then let the frame render
                        page.evaluate(f"window.setView('{name}', {theta}, {phi})")
                        # Double rAF + small delay to ensure WebGL
                        # composites the new frame to the display buffer
                        page.evaluate(
                            """() => new Promise(r =>
                                requestAnimationFrame(() =>
                                    requestAnimationFrame(() =>
                                        setTimeout(r, 200))))"""
                        )

                        out_file = output_path / f"{name}.png"
                        page.screenshot(path=str(out_file), type="png")
                        safe_print(f"[WebGL Render] Saved: {out_file}")
                        saved_count += 1

                    safe_print(f"[WebGL Render] Successfully saved {saved_count} views")
                    if webgl_config.use_gpu:
                        _log_gpu_snapshot("after_render_complete")

                    if saved_count == 0:
                        raise RuntimeError("No images were saved")

                finally:
                    if webgl_config.use_gpu:
                        _log_gpu_snapshot("before_browser_close")
                    if page is not None:
                        safe_print("[WebGL Render] Closing page...")
                        try:
                            page.close()
                        except Exception as exc:
                            safe_print(f"[WebGL Render] page.close() warning: {exc}")
                    if context is not None:
                        safe_print("[WebGL Render] Closing context...")
                        try:
                            context.close()
                        except Exception as exc:
                            safe_print(f"[WebGL Render] context.close() warning: {exc}")
                    safe_print("[WebGL Render] Closing browser...")
                    browser.close()
                    safe_print("[WebGL Render] Browser closed")

        finally:
            # Cleanup temp HTML file
            try:
                Path(html_path).unlink()
            except Exception:
                pass
    finally:
        # Stop HTTP server
        server.shutdown()
        server.server_close()


def check_playwright_installation():
    """Check if playwright is installed and browsers are available."""
    browsers_path = os.environ.get(
        "PLAYWRIGHT_BROWSERS_PATH", _PLAYWRIGHT_BROWSERS_PATH
    )

    try:
        import playwright
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
                browser.close()
                return True, f"Playwright OK. Browser cache: {browsers_path}"
            except Exception as e:
                return (
                    False,
                    f"Chromium not found in {browsers_path}. Run: playwright install chromium",
                )
    except ImportError:
        return (
            False,
            "Playwright not installed. Run: pip install playwright && playwright install chromium",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Render GLB model using WebGL")
    parser.add_argument("glb_path", help="Path to GLB file")
    parser.add_argument("output_dir", help="Output directory for rendered images")
    parser.add_argument(
        "--check", action="store_true", help="Check installation status"
    )
    args = parser.parse_args()

    if args.check:
        ok, msg = check_playwright_installation()
        print(msg)
        sys.exit(0 if ok else 1)

    # Run render
    try:
        run_webgl_render(args.glb_path, args.output_dir)
        print("Render completed successfully!")
    except Exception as e:
        print(f"Render failed: {e}")
        sys.exit(1)
