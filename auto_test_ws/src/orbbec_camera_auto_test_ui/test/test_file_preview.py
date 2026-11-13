import json
import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(PACKAGE_ROOT.parent / "orbbec_camera_auto_test"))

from orbbec_camera_auto_test_ui import file_preview, server  # noqa: E402
from orbbec_camera_auto_test_ui.run_manager import TestJob as Job  # noqa: E402
from orbbec_camera_auto_test_ui.standalone import validate_result_contract  # noqa: E402


@pytest.fixture(autouse=True)
def clear_result_cache():
    file_preview._cached_result.cache_clear()
    yield
    file_preview._cached_result.cache_clear()


def result_payload():
    return {
        "schema_version": 1,
        "test_id": "stream_toggle_stress_test",
        "run_id": "example",
        "status": "passed",
        "started_at": "2026-11-05T15:13:33",
        "ended_at": "2026-11-09T19:13:38",
        "duration_seconds": 360000,
        "request": {},
        "environment": {},
        "summary": {"passed_cycles": 9236, "failed_cycles": 0},
        "warnings": [],
        "details": {"cycles": [], "completed_cycles": 9236},
        "artifacts": [],
        "error": None,
    }


def make_job(root):
    return Job(
        run_id="example",
        mode="standalone:stream_toggle_stress_test",
        run_root=root,
        command_lines=[],
        shell="bash",
        runner_type="standalone",
        test_id="stream_toggle_stress_test",
        standalone_rounds_supported=True,
        standalone_round_total=10000,
    )


def test_sparse_three_gib_log_preview_is_bounded(tmp_path, monkeypatch):
    run = tmp_path / "example"
    run.mkdir()
    path = run / "camera.launch.log"
    with path.open("wb") as stream:
        stream.seek(3 * 1024 ** 3)
        stream.write(b"\nlast camera message\n")
    monkeypatch.setattr(server, "UI_RESULTS_ROOT", tmp_path)
    original = Path.read_text

    def guarded_read_text(self, *args, **kwargs):
        assert self != path, "large log must never be read in full"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    report = server.get_run("example")
    assert report["logs"][path.name].endswith("last camera message\n")
    assert len(report["logs"][path.name]) <= 20000
    assert path.stat().st_size > 3 * 1024 ** 3


def test_event_tail_handles_long_lines_utf8_and_partial_record(tmp_path):
    path = tmp_path / "events.jsonl"
    with path.open("wb") as stream:
        stream.write(json.dumps({"message": "x" * file_preview.PREVIEW_BYTES}).encode() + b"\n")
        for number in range(1100):
            stream.write(json.dumps({"number": number, "message": "相机"}, ensure_ascii=False).encode() + b"\n")
        stream.write(b'{"unfinished":')
    events = file_preview.read_event_tail(path, limit=25)
    assert [event["number"] for event in events] == list(range(1075, 1100))
    assert events[-1]["message"] == "相机"
    assert file_preview.read_event_tail(path, limit=0) == []


@pytest.mark.parametrize("chunk_size", [7, 64 * 1024])
def test_large_result_preserves_summary_after_skipped_rounds(tmp_path, monkeypatch, chunk_size):
    monkeypatch.setattr(file_preview, "READ_CHUNK", chunk_size)
    monkeypatch.setattr(file_preview, "SMALL_RESULT_BYTES", 0)
    payload = result_payload()
    payload["details"]["cycles"] = [{"cycle": 1, "status": "passed", "text": '相机 \\" [] {}' * 2000}]
    payload["details"]["failure_details"] = [{"camera": "camera", "reason": "frame gap"}]
    payload["error"] = "a final error after the large details object"
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    preview = file_preview.read_result_preview(path)
    assert preview["summary"] == payload["summary"]
    assert preview["details"]["cycles"] == []
    assert preview["details"]["completed_cycles"] == 9236
    assert preview["details"]["failure_details"] == payload["details"]["failure_details"]
    assert preview["error"] == payload["error"]
    assert preview["_preview"]["truncated"]
    validated, errors = validate_result_contract(path, "stream_toggle_stress_test")
    assert not errors
    assert validated["status"] == "passed"


def test_result_cache_reuses_scan_and_invalidates_on_replace(tmp_path, monkeypatch):
    path = tmp_path / "result.json"
    payload = result_payload()
    path.write_text(json.dumps(payload))
    first = file_preview.read_result_preview(path)
    assert first == payload
    assert file_preview.read_result_preview(path) is first
    replacement = tmp_path / "replacement.json"
    payload["status"] = "failed"
    replacement.write_text(json.dumps(payload))
    replacement.replace(path)
    assert file_preview.read_result_preview(path)["status"] == "failed"
    assert file_preview._cached_result.cache_info().hits == 1


