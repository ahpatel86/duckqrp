
from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from qrp import cli

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "study" / "demo_type2.json"
DATA = Path("/tmp/qrp_data/100k")


def _event_stream(log_dir: Path) -> list[dict]:
    files = list(log_dir.glob("*.jsonl"))
    assert len(files) == 1, f"expected exactly one JSONL stream, got {files}"
    return [json.loads(line) for line in files[0].read_text().splitlines()]


def _log_text(log_dir: Path) -> str:
    files = list(log_dir.glob("*.log"))
    assert len(files) == 1, f"expected exactly one .log, got {files}"
    return files[0].read_text()


def _run_cli(argv: list[str]) -> int:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return cli.main(argv)


def _argv(indata: Path, out: Path | None,
          log_dir: Path | None) -> list[str]:
    argv = ["run", "--study", str(STUDY), "--indata", str(indata)]
    if out is not None:
        argv += ["--out", str(out)]
    if log_dir is not None:
        argv += ["--log-dir", str(log_dir)]
    argv += ["--threads", "1", "--quiet"]
    return argv


def _stderr_lines(capsys) -> list[str]:
    return [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]


def test_sigint_mid_run_writes_the_cancelled_terminal_record(
        monkeypatch, tmp_path, capsys) -> None:
    out = tmp_path / "out"
    log_dir = tmp_path / "logs"

    def interrupted_run(*args, **kwargs):
        # Outputs are partly on disk when the SIGINT lands, so the
        # manifest assertion below is about the real invariant, not an
        # empty directory.
        out.mkdir(parents=True, exist_ok=True)
        (out / "partial.parquet").write_bytes(b"partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run", interrupted_run)

    rc = _run_cli(_argv(DATA, out, log_dir))

    assert rc == 130, "a SIGINTed run exits 130 (128+SIGINT)"
    lines = _stderr_lines(capsys)
    assert len(lines) == 1 and "Cancelled" in lines[0], (
        "exactly one plain-language diagnostic line on stderr; "
        f"got {lines!r}")

    recs = _event_stream(log_dir)
    last = recs[-1]
    assert last["type"] == "RunFinished", (
        "the JSONL stream ends with a terminal record")
    assert last["cancelled"] is True
    # cancelled is its own state: not a failure, error stays empty.
    assert last["ok"] is True, "a cancellation is not emitted as ok=False"
    assert last["error"] == "", "the error field stays empty on cancellation"
    assert not any(r["type"] == "LogMessage" and r.get("level") == "error"
                   for r in recs), "no ERROR LogMessage for a cancellation"

    log = _log_text(log_dir)
    assert "CANCELLED" in log, (
        "the .log carries the CANCELLED outcome where FAILED goes")
    assert "FAILED" not in log

    assert (out / "partial.parquet").exists()
    assert not (out / "manifest.json").exists(), (
        "manifest-last: a cancelled run writes no manifest, so the "
        "absence still marks the run incomplete")


def test_sigint_before_engine_construction_still_records(
        monkeypatch, tmp_path, capsys) -> None:
    out = tmp_path / "out"
    log_dir = tmp_path / "logs"

    class InterruptingEngine:
        def __init__(self, *args, **kwargs):
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "Engine", InterruptingEngine)

    rc = _run_cli(_argv(DATA, out, log_dir))

    assert rc == 130
    lines = _stderr_lines(capsys)
    assert len(lines) == 1 and "Cancelled" in lines[0]

    recs = _event_stream(log_dir)
    last = recs[-1]
    assert last["type"] == "RunFinished", (
        "the terminal record is written even when the engine never existed")
    assert last["cancelled"] is True
    assert last["ok"] is True
    assert last["error"] == ""
    assert "CANCELLED" in _log_text(log_dir)
    assert not (out / "manifest.json").exists()


def test_sigint_without_log_dir_still_exits_130(
        monkeypatch, tmp_path, capsys) -> None:
    out = tmp_path / "out"

    def interrupted_run(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run", interrupted_run)

    rc = _run_cli(_argv(DATA, out, None))

    assert rc == 130
    lines = _stderr_lines(capsys)
    assert len(lines) == 1 and "Cancelled" in lines[0]
    assert not list(tmp_path.rglob("*.log")), (
        "no --log-dir: no .log anywhere")
    assert not list(tmp_path.rglob("*.jsonl")), (
        "no --log-dir: no .jsonl anywhere")
    assert not (out / "manifest.json").exists()


@pytest.mark.skipif(not DATA.exists(),
                    reason="run tools/gen_synthetic.py first")
def test_success_path_is_unchanged(tmp_path):
    out = tmp_path / "out"
    log_dir = tmp_path / "logs"

    rc = _run_cli(_argv(DATA, out, log_dir))

    assert rc == 0, "the success path still exits 0"
    assert (out / "manifest.json").exists(), (
        "manifest-last: a completed run still writes the manifest")
    recs = _event_stream(log_dir)
    last = recs[-1]
    assert last["type"] == "RunFinished"
    assert last["ok"] is True
    assert last["cancelled"] is False
    assert last["error"] == ""
    assert "OK in" in _log_text(log_dir)
