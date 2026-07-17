# -*- coding: utf-8 -*-
"""Tests for TdxChronosFetcher.

The tests are split into two layers:

1. Offline / unit tests (no tdx-chronos install required):
   - code translation (600519 → sh600519, 510050 → sh510050, ...)
   - market / scope detection (rejects HK / US / convertible bond / index)
   - _normalize_data column mapping (vol → volume, date coercion, pct_chg compute)
   - lazy init defensive behaviour when data_dir is missing / tdx_chronos not installed
   - manager-level: registers with priority 0 when data is available,
     silently skipped when not (mirrors TickFlow/Tushare pattern)

2. Opt-in integration tests gated by env var TDX_CHRONOS_DATA_DIR pointing
   to a populated data directory; CI does not set this so they skip by default.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


from data_provider import tdx_chronos_fetcher as _tcf_module  # noqa: E402
from data_provider.base import DataFetchError, STANDARD_COLUMNS  # noqa: E402
from data_provider.tdx_chronos_fetcher import (  # noqa: E402
    DEFAULT_DATA_DIR_CANDIDATES,
    TdxChronosFetcher,
    _coerce_date_series,
)


def _make_raw_df(date_values, market_value="sh"):
    """Build a fake tdx-chronos kline DataFrame."""
    return pd.DataFrame(
        {
            "date": date_values,
            "open": [10.0] * len(date_values),
            "high": [11.0] * len(date_values),
            "low": [9.0] * len(date_values),
            "close": [10.5, 10.6, 10.4, 10.8, 10.9][: len(date_values)]
            if len(date_values) > 0
            else [],
            "amount": [100000.0] * len(date_values),
            "vol": [1000, 1100, 900, 1200, 1300][: len(date_values)]
            if len(date_values) > 0
            else [],
            "reserved": [0] * len(date_values),
            "market": [market_value] * len(date_values),
            "source_zip": ["hsjday.zip"] * len(date_values),
            "ingested_at": ["2026-01-01T00:00:00+00:00"] * len(date_values),
        }
    )


class TestTdxChronosCodeTranslation(unittest.TestCase):
    def test_a_share_stock_translation(self):
        self.assertEqual(TdxChronosFetcher._to_chronos_symbol("600519"), "sh600519")
        self.assertEqual(TdxChronosFetcher._to_chronos_symbol("000001"), "sz000001")
        self.assertEqual(TdxChronosFetcher._to_chronos_symbol("301001"), "sz301001")
        self.assertEqual(TdxChronosFetcher._to_chronos_symbol("002594"), "sz002594")
        self.assertEqual(TdxChronosFetcher._to_chronos_symbol("688001"), "sh688001")

    def test_bse_translation(self):
        self.assertEqual(TdxChronosFetcher._to_chronos_symbol("920001"), "bj920001")
        self.assertEqual(TdxChronosFetcher._to_chronos_symbol("430123"), "bj430123")
        self.assertEqual(TdxChronosFetcher._to_chronos_symbol("831001"), "bj831001")

    def test_etf_translation(self):
        self.assertEqual(TdxChronosFetcher._to_chronos_symbol("510050"), "sh510050")
        self.assertEqual(TdxChronosFetcher._to_chronos_symbol("512760"), "sh512760")
        self.assertEqual(TdxChronosFetcher._to_chronos_symbol("588000"), "sh588000")
        self.assertEqual(TdxChronosFetcher._to_chronos_symbol("159915"), "sz159915")
        self.assertEqual(TdxChronosFetcher._to_chronos_symbol("160216"), "sz160216")

    def test_convertible_bond_rejected(self):
        # sh110xxx / sz12xxxx are convertible bonds, not stock / ETF.
        with self.assertRaises(DataFetchError):
            TdxChronosFetcher._to_chronos_symbol("110001")
        with self.assertRaises(DataFetchError):
            TdxChronosFetcher._to_chronos_symbol("113001")
        with self.assertRaises(DataFetchError):
            TdxChronosFetcher._to_chronos_symbol("128001")

    def test_index_rejected(self):
        # Index codes that look like 6-digit numbers but aren't stock/ETF.
        # sz399xxx (深证指数) is NOT covered by this fetcher.
        with self.assertRaises(DataFetchError):
            TdxChronosFetcher._to_chronos_symbol("399001")
        with self.assertRaises(DataFetchError):
            TdxChronosFetcher._to_chronos_symbol("399006")
        # Note: sh000001 is a special case where the same code is also a
        # SZ stock — our prefix-collision resolution maps 000xxx → sz,
        # so this is treated as SZ stock. We document that here.
        self.assertEqual(TdxChronosFetcher._to_chronos_symbol("000001"), "sz000001")

    def test_hk_us_jp_kr_tw_rejected(self):
        for code in ("hk00700", "AAPL", "TSLA", "7203.T", "005930.KS", "2330.TW"):
            with self.assertRaises(DataFetchError):
                TdxChronosFetcher._to_chronos_symbol(code)

    def test_invalid_format_rejected(self):
        for code in ("12345", "1234567", "abc123", ""):
            with self.assertRaises(DataFetchError):
                TdxChronosFetcher._to_chronos_symbol(code)

    def test_is_in_scope_matches_translation(self):
        # All translatable codes are in-scope.
        for code in ("600519", "000001", "301001", "688001", "510050",
                     "159915", "920001", "430123"):
            self.assertTrue(
                TdxChronosFetcher._is_in_scope(code),
                f"{code} should be in scope",
            )
        # Out-of-scope codes.
        for code in ("110001", "123456", "399001", "hk00700", "AAPL",
                     "7203.T", "abc", "12345"):
            self.assertFalse(
                TdxChronosFetcher._is_in_scope(code),
                f"{code} should NOT be in scope",
            )


class TestTdxChronosDateCoercion(unittest.TestCase):
    def test_int_yyyymmdd(self):
        s = pd.Series([20241225, 20241226, 20241227])
        out = _coerce_date_series(s)
        self.assertEqual(out.tolist(), ["2024-12-25", "2024-12-26", "2024-12-27"])

    def test_string_yyyymmdd(self):
        s = pd.Series(["20241225", "20241226"])
        out = _coerce_date_series(s)
        self.assertEqual(out.tolist(), ["2024-12-25", "2024-12-26"])

    def test_string_dashed(self):
        s = pd.Series(["2024-12-25", "2024-12-26"])
        out = _coerce_date_series(s)
        self.assertEqual(out.tolist(), ["2024-12-25", "2024-12-26"])

    def test_datetime64(self):
        s = pd.to_datetime(["2024-12-25", "2024-12-26"])
        out = _coerce_date_series(s)
        self.assertEqual(out.tolist(), ["2024-12-25", "2024-12-26"])

    def test_datetime_index(self):
        # Sometimes pd.to_datetime on a list returns DatetimeIndex not Series.
        idx = pd.to_datetime(["2024-12-25", "2024-12-26"])
        s = pd.Series(idx)
        out = _coerce_date_series(s)
        self.assertEqual(out.tolist(), ["2024-12-25", "2024-12-26"])


class TestTdxChronosNormalize(unittest.TestCase):
    def test_vol_to_volume_and_pct_chg(self):
        raw = _make_raw_df([20241225, 20241226, 20241227])
        norm = TdxChronosFetcher()._normalize_data(raw, "600519")
        self.assertIn("volume", norm.columns)
        self.assertNotIn("vol", norm.columns)
        self.assertIn("pct_chg", norm.columns)
        self.assertEqual(norm["code"].iloc[0], "600519")
        # First row pct_chg should be 0 (no prior close)
        self.assertEqual(norm["pct_chg"].iloc[0], 0.0)
        # Standard column contract
        for col in STANDARD_COLUMNS:
            self.assertIn(col, norm.columns)
        self.assertIn("code", norm.columns)

    def test_drops_metadata_columns(self):
        raw = _make_raw_df([20241225])
        norm = TdxChronosFetcher()._normalize_data(raw, "510050")
        for col in ("reserved", "market", "source_zip", "ingested_at"):
            self.assertNotIn(col, norm.columns)

    def test_empty_input_returns_empty_standard_frame(self):
        f = TdxChronosFetcher()
        norm = f._normalize_data(pd.DataFrame(), "600519")
        self.assertEqual(list(norm.columns), ["code"] + STANDARD_COLUMNS)
        self.assertEqual(len(norm), 0)

    def test_pct_chg_existing_passthrough(self):
        raw = _make_raw_df([20241225, 20241226])
        raw["pct_chg"] = [1.5, -2.3]
        norm = TdxChronosFetcher()._normalize_data(raw, "600519")
        self.assertEqual(norm["pct_chg"].iloc[0], 1.5)
        self.assertEqual(norm["pct_chg"].iloc[1], -2.3)

    def test_missing_volume_filled_zero(self):
        raw = _make_raw_df([20241225])
        raw = raw.drop(columns=["vol"])
        norm = TdxChronosFetcher()._normalize_data(raw, "600519")
        self.assertEqual(norm["volume"].iloc[0], 0)


class TestTdxChronosLazyInit(unittest.TestCase):
    def test_gracefully_handles_missing_data_dir(self):
        """When neither env nor default candidate has a valid data dir,
        is_available() must return False and fetch must raise DataFetchError.
        """
        empty_candidates_patch = patch.object(
            _tcf_module, "DEFAULT_DATA_DIR_CANDIDATES", ()
        )
        with patch.dict(os.environ, {"TDX_CHRONOS_DATA_DIR": ""}, clear=False), \
                empty_candidates_patch:
            f = TdxChronosFetcher()
            self.assertFalse(f.is_available())
            with self.assertRaises(DataFetchError):
                f._fetch_raw_data("600519", "2024-12-25", "2024-12-31")

    def test_gracefully_handles_import_error(self):
        """If tdx_chronos cannot be imported, the fetcher must self-disable
        and raise DataFetchError rather than crash the manager."""
        empty_candidates_patch = patch.object(
            _tcf_module, "DEFAULT_DATA_DIR_CANDIDATES", ()
        )
        with patch.dict(sys.modules, {"tdx_chronos": None}), empty_candidates_patch:
            f = TdxChronosFetcher()
            f._client_init_failed = False
            f._client_init_error = None
            f._resolved_data_dir = None
            with self.assertRaises(DataFetchError):
                f._fetch_raw_data("600519", "2024-12-25", "2024-12-31")

    def test_out_of_scope_does_not_init_client(self):
        """HK / US / convertible bond must raise BEFORE touching the client."""
        f = TdxChronosFetcher()
        with patch.object(f, "_ensure_client") as mock_client:
            with self.assertRaises(DataFetchError):
                f._fetch_raw_data("hk00700", "2024-12-25", "2024-12-31")
            with self.assertRaises(DataFetchError):
                f._fetch_raw_data("110001", "2024-12-25", "2024-12-31")
            mock_client.assert_not_called()

    def test_init_failure_is_cached(self):
        """After one init failure, _ensure_client must not retry repeatedly."""
        empty_candidates_patch = patch.object(
            _tcf_module, "DEFAULT_DATA_DIR_CANDIDATES", ()
        )
        with patch.dict(os.environ, {"TDX_CHRONOS_DATA_DIR": ""}), \
                empty_candidates_patch:
            f = TdxChronosFetcher()
            with self.assertRaises(DataFetchError):
                f._ensure_client()
            self.assertTrue(f._client_init_failed)
            with self.assertRaises(DataFetchError):
                f._ensure_client()  # should raise immediately, not retry I/O

    def test_priority_overridable_via_ctor(self):
        f = TdxChronosFetcher(priority=5)
        self.assertEqual(f.priority, 5)
        f2 = TdxChronosFetcher(priority="not-an-int")
        self.assertEqual(f2.priority, 0)  # invalid -> default

    def test_default_candidates_listed(self):
        self.assertIn("/app/tdx-chronos/data", DEFAULT_DATA_DIR_CANDIDATES)


class TestTdxChronosManagerRegistration(unittest.TestCase):
    """Manager-level: TdxChronosFetcher slots in at the top when data is
    available, and is silently skipped otherwise."""

    def test_added_to_default_fetcher_market_support(self):
        from data_provider.base import DataFetcherManager

        supported = DataFetcherManager._DAILY_MARKET_FETCHER_SUPPORT.get(
            "TdxChronosFetcher"
        )
        self.assertEqual(supported, {"cn"})

    def test_manager_registers_when_data_dir_available(self):
        """If TDX_CHRONOS_DATA_DIR points at a real data dir, manager should
        insert TdxChronosFetcher at priority 0 at the top."""
        data_dir = os.environ.get("TDX_CHRONOS_DATA_DIR")
        if not (data_dir and os.path.isdir(os.path.join(data_dir, "meta"))):
            self.skipTest("TDX_CHRONOS_DATA_DIR not set; skipping real integration")

        from data_provider.base import DataFetcherManager

        mgr = DataFetcherManager()
        names = [f.name for f in mgr._get_fetchers_snapshot()]
        self.assertIn("TdxChronosFetcher", names)
        # First in priority order (lowest priority number)
        tdx = next(f for f in mgr._get_fetchers_snapshot() if f.name == "TdxChronosFetcher")
        self.assertEqual(tdx.priority, 0)
        # All other fetchers have priority >= tdx's
        for f in mgr._get_fetchers_snapshot():
            if f.name != "TdxChronosFetcher":
                self.assertGreaterEqual(f.priority, tdx.priority)

    def test_manager_skips_when_data_dir_unavailable(self):
        """If neither the env var nor any candidate points at a real
        tdx-chronos data dir, the manager should skip silently."""
        empty_candidates_patch = patch.object(
            _tcf_module, "DEFAULT_DATA_DIR_CANDIDATES", ()
        )
        # Drop the env var explicitly so a parent shell's
        # TDX_CHRONOS_DATA_DIR does not leak into this test, and reset the
        # Config singleton so its cached tdx_chronos_data_dir doesn't leak.
        env_purge = {k: v for k, v in os.environ.items() if k != "TDX_CHRONOS_DATA_DIR"}
        with patch.dict(os.environ, env_purge, clear=True), empty_candidates_patch:
            from data_provider.base import DataFetcherManager
            from src.config import Config

            Config._instance = None  # noqa: SLF001 — test isolation
            self.assertIsNone(os.environ.get("TDX_CHRONOS_DATA_DIR"))
            self.assertEqual(_tcf_module.DEFAULT_DATA_DIR_CANDIDATES, ())
            mgr = DataFetcherManager()
            names = [f.name for f in mgr._get_fetchers_snapshot()]
            self.assertNotIn("TdxChronosFetcher", names)

    def test_manager_skips_when_tdx_chronos_not_installed(self):
        """If tdx_chronos cannot be imported, manager should skip silently."""
        with patch.dict(sys.modules, {"tdx_chronos": None}):
            # Also reset module-level cache so the new import attempt fails.
            from data_provider.base import DataFetcherManager

            mgr = DataFetcherManager()
            names = [f.name for f in mgr._get_fetchers_snapshot()]
            self.assertNotIn("TdxChronosFetcher", names)


class TestTdxChronosEndToEnd(unittest.TestCase):
    """Opt-in integration test against a real tdx-chronos data dir.

    Run with: TDX_CHRONOS_DATA_DIR=/app/tdx-chronos/data python3 -m pytest \
        tests/test_tdx_chronos_fetcher.py::TestTdxChronosEndToEnd -v
    """

    @classmethod
    def setUpClass(cls):
        data_dir = os.environ.get("TDX_CHRONOS_DATA_DIR", "").strip()
        if not data_dir:
            raise unittest.SkipTest("TDX_CHRONOS_DATA_DIR not set; skipping e2e")
        if not os.path.isfile(os.path.join(data_dir, "meta", "meta.db")):
            raise unittest.SkipTest(
                f"TDX_CHRONOS_DATA_DIR={data_dir} is missing meta/meta.db"
            )
        cls.fetcher = TdxChronosFetcher(data_dir=data_dir)
        if not cls.fetcher.is_available():
            raise unittest.SkipTest("tdx_chronos client could not initialise")

    def test_a_share_stock_kline(self):
        df = self.fetcher._fetch_raw_data("600519", "2024-12-25", "2024-12-31")
        self.assertGreater(len(df), 0)
        norm = self.fetcher._normalize_data(df, "600519")
        self.assertEqual(norm["code"].iloc[0], "600519")
        for col in STANDARD_COLUMNS:
            self.assertIn(col, norm.columns)

    def test_etf_kline(self):
        df = self.fetcher._fetch_raw_data("510050", "2024-12-25", "2024-12-31")
        self.assertGreater(len(df), 0)
        norm = self.fetcher._normalize_data(df, "510050")
        self.assertEqual(norm["code"].iloc[0], "510050")

    def test_bse_kline(self):
        df = self.fetcher._fetch_raw_data("920001", "2024-12-25", "2024-12-31")
        if len(df) == 0:
            self.skipTest("BSE 920001 has no data in 2024-12 range")
        norm = self.fetcher._normalize_data(df, "920001")
        self.assertEqual(norm["code"].iloc[0], "920001")

    def test_convertible_bond_rejected_against_real_client(self):
        with self.assertRaises(DataFetchError):
            self.fetcher._fetch_raw_data("110001", "2024-12-25", "2024-12-31")

    def test_unknown_symbol_raises(self):
        # 999999 is not a real symbol in tdx-chronos
        with self.assertRaises(DataFetchError):
            self.fetcher._fetch_raw_data("999999", "2024-12-25", "2024-12-31")


if __name__ == "__main__":
    unittest.main()