@pytest.mark.parametrize("ending", ["", "]}", "}} trailing"])
def test_truncated_or_unbalanced_large_results_are_rejected(tmp_path, monkeypatch, ending):
    monkeypatch.setattr(file_preview, "SMALL_RESULT_BYTES", 0)
    path = tmp_path / "result.json"
    path.write_text('{"details":{"cycles":[{"message":"x"}' + ending)
    assert file_preview.result_preview_or_default(path, {}) == {}


def test_completed_snapshot_uses_large_result_summary_without_loading_rounds(tmp_path, monkeypatch):
    monkeypatch.setattr(file_preview, "SMALL_RESULT_BYTES", 0)
    payload = result_payload()
    payload["details"]["cycles"] = [
        {"cycle": cycle, "status": "passed"} for cycle in range(1, 9237)
    ]
    (tmp_path / "result.json").write_text(json.dumps(payload))
    job = make_job(tmp_path)
    job.status = "passed"
    snapshot = job.snapshot()
    assert snapshot["standalone"]["progress"]["successes"] == 9236
    assert snapshot["standalone"]["result"]["details"]["cycles"] == []
    assert job.snapshot()["standalone"]["result"] is snapshot["standalone"]["result"]


def test_event_counts_stream_chunks_and_recover_after_partial_or_replaced_file(tmp_path):
    path = tmp_path / "events.jsonl"
    success = json.dumps({"event": "progress", "phase": "completed-cycle"}).encode() + b"\n"
    with path.open("wb") as stream:
        stream.write(success * 3000)
        stream.write(b'{"message":"' + b"x" * (file_preview.PREVIEW_BYTES * 2))
    job = make_job(tmp_path)
    assert job._update_standalone_event_counts() == {"successes": 3000, "failures": 0}
    assert len(job.standalone_event_remainder) <= file_preview.PREVIEW_BYTES
    with path.open("ab") as stream:
        stream.write(b'"}\n' + success + b'{"event":"fail')
    assert job._update_standalone_event_counts() == {"successes": 3001, "failures": 0}
    with path.open("ab") as stream:
        stream.write(b'ure"}\n')
    assert job._update_standalone_event_counts() == {"successes": 3001, "failures": 1}
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(success)
    replacement.replace(path)
    assert job._update_standalone_event_counts() == {"successes": 1, "failures": 0}


def test_event_counts_use_the_identity_of_the_file_actually_opened(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    success = b'{"event":"progress","phase":"completed-cycle"}\n'
    path.write_bytes(success)
    job = make_job(tmp_path)
    assert job._update_standalone_event_counts()["successes"] == 1
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(success * 3)
    original_open = Path.open

    def replace_before_open(self, *args, **kwargs):
        if self == path:
            replacement.replace(path)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replace_before_open)
    assert job._update_standalone_event_counts() == {"successes": 3, "failures": 0}


def test_many_logs_have_a_combined_preview_budget(tmp_path):
    for number in range(60):
        (tmp_path / f"camera_{number}.log").write_text("相机" * 15000, encoding="utf-8")
    logs = server._read_log_previews(tmp_path)
    assert len(logs) <= 50
    assert sum(len(text.encode("utf-8")) for text in logs.values()) <= 4 * file_preview.PREVIEW_BYTES


@pytest.mark.parametrize("chunk_size", [1, 7, 64 * 1024])
@pytest.mark.parametrize("invalid", [
    'bare_identifier', '[1 2]', '{"x" 1}', '{"x":1,}', '[1,]',
    '"invalid\\q"', '"invalid\\u12xz"', '"line\nbreak"',
    '01', '1.', '1e', '-true', 'truefalse',
])
def test_invalid_json_in_skipped_values_is_rejected(tmp_path, monkeypatch, chunk_size, invalid):
    monkeypatch.setattr(file_preview, "READ_CHUNK", chunk_size)
    monkeypatch.setattr(file_preview, "SMALL_RESULT_BYTES", 0)
    path = tmp_path / "result.json"
    payload = result_payload()
    payload["details"]["cycles"] = ["INVALID_VALUE"]
    path.write_text(json.dumps(payload).replace('"INVALID_VALUE"', invalid))
    with pytest.raises(ValueError):
        json.loads(path.read_text())
    _, errors = validate_result_contract(path, payload["test_id"])
    assert errors and "invalid" in errors[0]


