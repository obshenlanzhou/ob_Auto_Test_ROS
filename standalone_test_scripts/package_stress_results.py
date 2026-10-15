#!/usr/bin/env python3
"""Package failure windows or the last five rounds of a standalone stress test."""

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


def _selected_rounds(details: Dict[str, Any], spec: TestSpec) -> list[Dict[str, Any]]:
    rounds = details.get(spec.rounds_key)
    if not isinstance(rounds, list):
        raise PackagingError(f"invalid result: {spec.rounds_key} must be an array")
    finished = [
        item
        for item in rounds
        if isinstance(item, dict) and item.get("status") != "running"
    ]
    failures = [index for index, item in enumerate(finished) if item.get("status") == "failed"]
    if not failures:
        return finished[-5:]
    indices = set()
    for index in failures:
        indices.update(range(max(0, index - 2), min(len(finished), index + 3)))
    return [item for index, item in enumerate(finished) if index in indices]


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
    rounds = _selected_rounds(details, spec)

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


def _snapshot_details(root: Path, test_id: str, destination: Path):
    if test_id not in TEST_SPECS:
        raise PackagingError(f"unsupported standalone stress result: {test_id!r}")
    spec = TEST_SPECS[test_id]
    events_path = root / "events.jsonl"
    if not events_path.is_file():
        raise PackagingError("尚无事件记录，请等待失败轮次结束后再打包")
    rounds = {}
    highest_round = 0
    # Freeze the event prefix before inspecting it; ignore an unfinished last line.
    with events_path.open("rb") as source, destination.open("wb") as target:
        remaining = events_path.stat().st_size
        while remaining:
            line = source.readline(remaining)
            if not line:
                break
            remaining -= len(line)
            if not line.endswith(b"\n"):
                break
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise PackagingError("事件记录损坏，无法确定失败轮次") from exc
            target.write(line)
            if not isinstance(event, dict):
                continue
            round_id = _round_id(event, "current")
            if round_id is None:
                continue
            highest_round = max(highest_round, round_id)
            phase = event.get("phase")
            if phase in {"completed-cycle", "failed-cycle"}:
                previous = rounds.get(round_id, {})
                status = "failed" if phase == "failed-cycle" else "passed"
                if previous.get("status") == "failed":
                    status = "failed"
                rounds[round_id] = {
                    spec.round_id_key: round_id, "status": status,
                    "ended_at": event.get("time", ""),
                }
    # A failed-cycle event may be emitted before the remaining work in that
    # cycle finishes (notably stream toggle). Wait for the next round to start.
    finished = [item for number, item in sorted(rounds.items())
                if number < highest_round or item["status"] == "passed"]
    if not any(item["status"] == "failed" for item in finished):
        raise PackagingError("尚无已结束的失败轮次；若当前轮刚失败，请等下一轮开始后重试")
    return {spec.rounds_key: finished}, spec


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
    *,
    snapshot: bool = False,
    test_id: Optional[str] = None,
) -> Path:
    with tempfile.TemporaryDirectory(prefix="stress-snapshot-") as directory:
        return _package_results(result_directory, output, progress, snapshot, test_id, Path(directory))


def _package_results(result_directory, output, progress, snapshot, test_id, staging) -> Path:
    root = result_directory.expanduser().resolve()
    if not root.is_dir():
        raise PackagingError(f"result directory not found: {root}")
    if snapshot:
        details, spec = _snapshot_details(root, test_id, staging / "events.jsonl")
    else:
        details, spec = _read_result(root)
    files = _collect_files(root, details, spec)
    replacements = {}
    if snapshot:
        # Final reports may appear while packaging; the snapshot has its own
        # round inventory instead of claiming to be a completed test result.
        files -= {root / "result.json", root / "summary.md"}
        replacements[root / "events.jsonl"] = staging / "events.jsonl"
        selected = _selected_rounds(details, spec)
        metadata = {
            "snapshot": True,
            "test_id": test_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "selection": "failed rounds and up to two completed rounds before/after",
            "selected_rounds": [item[spec.round_id_key] for item in selected],
            "failed_rounds": [item[spec.round_id_key] for item in selected if item["status"] == "failed"],
            "details": details,
            "shared_log_scope": "Shared logs contain their full captured prefix, not only selected rounds.",
        }
        metadata_path = staging / "packaging_snapshot.json"
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        replacements[root / "packaging_snapshot.json"] = metadata_path
        files.update(replacements)
        for name in ROOT_FILES:
            source = root / name
            if source in files and source not in replacements and source.suffix == ".json":
                target = staging / name
                target.write_bytes(source.read_bytes())
                replacements[source] = target
    output_path = (
        output.expanduser().resolve()
        if output is not None
        else root.with_name(root.name + ("_snapshot.tar.gz" if snapshot else ".tar.gz"))
    )
    try:
        paths = sorted(files - {output_path})
        sizes = {path: replacements.get(path, path).stat().st_size for path in paths}
        total_bytes = sum(sizes.values())
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
            with tarfile.open(temporary_path, "w:gz", compresslevel=1, dereference=True) as archive:
                archive.copybufsize = 1024 * 1024
                for path in paths:
                    current_file = str(path.relative_to(root))
                    report(force=True)
                    with replacements.get(path, path).open("rb") as source:
                        info = archive.gettarinfo(
                            arcname=f"{root.name}/{current_file}", fileobj=source
                        )
                        # Appending logs cannot extend the snapshot or prevent
                        # completion. A truncated source fails without publishing.
                        info.size = sizes[path]
                        archive.addfile(info, ProgressReader(source, advance))
                report("finalizing", force=True)
            temporary_path.replace(output_path)
        report("completed", force=True)
    except (OSError, tarfile.TarError) as exc:
        raise PackagingError(f"failed to create archive: {exc}") from exc
    return output_path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Package failure windows or the last five rounds of a standalone stress test."
    )
    parser.add_argument("result_directory", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--progress-json", action="store_true",
                        help="Emit byte progress as JSON lines on stdout")
    parser.add_argument("--snapshot", action="store_true",
                        help="Package completed failure windows while the test keeps running")
    parser.add_argument("--test-id", choices=sorted(TEST_SPECS),
                        help="Required with --snapshot")
    args = parser.parse_args(argv)
    if args.snapshot and not args.test_id:
        parser.error("--snapshot requires --test-id")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        progress = (lambda value: print(json.dumps(value), flush=True)) if args.progress_json else None
        output = package_results(args.result_directory, args.output, progress,
                                 snapshot=args.snapshot, test_id=args.test_id)
    except PackagingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
