from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, unquote, urlparse

import yaml

from .run_manager import (
    AUTO_TEST_WS,
    CONFIG_PATH,
    CORE_PACKAGE_ROOT,
    UI_RESULTS_ROOT,
    RunManager,
    ensure_dir,
    load_config,
    read_json,
    save_config,
)


MANAGER = RunManager()
UI_PACKAGE_DIR = Path(__file__).resolve().parent
RUNTIME_CONFIG: Dict[str, Any] = load_config()


def _read_asset(relative_path: str) -> bytes:
    asset_path = (UI_PACKAGE_DIR / relative_path).resolve()
    if UI_PACKAGE_DIR not in [asset_path, *asset_path.parents]:
        raise FileNotFoundError(relative_path)
    return asset_path.read_bytes()


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            return yaml.safe_load(stream) or {}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def list_profiles() -> Dict[str, Any]:
    profiles_dir = CORE_PACKAGE_ROOT / "profiles"
    profiles_by_type: Dict[str, Any] = {"functional": [], "performance": []}
    all_profiles = []

    def append_profile(path: Path, profile_type: str) -> None:
        data = _load_yaml(path)
        item = {
            "name": data.get("profile_name") or path.stem,
            "type": profile_type,
            "path": str(path),
            "launch_file": data.get("launch_file", ""),
            "default_launch_args": data.get("default_launch_args", {}),
            "launch_scenarios": [
                entry.get("name", "") for entry in data.get("launch_scenarios", [])
            ],
            "performance_scenarios": [
                {
                    "name": entry.get("name", ""),
                    "description": entry.get("description", ""),
                    "duration": entry.get("duration", ""),
                }
                for entry in data.get("performance_scenarios", [])
            ],
            "error": data.get("error"),
        }
        profiles_by_type.setdefault(profile_type, []).append(item)
        all_profiles.append(item)

    for profile_type in ("functional", "performance"):
        for path in sorted((profiles_dir / profile_type).glob("*.yaml")):
            append_profile(path, profile_type)

    for path in sorted(profiles_dir.glob("*.yaml")):
        append_profile(path, "legacy")

    return {
        "profiles": all_profiles,
        "profiles_by_type": profiles_by_type,
        "profiles_dir": str(profiles_dir),
    }


def _find_result_json(run_dir: Path) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for candidate in [
        run_dir / "result.json",
        run_dir / "functional" / "result.json",
        run_dir / "performance" / "result.json",
    ]:
        if candidate.is_file():
            result[candidate.parent.name if candidate.parent != run_dir else "result"] = read_json(
                candidate, {}
            )
    return result


def _read_summary_files(run_dir: Path) -> Dict[str, str]:
    summaries: Dict[str, str] = {}
    for candidate in [
        run_dir / "summary.md",
        run_dir / "functional" / "summary.md",
        run_dir / "performance" / "summary.md",
    ]:
        if candidate.is_file():
            key = candidate.parent.name if candidate.parent != run_dir else "summary"
            summaries[key] = candidate.read_text(encoding="utf-8")
    return summaries


def _aggregate_status(run_dir: Path, ui_status: Dict[str, Any]) -> str:
    if ui_status.get("status"):
        return str(ui_status["status"])
    results = _find_result_json(run_dir)
    statuses = [
        str(item.get("status", "unknown")) for item in results.values() if isinstance(item, dict)
    ]
    if not statuses:
        return "unknown"
    if any(status == "failed" for status in statuses):
        return "failed"
    if any(status == "interrupted" for status in statuses):
        return "interrupted"
    if all(status == "passed" for status in statuses):
        return "passed"
    return statuses[0]


