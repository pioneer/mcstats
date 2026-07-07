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
    _order_candidates,
    _ordered_subsequences,
    _parse_corridors,
    _path_avg_snr,
    _path_min_snr,
    _roi_path_from_config,
    _score_path,
    _select_candidates,
    _select_tail_candidates,
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

    def test_trace_to_roi_hash_equals_last_hop(self):
        # ROI hash 55 with path "55" — no redundant repeated 55
        rp = RoiPath(roi_hash="55", intermediate_hashes=["55"], hash_len=1)
        assert rp.trace_to("b3") == "55,b3,55"
        assert rp.trace_to_roi() == "55"

    def test_hops_to_roi_len(self):
        assert RoiPath("e5", []).hops_to_roi_len == 1
        assert RoiPath("e5", ["55", "b0"]).hops_to_roi_len == 3
        assert RoiPath("55", ["55"]).hops_to_roi_len == 1


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


class TestPathFinding:
    def test_path_min_snr_basic(self):
        payload = {"path": [{"snr": 5.0}, {"snr": -2.0}, {"snr": 3.0}]}
        assert _path_min_snr(payload) == pytest.approx(-2.0)

    def test_path_min_snr_ignores_non_numeric(self):
        payload = {"path": [{"snr": 4.0}, {"snr": None}, {}]}
        assert _path_min_snr(payload) == pytest.approx(4.0)

    def test_path_min_snr_empty(self):
        assert _path_min_snr({"path": []}) is None
        assert _path_min_snr(None) is None

    def test_path_avg_snr_basic(self):
        payload = {"path": [{"snr": 6.0}, {"snr": -2.0}, {"snr": 2.0}]}
        assert _path_avg_snr(payload) == pytest.approx(2.0)

    def test_path_avg_snr_ignores_non_numeric(self):
        payload = {"path": [{"snr": 4.0}, {"snr": None}, {}]}
        assert _path_avg_snr(payload) == pytest.approx(4.0)

    def test_path_avg_snr_empty(self):
        assert _path_avg_snr({"path": []}) is None
        assert _path_avg_snr(None) is None

    def test_score_path_stability_beats_hops(self):
        # A more-stable path ranks better even with more hops
        assert _score_path(8.0, 8.0, 3) < _score_path(2.0, 2.0, 1)

    def test_score_path_higher_min_snr_wins(self):
        # Higher weakest-hop SNR ranks better regardless of avg
        assert _score_path(8.0, 8.0) < _score_path(3.0, 20.0)

    def test_score_path_avg_breaks_min_tie(self):
        # Equal min SNR → higher average wins
        assert _score_path(5.0, 9.0) < _score_path(5.0, 6.0)

    def test_score_path_hops_last_tiebreak(self):
        # Equal min and avg → fewer hops wins
        assert _score_path(5.0, 5.0, 1) < _score_path(5.0, 5.0, 3)

    def test_score_path_none_snr_is_worst(self):
        assert _score_path(None, None) > _score_path(-100.0, -100.0)

    def test_order_candidates_descending_snr(self):
        snr_map = {"aa": 3.0, "bb": 9.0, "cc": -1.0}
        assert _order_candidates(["aa", "bb", "cc"], snr_map) == ["bb", "aa", "cc"]

    def test_order_candidates_unknown_last(self):
        snr_map = {"aa": 3.0, "bb": None}
        assert _order_candidates(["bb", "aa", "dd"], snr_map)[0] == "aa"
        # bb (None) and dd (missing) both sort to the end
        assert set(_order_candidates(["bb", "aa", "dd"], snr_map)[1:]) == {"bb", "dd"}

    def test_ordered_subsequences_longest_first(self):
        seqs = _ordered_subsequences(["a", "b", "c"], max_len=3)
        # Longest first, order preserved within each subsequence
        assert seqs[0] == ["a", "b", "c"]
        assert ["a", "b"] in seqs and ["a", "c"] in seqs and ["b", "c"] in seqs
        assert ["a"] in seqs and ["b"] in seqs and ["c"] in seqs
        # 2^3 - 1 = 7 non-empty subsequences
        assert len(seqs) == 7
        # Every subsequence preserves the original relative order
        for s in seqs:
            idxs = [["a", "b", "c"].index(x) for x in s]
            assert idxs == sorted(idxs)

    def test_ordered_subsequences_respects_max_len(self):
        seqs = _ordered_subsequences(["a", "b", "c"], max_len=1)
        assert seqs == [["a"], ["b"], ["c"]]

    def test_ordered_subsequences_empty(self):
        assert _ordered_subsequences([], max_len=3) == []


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


