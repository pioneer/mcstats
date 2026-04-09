"""Unit tests for mcstats core logic (no hardware required)."""
from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from mcstats.scanner import (
    NeighbourStats,
    RoiPath,
    SnrSample,
    _contact_hash,
    _filter_repeaters,
    _find_roi,
    _roi_path_from_config,
    _split_path_hashes,
)
from mcstats.config import DEFAULTS, load_config
from mcstats.cache import load_neighbours, save_neighbours
from mcstats.csv_export import write_csv


# ---------------------------------------------------------------------------
# RoiPath
# ---------------------------------------------------------------------------

class TestRoiPath:
    def test_direct_prefix(self):
        rp = RoiPath(roi_hash="e5", intermediate_hashes=[], hash_len=1)
        assert rp.prefix == ""

    def test_multihop_prefix(self):
        rp = RoiPath(roi_hash="e5", intermediate_hashes=["55", "b0"], hash_len=1)
        assert rp.prefix == "55,b0"

    def test_trace_to_direct(self):
        rp = RoiPath(roi_hash="e5", intermediate_hashes=[], hash_len=1)
        assert rp.trace_to("aa") == "e5,aa,e5"

    def test_trace_to_multihop(self):
        rp = RoiPath(roi_hash="e5", intermediate_hashes=["55", "b0"], hash_len=1)
        # fwd: 55,b0,e5,aa  ret: e5,b0,55
        assert rp.trace_to("aa") == "55,b0,e5,aa,e5,b0,55"

    def test_trace_roundtrip_alias(self):
        rp = RoiPath(roi_hash="e5", intermediate_hashes=["55"], hash_len=1)
        assert rp.trace_roundtrip("bb") == rp.trace_to("bb")

    def test_trace_to_roi_direct(self):
        rp = RoiPath(roi_hash="e5", intermediate_hashes=[], hash_len=1)
        assert rp.trace_to_roi() == "e5"

    def test_trace_to_roi_multihop(self):
        rp = RoiPath(roi_hash="e5", intermediate_hashes=["55", "b0"], hash_len=1)
        assert rp.trace_to_roi() == "55,b0,e5,b0,55"


# ---------------------------------------------------------------------------
# SnrSample / NeighbourStats
# ---------------------------------------------------------------------------

class TestNeighbourStats:
    def test_avg_out_basic(self):
        ns = NeighbourStats(name="R1", pub_key="aabb")
        ns.out_snr_samples = [SnrSample(value=5.0), SnrSample(value=3.0)]
        assert ns.avg_out(-30) == pytest.approx(4.0)

    def test_avg_in_with_timeout(self):
        ns = NeighbourStats(name="R1", pub_key="aabb")
        ns.in_snr_samples = [SnrSample(value=10.0), SnrSample(timed_out=True)]
        # (10 + -30) / 2 = -10
        assert ns.avg_in(-30) == pytest.approx(-10.0)

    def test_avg_empty(self):
        ns = NeighbourStats(name="R1", pub_key="aabb")
        assert ns.avg_out(-30) is None
        assert ns.avg_in(-30) is None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestContactHash:
    def test_1byte(self):
        c = {"public_key": "e5deadbeef"}
        assert _contact_hash(c) == "e5"

    def test_2byte(self):
        c = {"public_key": "e5deadbeef"}
        assert _contact_hash(c, hash_len=2) == "e5de"

    def test_pubkey_fallback(self):
        c = {"pubkey": "abcd1234"}
        assert _contact_hash(c) == "ab"


class TestSplitPathHashes:
    def test_basic(self):
        assert _split_path_hashes("55b0", 1, 2) == ["55", "b0"]

    def test_2byte_hash(self):
        assert _split_path_hashes("55aab0cc", 2, 2) == ["55aa", "b0cc"]

    def test_empty(self):
        assert _split_path_hashes("", 1, 0) == []


class TestFindRoi:
    def _repeaters(self):
        return [
            {"adv_name": "Kyiv_R1", "public_key": "e5abcd"},
            {"adv_name": "Kyiv_R2", "public_key": "b0ffee"},
        ]

    def test_by_name(self):
        roi = _find_roi(self._repeaters(), "Kyiv_R1")
        assert roi is not None
        assert roi["adv_name"] == "Kyiv_R1"

    def test_by_hex_hash(self):
        roi = _find_roi(self._repeaters(), "b0")
        assert roi is not None
        assert roi["adv_name"] == "Kyiv_R2"

    def test_not_found(self):
        assert _find_roi(self._repeaters(), "nope") is None