def list_runs() -> Dict[str, Any]:
    ensure_dir(UI_RESULTS_ROOT)
    runs = []
    for run_dir in sorted(UI_RESULTS_ROOT.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue
        ui_status = read_json(run_dir / "ui_status.json", {})
        request = read_json(run_dir / "ui_request.json", {})
        runs.append(
            {
                "run_id": run_dir.name,
                "mode": ui_status.get("mode") or request.get("mode", ""),
                "status": _aggregate_status(run_dir, ui_status),
                "started_at": ui_status.get("started_at", ""),
                "ended_at": ui_status.get("ended_at", ""),
                "results_dir": str(run_dir),
            }
        )
    return {"runs": runs, "results_root": str(UI_RESULTS_ROOT)}


def get_run(run_id: str) -> Dict[str, Any]:
    run_dir = (UI_RESULTS_ROOT / run_id).resolve()
    if UI_RESULTS_ROOT.resolve() not in [run_dir, *run_dir.parents] or not run_dir.is_dir():
        raise FileNotFoundError(run_id)
    return {
        "run_id": run_id,
        "ui_status": read_json(run_dir / "ui_status.json", {}),
        "ui_request": read_json(run_dir / "ui_request.json", {}),
        "results": _find_result_json(run_dir),
        "summaries": _read_summary_files(run_dir),
        "logs": {
            path.name: path.read_text(encoding="utf-8", errors="replace")[-20000:]
            for path in sorted(run_dir.rglob("*.log"))
        },
    }


class UiHandler(BaseHTTPRequestHandler):
    server_version = "OrbbecAutoTestUI/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        status = args[1] if len(args) > 1 else ""
        try:
            if int(status) < 400:
                return
        except (TypeError, ValueError):
            pass
        print(f"[UI HTTP] {self.address_string()} - {format % args}", file=sys.stderr)

    def _send_bytes(
        self, body: bytes, status: int = 200, content_type: str = "application/octet-stream"
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_json(self, payload: Any, status: int = 200) -> None:
        self._send_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            status=status,
            content_type="application/json; charset=utf-8",
        )

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path.startswith("/static/"):
            self.send_response(HTTPStatus.OK)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        else:
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        body = self.rfile.read(length)
        return json.loads(body.decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path == "/":
                self._send_bytes(_read_asset("templates/index.html"), content_type="text/html")
            elif path.startswith("/static/"):
                asset_path = path.lstrip("/")
                content_type = mimetypes.guess_type(asset_path)[0] or "application/octet-stream"
                self._send_bytes(_read_asset(asset_path), content_type=content_type)
            elif path == "/api/config":
                config = dict(RUNTIME_CONFIG)
                config.update(
                    {
                        "auto_test_ws": str(AUTO_TEST_WS),
                        "config_path": str(CONFIG_PATH),
                    }
                )
                self._send_json(config)
            elif path == "/api/profiles":
                self._send_json(list_profiles())
            elif path == "/api/status":
                offset = int((query.get("offset") or ["0"])[0] or "0")
                self._send_json(MANAGER.current_snapshot(log_offset=offset))
            elif path == "/api/runs":
                self._send_json(list_runs())
            elif path.startswith("/api/runs/"):
                run_id = unquote(path.removeprefix("/api/runs/"))
                self._send_json(get_run(run_id))
            else:
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
        except FileNotFoundError:
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            payload = self._read_json_body()
            if parsed.path == "/api/config":
                self._send_json(save_config(payload))
            elif parsed.path == "/api/run":
                status, response = MANAGER.start(payload)
                self._send_json(response, status=status)
            elif parsed.path == "/api/stop":
                self._send_json(MANAGER.stop())
            else:
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
        except json.JSONDecodeError as exc:
            self._send_json({"error": f"invalid json: {exc}"}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)


def parse_args() -> argparse.Namespace:
    config = load_config()
    parser = argparse.ArgumentParser(description="Run the Orbbec camera auto test web UI")
    parser.add_argument("--host", default=str(config.get("host") or "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(config.get("port") or 8000))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    RUNTIME_CONFIG["host"] = args.host
    RUNTIME_CONFIG["port"] = args.port
    host = args.host
    port = args.port
    ensure_dir(UI_RESULTS_ROOT)
    server = ThreadingHTTPServer((host, port), UiHandler)
    print(f"Orbbec camera auto test UI: http://{host}:{port}", flush=True)
    print(f"Results root: {UI_RESULTS_ROOT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nUI server stopped", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
