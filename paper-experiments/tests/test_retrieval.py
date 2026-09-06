import json

import pytest

from paper_experiments import retrieval
from paper_experiments.prepare import file_inventory, write_json


def bundle_fixture(tmp_path, *, toolchain="leanprover/lean4:v4.28.0-rc1", revision=None):
    bundle = tmp_path / "bundle"
    (bundle / "dependencies" / "mathlib" / "Mathlib").mkdir(parents=True)
    (bundle / "dependencies" / "mathlib" / "Mathlib" / "Algebra.lean").write_text(
        "theorem identity (n : Nat) : n = n := rfl\n"
    )
    spec = json.loads((retrieval.ROOT / "configs" / "putnam2025.json").read_text())
    write_json(
        bundle / "manifest.json",
        {
            "lean_toolchain": toolchain,
            "mathlib_revision": revision or spec["mathlib_revision"],
            "files": file_inventory(bundle),
        },
    )
    return bundle


def test_native_retrieval_pins_matching_library_and_records_version_basis(tmp_path):
    bundle = bundle_fixture(tmp_path)
    receipt = retrieval.prepare_native_retrieval(bundle, tmp_path / "retrieval")
    metadata = retrieval.validate_registered_retrieval(
        {"bundle": str(bundle), "retrieval": receipt}
    )
    assert metadata["tool_config"] == {
        "tool_type": "search_lean_search",
        "server_url": "https://leansearch.net",
        "max_results": 6,
        "timeout": 60,
        "max_retries": 3,
        "retry_delay": 2,
    }
    assert metadata["mathlib_release"] == "v4.28.0-rc1"
    assert metadata["lean_toolchain"] == "leanprover/lean4:v4.28.0-rc1"
    assert "operator-confirmed" in metadata["version_basis"]
    assert "index_sha256" not in metadata
    assert not (tmp_path / "retrieval" / "mathlib.sqlite3").exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"toolchain": "leanprover/lean4:v4.26.0"},
        {"revision": "f" * 40},
    ],
)
def test_mismatched_experiment_version_rejected_before_registration(tmp_path, kwargs):
    bundle = bundle_fixture(tmp_path, **kwargs)
    with pytest.raises(ValueError, match="versions differ"):
        retrieval.prepare_native_retrieval(bundle, tmp_path / "retrieval")
    assert not (tmp_path / "retrieval").exists()


@pytest.mark.parametrize("filename", ["metadata.json", "openapi.json"])
def test_frozen_search_metadata_cannot_be_swapped(tmp_path, filename):
    bundle = bundle_fixture(tmp_path)
    receipt = retrieval.prepare_native_retrieval(bundle, tmp_path / "retrieval")
    path = tmp_path / "retrieval" / filename
    changed = json.loads(path.read_text())
    changed["tamper"] = True
    write_json(path, changed)
    with pytest.raises(ValueError, match="changed"):
        retrieval.validate_registered_retrieval({"bundle": str(bundle), "retrieval": receipt})


def test_registered_mathlib_sources_cannot_change(tmp_path):
    bundle = bundle_fixture(tmp_path)
    receipt = retrieval.prepare_native_retrieval(bundle, tmp_path / "retrieval")
    (bundle / "dependencies" / "mathlib" / "Mathlib" / "Algebra.lean").write_text("changed")
    with pytest.raises(ValueError, match="integrity"):
        retrieval.validate_registered_retrieval({"bundle": str(bundle), "retrieval": receipt})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("server_url", "https://example.org"),
        ("mathlib_release", "v4.26.0"),
        ("max_results", 10),
    ],
)
def test_search_settings_cannot_enable_different_service_or_version(tmp_path, field, value):
    config = json.loads(retrieval.CONFIG.read_text())
    config[field] = value
    write_json(tmp_path / "config.json", config)
    with pytest.raises(ValueError):
        retrieval.registered_settings(tmp_path / "config.json")