class TestRoiPathFromConfig:
    def test_empty(self):
        assert _roi_path_from_config("e5", "") is None

    def test_direct(self):
        rp = _roi_path_from_config("e5", "direct")
        assert rp is not None
        assert rp.intermediate_hashes == []
        assert rp.roi_hash == "e5"

    def test_hops(self):
        rp = _roi_path_from_config("e5", "55,b0")
        assert rp is not None
        assert rp.intermediate_hashes == ["55", "b0"]


class TestFilterRepeaters:
    def _repeaters(self):
        return [
            {"adv_name": "Kyiv_R1", "public_key": "aa"},
            {"adv_name": "Kyiv_R2", "public_key": "bb"},
            {"adv_name": "Lviv_R1", "public_key": "cc"},
        ]

    def test_no_filter(self):
        assert len(_filter_repeaters(self._repeaters(), "", "")) == 3

    def test_prefix_filter(self):
        result = _filter_repeaters(self._repeaters(), "Kyiv_", "")
        assert len(result) == 2
        assert all(r["adv_name"].startswith("Kyiv_") for r in result)

    def test_exclude_filter(self):
        result = _filter_repeaters(self._repeaters(), "", "Kyiv_R1,Lviv_R1")
        assert len(result) == 1
        assert result[0]["adv_name"] == "Kyiv_R2"

    def test_combined(self):
        result = _filter_repeaters(self._repeaters(), "Kyiv_", "Kyiv_R2")
        assert len(result) == 1
        assert result[0]["adv_name"] == "Kyiv_R1"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_load_defaults(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("serial_port: COM9\n")
        cfg = load_config(cfg_file)
        assert cfg["serial_port"] == "COM9"
        assert cfg["snr_samples"] == DEFAULTS["snr_samples"]

    def test_cli_override(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("snr_samples: 5\n")
        cfg = load_config(cfg_file, snr_samples=10)
        assert cfg["snr_samples"] == 10

    def test_none_override_ignored(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("snr_samples: 7\n")
        cfg = load_config(cfg_file, snr_samples=None)
        assert cfg["snr_samples"] == 7

    def test_missing_file(self):
        with pytest.raises(SystemExit):
            load_config("nonexistent.yaml")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class TestCache:
    def test_roundtrip(self, tmp_path):
        neighbours = [{"adv_name": "R1", "public_key": "aa"}]
        p = save_neighbours("TestROI", neighbours, str(tmp_path))
        assert p.exists()
        loaded = load_neighbours("TestROI", str(tmp_path))
        assert loaded == neighbours

    def test_missing(self, tmp_path):
        assert load_neighbours("Missing", str(tmp_path)) is None


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

class TestCsvExport:
    def test_write_csv(self, tmp_path):
        stats = [
            NeighbourStats(
                name="R1",
                pub_key="aabb",
                out_snr_samples=[SnrSample(value=5.0), SnrSample(value=3.0)],
                in_snr_samples=[SnrSample(value=-2.0), SnrSample(timed_out=True)],
            ),
        ]
        csv_path = tmp_path / "out.csv"
        result = write_csv(stats, -30.0, csv_path, "TestROI")
        assert result.exists()
        text = result.read_text()
        lines = text.strip().split("\n")
        assert len(lines) == 3  # metadata + header + 1 data row
        assert "R1" in lines[2]
        assert "5.0" in lines[2]
        assert "TOUT" in lines[2]

    def test_empty_stats(self, tmp_path):
        csv_path = tmp_path / "empty.csv"
        result = write_csv([], -30.0, csv_path)
        assert result.exists()


# ---------------------------------------------------------------------------
# Display (smoke test — just ensure no crashes)
# ---------------------------------------------------------------------------

class TestDisplay:
    def test_show_stats_empty(self):
        from mcstats.display import show_stats
        # Should not raise
        show_stats([], -30)

    def test_show_stats_data(self):
        from mcstats.display import show_stats
        stats = [
            NeighbourStats(
                name="R1",
                pub_key="aabb",
                out_snr_samples=[SnrSample(value=5.0)],
                in_snr_samples=[SnrSample(value=-2.0)],
            ),
        ]
        show_stats(stats, -30, "TestROI", "aa")

    def test_show_repeaters(self):
        from mcstats.display import show_repeaters
        repeaters = [
            {"adv_name": "R1", "public_key": "aabbccdd", "out_path_len": 0, "out_path": "", "type": 2},
        ]
        show_repeaters(repeaters)
