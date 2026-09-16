"""Tests for the durable-IO helpers in common.py."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402
from common import (DEFAULT_SOURCE_FOLDERS, JsonFileError, load_json,
                    source_folders, write_json)  # noqa: E402


@pytest.fixture(autouse=True)
def clear_pdftotext_probe_cache():
    common._pdftotext_available.cache_clear()
    yield
    common._pdftotext_available.cache_clear()


def test_write_json_round_trips(tmp_path):
    p = tmp_path / "x.json"
    write_json(p, {"a": 1, "label": "Cafe \u00e9", "b": [2, 3]})
    assert json.loads(p.read_text(encoding="utf-8")) == {
        "a": 1, "label": "Cafe \u00e9", "b": [2, 3],
    }


def test_load_json_reads_utf8_independent_of_platform_default(tmp_path):
    p = tmp_path / "unicode.json"
    p.write_bytes('{"merchant": "Caf\u00e9 \u6771\u4eac"}'.encode("utf-8"))
    assert load_json(p, {}) == {"merchant": "Caf\u00e9 \u6771\u4eac"}


def test_write_json_leaves_no_tmp_files(tmp_path):
    p = tmp_path / "x.json"
    write_json(p, {"ok": True})
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_write_json_creates_parent_dir(tmp_path):
    p = tmp_path / "nested" / "deep" / "x.json"
    write_json(p, [1, 2])
    assert json.loads(p.read_text(encoding="utf-8")) == [1, 2]


def test_write_json_does_not_corrupt_existing_on_reader_view(tmp_path):
    """os.replace is atomic — the destination is always either the old or the new
    complete file, never a partial. Verify a second write fully replaces."""
    p = tmp_path / "x.json"
    write_json(p, {"v": 1})
    write_json(p, {"v": 2})
    assert json.loads(p.read_text(encoding="utf-8")) == {"v": 2}


def test_load_json_missing_returns_default(tmp_path):
    assert load_json(tmp_path / "nope.json", "DEFAULT") == "DEFAULT"


def test_load_json_corrupt_raises_without_replacing_file(tmp_path):
    """Corrupt user data must not be silently treated as an empty overlay."""
    p = tmp_path / "broken.json"
    corrupt = '{ "half written'
    p.write_text(corrupt, encoding="utf-8")
    with pytest.raises(JsonFileError, match="Invalid JSON"):
        load_json(p, {"fallback": True})
    assert p.read_text(encoding="utf-8") == corrupt


def test_load_json_empty_file_raises(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text("", encoding="utf-8")
    with pytest.raises(JsonFileError, match="Invalid JSON"):
        load_json(p, [])


def test_source_folders_uses_defaults_when_not_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "RULES", tmp_path / "rules")
    assert source_folders() == DEFAULT_SOURCE_FOLDERS


def test_source_folders_accepts_project_relative_overrides(tmp_path, monkeypatch):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "sources.json").write_text(
        json.dumps({
            "chase": "accounts/checking",
            "csp": "Sapphire",
            "vx": "TravelCard",
            "venmo": "P2P",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(common, "RULES", rules)
    assert source_folders() == {
        "chase": "accounts/checking",
        "csp": "Sapphire",
        "vx": "TravelCard",
        "venmo": "P2P",
    }


def test_source_folders_normalizes_windows_separators(tmp_path, monkeypatch):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "sources.json").write_text(
        json.dumps({"chase": r"accounts\checking"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(common, "RULES", rules)
    assert source_folders()["chase"] == "accounts/checking"


@pytest.mark.parametrize("config, message", [
    ({"unknown": "Folder"}, "unsupported source key"),
    ({"chase": "../outside"}, "must be relative"),
    ({"chase": "/outside"}, "must be relative"),
    ({"chase": r"\outside"}, "must be relative"),
    ({"chase": r"C:\outside"}, "must be relative"),
    ({"chase": "Shared", "csp": "Shared"}, "must be distinct"),
    ({"chase": "shared", "csp": "SHARED"}, "must be distinct"),
])
def test_source_folders_rejects_invalid_mappings(tmp_path, monkeypatch, config, message):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "sources.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(common, "RULES", rules)
    with pytest.raises(JsonFileError, match=message):
        source_folders()


def test_pdftext_raises_when_extractor_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "_pdftotext_available", lambda: True)
    failed = subprocess.CompletedProcess(
        ["pdftotext", "-layout", str(tmp_path / "broken.pdf"), "-"],
        returncode=2, stdout="", stderr="invalid PDF",
    )
    monkeypatch.setattr(common.subprocess, "run", lambda *args, **kwargs: failed)

    with pytest.raises(subprocess.CalledProcessError) as exc:
        common.pdftext(tmp_path / "broken.pdf")
    assert exc.value.returncode == 2
    assert exc.value.stderr == "invalid PDF"


def test_pdftext_rejects_empty_successful_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "_pdftotext_available", lambda: True)
    empty = subprocess.CompletedProcess(
        ["pdftotext", "-layout", str(tmp_path / "blank.pdf"), "-"],
        returncode=0, stdout=" \n", stderr="",
    )
    monkeypatch.setattr(common.subprocess, "run", lambda *args, **kwargs: empty)
    monkeypatch.setattr(
        common,
        "_pdftext_fallback",
        lambda path: (_ for _ in ()).throw(ValueError("still empty")),
    )

    with pytest.raises(ValueError, match="produced no text"):
        common.pdftext(tmp_path / "blank.pdf")


def test_pdftext_retries_python_extractors_when_poppler_has_no_text(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "_pdftotext_available", lambda: True)
    empty = subprocess.CompletedProcess(
        ["pdftotext", "-layout", str(tmp_path / "scan.pdf"), "-"],
        returncode=0, stdout=" \n", stderr="",
    )
    monkeypatch.setattr(common.subprocess, "run", lambda *args, **kwargs: empty)
    monkeypatch.setattr(common, "_pdftext_fallback", lambda path: "OCRed text")

    assert common.pdftext(tmp_path / "scan.pdf") == "OCRed text"


def test_pdftotext_probe_rejects_a_broken_executable(monkeypatch):
    broken = subprocess.CompletedProcess(
        ["pdftotext", "-v"], returncode=127, stdout="", stderr="missing DLL",
    )
    monkeypatch.setattr(common.subprocess, "run", lambda *args, **kwargs: broken)

    assert not common._pdftotext_available()


def test_pdftext_uses_pymupdf_when_poppler_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "_pdftotext_available", lambda: False)
    monkeypatch.setattr(common, "_pdftext_pymupdf", lambda path: "fallback text")

    assert common.pdftext(tmp_path / "statement.pdf") == "fallback text"


def test_pdftext_uses_pypdf_when_pymupdf_cannot_read(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "_pdftotext_available", lambda: False)
    monkeypatch.setattr(
        common,
        "_pdftext_pymupdf",
        lambda path: (_ for _ in ()).throw(ValueError("cannot read")),
    )
    monkeypatch.setattr(common, "_pdftext_pypdf", lambda path: "final fallback")

    assert common.pdftext(tmp_path / "statement.pdf") == "final fallback"