class TestSelectCandidates:
    def _repeaters(self):
        return [
            {"adv_name": "Kyiv_R1", "public_key": "aa11"},
            {"adv_name": "Kyiv_R2", "public_key": "bb22"},
            {"adv_name": "Lviv_R1", "public_key": "cc33"},
        ]

    def test_empty_keeps_all(self):
        assert len(_select_candidates(self._repeaters(), "")) == 3

    def test_by_name(self):
        result = _select_candidates(self._repeaters(), "Kyiv_R1,Lviv_R1")
        assert [r["adv_name"] for r in result] == ["Kyiv_R1", "Lviv_R1"]

    def test_by_hex_hash(self):
        result = _select_candidates(self._repeaters(), "bb")
        assert len(result) == 1
        assert result[0]["adv_name"] == "Kyiv_R2"

    def test_preserves_allowlist_order(self):
        result = _select_candidates(self._repeaters(), "Lviv_R1,Kyiv_R1")
        assert [r["adv_name"] for r in result] == ["Lviv_R1", "Kyiv_R1"]

    def test_ignores_unknown(self):
        result = _select_candidates(self._repeaters(), "Kyiv_R1,Nonexistent")
        assert [r["adv_name"] for r in result] == ["Kyiv_R1"]

    def test_dedups(self):
        result = _select_candidates(self._repeaters(), "Kyiv_R1,aa,Kyiv_R1")
        assert [r["adv_name"] for r in result] == ["Kyiv_R1"]


class TestParseCorridors:
    def _repeaters(self):
        return [
            {"adv_name": "R55", "public_key": "5500"},
            {"adv_name": "Rb0", "public_key": "b000"},
            {"adv_name": "Re5", "public_key": "e500"},
            {"adv_name": "Rc0", "public_key": "c000"},
        ]

    def test_single_corridor(self):
        corridors = _parse_corridors("R55,Rb0,Re5", self._repeaters())
        assert corridors == [["55", "b0", "e5"]]

    def test_multiple_corridors(self):
        corridors = _parse_corridors("R55,Rb0,Re5;R55,Rb0,Rc0", self._repeaters())
        assert corridors == [["55", "b0", "e5"], ["55", "b0", "c0"]]

    def test_preserves_order_within_corridor(self):
        corridors = _parse_corridors("Re5,Rb0,R55", self._repeaters())
        assert corridors == [["e5", "b0", "55"]]

    def test_skips_unresolvable_waypoints(self):
        corridors = _parse_corridors("R55,Nope,Re5", self._repeaters())
        assert corridors == [["55", "e5"]]

    def test_drops_empty_corridors(self):
        corridors = _parse_corridors("R55,Rb0;;Nope", self._repeaters())
        assert corridors == [["55", "b0"]]

    def test_dedups_identical_corridors(self):
        corridors = _parse_corridors("R55,Rb0;R55,Rb0", self._repeaters())
        assert corridors == [["55", "b0"]]

    def test_empty_spec(self):
        assert _parse_corridors("", self._repeaters()) == []


class TestSelectTailCandidates:
    def _repeaters(self):
        return [
            {"adv_name": "Kyiv_Troieshchyna_R1", "public_key": "1400"},
            {"adv_name": "Kyiv_Troieshchyna_R2", "public_key": "1500"},
            {"adv_name": "Kyiv_Voskresenka_R1", "public_key": "f300"},
            {"adv_name": "Lviv_Center_R1", "public_key": "ab00"},
        ]

    def test_empty_keeps_all(self):
        assert len(_select_tail_candidates(self._repeaters(), "")) == 4

    def test_by_name_prefix(self):
        result = _select_tail_candidates(self._repeaters(), "Kyiv_Troieshchyna")
        assert [r["adv_name"] for r in result] == [
            "Kyiv_Troieshchyna_R1", "Kyiv_Troieshchyna_R2",
        ]

    def test_by_exact_name(self):
        result = _select_tail_candidates(self._repeaters(), "Kyiv_Voskresenka_R1")
        assert [r["adv_name"] for r in result] == ["Kyiv_Voskresenka_R1"]

    def test_by_hex_hash(self):
        result = _select_tail_candidates(self._repeaters(), "f3")
        assert [r["adv_name"] for r in result] == ["Kyiv_Voskresenka_R1"]

    def test_mixed_tokens(self):
        result = _select_tail_candidates(self._repeaters(), "Kyiv_Troieshchyna,f3")
        assert [r["adv_name"] for r in result] == [
            "Kyiv_Troieshchyna_R1", "Kyiv_Troieshchyna_R2", "Kyiv_Voskresenka_R1",
        ]

    def test_dedups_overlapping_tokens(self):
        result = _select_tail_candidates(self._repeaters(), "Kyiv_Tro,14")
        assert [r["adv_name"] for r in result] == [
            "Kyiv_Troieshchyna_R1", "Kyiv_Troieshchyna_R2",
        ]

    def test_no_match_returns_empty(self):
        assert _select_tail_candidates(self._repeaters(), "Odesa") == []


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
        assert cfg["max_path_hops"] == DEFAULTS["max_path_hops"]

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