@pytest.mark.parametrize("collection,summary", [
    ("cycles", {"passed_cycles": 2, "failed_cycles": 1}),
    ("attempts", {"successful_restarts": 2, "launch_attempts": 4}),
    ("tests", {"passed_runs": 2, "completed_runs": 4}),
    ("runs", {"passed_runs": 2, "completed_runs": 4}),
])
def test_large_results_preserve_failures_and_exact_counts_without_events(tmp_path, collection, summary):
    failure = {"camera": "camera_1", "topic": "/camera/depth/image_raw", "reason": "no frames"}
    payload = result_payload()
    payload.update(status="failed", summary=summary)
    payload["details"] = {collection: [
        {"trace": "x" * file_preview.SMALL_RESULT_BYTES, "status": "passed"},
        {"status": "passed"},
        {"cycle": 3, "status": "failed", "error": "stream check failed",
         "operations": [{"profile_switch": {"failure_details": [failure]}}]},
        {"status": "interrupted"},
    ]}
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload))
    preview, errors = validate_result_contract(path, payload["test_id"])
    assert not errors
    assert preview["details"]["failure_details"] == [failure]
    assert preview["_preview"]["round_counts"][collection] == {
        "passed": 2, "failed": 1, "interrupted": 1,
    }
    assert [record["status"] for record in preview["details"][collection]] == ["failed"]
    job = make_job(tmp_path)
    job.status = "failed"
    progress = job.snapshot()["standalone"]["progress"]
    assert (progress["successes"], progress["failures"]) == (2, 1)


def test_failure_previews_stay_bounded_and_count_every_round(tmp_path, monkeypatch):
    monkeypatch.setattr(file_preview, "SMALL_RESULT_BYTES", 0)
    payload = result_payload()
    payload["status"] = "failed"
    failure = {"camera": "camera", "reason": "x" * 1000}
    payload["details"]["cycles"] = [
        {"cycle": number, "status": "failed", "error": "x" * 1000,
         "failure_details": [failure]}
        for number in range(200)
    ]
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload))
    preview = file_preview.read_result_preview(path)
    assert preview["_preview"]["round_counts"]["cycles"]["failed"] == 200
    assert len(preview["details"]["cycles"]) <= 10
    assert len(preview["details"]["failure_details"]) <= 50
    assert len(json.dumps(preview)) < 3 * file_preview.PREVIEW_BYTES


@pytest.mark.parametrize("chunk_size", [1, 7, 64 * 1024])
def test_valid_json_tokens_across_chunks_and_large_invocation(tmp_path, monkeypatch, chunk_size):
    monkeypatch.setattr(file_preview, "SMALL_RESULT_BYTES", 0)
    monkeypatch.setattr(file_preview, "READ_CHUNK", chunk_size)
    payload = result_payload()
    payload.update({"": "empty key", "…": "ellipsis key", "\ud800": "escaped surrogate key"})
    payload["invocation"] = {"command": "x" * 5000, "argv": ["x" * 5000], "cwd": "/tmp"}
    payload["details"]["cycles"] = [{
        "status": "passed", "tokens": [0, -12, 1.25, -2.5e-30, True, False, None,
                                      "相机 \\\"\b\f\n\r\t", {"": "…"}],
    }]
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload, ensure_ascii=True))
    preview, errors = validate_result_contract(path, payload["test_id"])
    assert not errors
    assert preview["_preview"]["round_counts"]["cycles"]["passed"] == 1
    assert isinstance(preview["invocation"]["command"], str)
    assert isinstance(preview["invocation"]["argv"], list)


def test_large_results_are_never_read_in_full(tmp_path, monkeypatch):
    payload = result_payload()
    payload["details"]["cycles"] = [
        {"trace": "x" * (2 * file_preview.SMALL_RESULT_BYTES), "status": "passed"}
    ]
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload))
    original_open = Path.open
    read_sizes = []

    class GuardedStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size=-1):
            assert 0 < size <= file_preview.READ_CHUNK, "large result must be streamed"
            read_sizes.append(size)
            return self.stream.read(size)

    def guarded_open(self, *args, **kwargs):
        stream = original_open(self, *args, **kwargs)
        return GuardedStream(stream) if self == path else stream

    monkeypatch.setattr(Path, "open", guarded_open)
    preview = file_preview.read_result_preview(path)
    assert read_sizes
    assert preview["_preview"]["round_counts"]["cycles"]["passed"] == 1
