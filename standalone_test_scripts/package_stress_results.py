#!/usr/bin/env python3
"""Package the last five rounds of a standalone stress-test result."""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, NamedTuple, Optional, Sequence, Set


class PackagingError(RuntimeError):
    pass


class TestSpec(NamedTuple):
    rounds_key: str
    round_id_key: str
    round_dirs: tuple[str, ...]
    shared_dirs: tuple[str, ...] = ()


TEST_SPECS = {
    "export_load_stress_test": TestSpec(
        "tests",
        "test_index",
        ("logs/test_{:04d}", "exports/test_{:04d}", "images/test_{:04d}"),
    ),
    "firmware_update_stress_test": TestSpec(
        "tests", "test_index", ("logs/test_{:04d}",)
    ),
    "launch_param_load_stress": TestSpec(
        "runs", "run", ("test_{:04d}", "images/test_{:04d}")
    ),
    "launch_restart_stream_check": TestSpec(
        "attempts", "attempt", ("logs/test_{:04d}", "images/test_{:04d}")
    ),
    "preset_upgrade_stress_test": TestSpec(
        "tests", "test_index", ("logs/test_{:04d}", "images/test_{:04d}")
    ),
    "stream_toggle_stress_test": TestSpec("cycles", "cycle", (), ("logs",)),
}

ROOT_FILES = (
    "result.json",
    "summary.md",
    "events.jsonl",
    "terminal.log",
    "ui_request.json",
    "ui_status.json",
    "ui_stdout.log",
)


def _read_result(root: Path) -> tuple[Dict[str, Any], TestSpec]:
    result_path = root / "result.json"
    if not result_path.is_file():
        raise PackagingError(f"result.json not found: {result_path}")
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackagingError(f"invalid result.json: {exc}") from exc

    test_id = result.get("test_id") if isinstance(result, dict) else None
    details = result.get("details") if isinstance(result, dict) else None
    if test_id not in TEST_SPECS or not isinstance(details, dict):
        raise PackagingError(f"unsupported standalone stress result: {test_id!r}")

    status = str(result.get("status") or details.get("status") or "")
    if status not in {"passed", "failed", "warning", "interrupted"}:
        raise PackagingError(f"result is not finished: {status or '<missing>'}")
    return details, TEST_SPECS[test_id]


def _last_five_rounds(details: Dict[str, Any], spec: TestSpec) -> list[Dict[str, Any]]:
    rounds = details.get(spec.rounds_key)
    if not isinstance(rounds, list):
        raise PackagingError(f"invalid result: {spec.rounds_key} must be an array")
    finished = [
        item
        for item in rounds
        if isinstance(item, dict) and item.get("status") != "running"
    ]
    return finished[-5:]


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str) and value.strip():
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _add_path(root: Path, path: Path, files: Set[Path]) -> None:
    if not path.exists():
        return
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return
    if resolved.is_file():
        files.add(resolved)
    elif resolved.is_dir():
        for child in resolved.rglob("*"):
            if not child.is_file():
                continue
            try:
                resolved_child = child.resolve(strict=True)
                resolved_child.relative_to(root)
            except (OSError, ValueError):
                continue
            files.add(resolved_child)


def _round_id(item: Dict[str, Any], key: str) -> Optional[int]:
    try:
        value = int(item.get(key))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _collect_files(root: Path, details: Dict[str, Any], spec: TestSpec) -> Set[Path]:
    files: Set[Path] = set()
    rounds = _last_five_rounds(details, spec)

    for name in ROOT_FILES:
        _add_path(root, root / name, files)
    for relative in spec.shared_dirs:
        _add_path(root, root / relative, files)
    if not rounds:
        _add_path(root, root / "logs", files)

    for item in rounds:
        round_id = _round_id(item, spec.round_id_key)
        if round_id is not None:
            for template in spec.round_dirs:
                _add_path(root, root / template.format(round_id), files)
        for value in _strings(item):
            candidate = Path(value).expanduser()
            candidate = candidate if candidate.is_absolute() else root / candidate
            if candidate.is_file():
                _add_path(root, candidate, files)
    return files


class ProgressReader:
    def __init__(self, source, advance):
        self.source = source
        self.advance = advance

    def read(self, size=-1):
        data = self.source.read(size)
        self.advance(len(data))
        return data


def package_results(
    result_directory: Path,
    output: Optional[Path] = None,
    progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Path:
    root = result_directory.expanduser().resolve()
    if not root.is_dir():
        raise PackagingError(f"result directory not found: {root}")
    details, spec = _read_result(root)
    files = _collect_files(root, details, spec)
    output_path = (
        output.expanduser().resolve()
        if output is not None
        else root.with_name(root.name + ".tar.gz")
    )
    try:
        paths = sorted(files - {output_path})
        total_bytes = sum(path.stat().st_size for path in paths)
        processed_bytes = 0
        current_file = ""
        last_update = 0.0

        def report(status="running", force=False):
            nonlocal last_update
            now = time.monotonic()
            if progress and (force or now - last_update >= 0.2):
                progress({"status": status, "processed_bytes": processed_bytes,
                          "total_bytes": total_bytes, "current_file": current_file})
                last_update = now

        def advance(size):
            nonlocal processed_bytes
            processed_bytes += size
            report()

        report(force=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Publish only after gzip has written its footer. Keep the temporary
        # archive on the same filesystem so replacement is atomic.
        with tempfile.TemporaryDirectory(
            prefix=".package-", dir=output_path.parent
        ) as temporary_directory:
            temporary_path = Path(temporary_directory) / "archive.tar.gz"
            with tarfile.open(temporary_path, "w:gz", compresslevel=1) as archive:
                archive.copybufsize = 1024 * 1024
                for path in paths:
                    current_file = str(path.relative_to(root))
                    report(force=True)
                    with path.open("rb") as source:
                        info = archive.gettarinfo(
                            arcname=f"{root.name}/{current_file}", fileobj=source
                        )
                        archive.addfile(info, ProgressReader(source, advance))
                report("finalizing", force=True)
            temporary_path.replace(output_path)
        report("completed", force=True)
    except (OSError, tarfile.TarError) as exc:
        raise PackagingError(f"failed to create archive: {exc}") from exc
    return output_path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Package the last five rounds of a standalone stress-test result."
    )
    parser.add_argument("result_directory", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--progress-json", action="store_true",
                        help="Emit byte progress as JSON lines on stdout")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        progress = (lambda value: print(json.dumps(value), flush=True)) if args.progress_json else None
        output = package_results(args.result_directory, args.output, progress)
    except PackagingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
