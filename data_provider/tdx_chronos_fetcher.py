# -*- coding: utf-8 -*-
"""
===================================
TdxChronosFetcher - tdx-chronos 离线数据仓库 (Priority 0)
===================================

数据来源：本地 tdx-chronos Parquet 数据仓库（基于通达信 .day / .dat 整理）。
特点：完全离线、零网络、零限流；覆盖 A 股 + 场内 ETF / LOF / REITs / 可转债 + 主要指数。

启用条件：
1. 安装 tdx-chronos（pip install / editable）
2. 配置 TDX_CHRONOS_DATA_DIR 指向包含 parquet_compact/ + fin/parsed/ + gp/ +
   index/ + meta/meta.db 的目录；未配置时按以下顺序自动探测：
     - $TDX_CHRONOS_DATA_DIR
     - /app/tdx-chronos/data
     - ./tdx-chronos/data（相对工作目录）
3. 探测到且能成功初始化 TdxChronos 时插入到 cn 优先级 0；否则跳过。

市场支持范围：
- 仅做 A 股 stock + ETF 优先（与本任务范围一致）：
    - sh6/sh0/sh3/sh68 + sh51/sh52/sh56/sh58 + sz0/sz30/sz002 + sz15/sz16/sz18
    - bj4/bj8/bj92 + bj43/bj83/bj87/bj88
- 可转债（sh11/sh12/sz12）、REITs（sz18 中除 ETF 段）、指数（sh000xxx/sz399xxx）
  不在本 fetcher 范围内，会主动抛 DataFetchError 让上层 fallback 到原优先级链，
  与用户确认的范围保持一致。

代码格式转换：
- 项目内调用约定（裸 6 位，例如 600519 / 510050）→ tdx-chronos（带前缀 sh600519）。
- _to_chronos_symbol() 复用 ETF_PREFIXES + 标准 A 股代码段；HK/US/JP/KR/TW 与
  非 6 位数字代码直接抛 DataFetchError。

关键策略：
- 懒加载 TdxChronos client（首次 _fetch_raw_data 才 import + open db），避免在
  DataFetcherManager 构造阶段就引入 pyarrow/pandas 版本耦合；任何 ImportError /
  FileNotFoundError 都会让 _ensure_client() 抛 DataFetchError，被 manager 视为
  不可用而熔断。
- 离线读写 / 不需要 user-agent / 不需要限流；连接失败（data_dir 缺失 / 不完整）
  即视为整体不可用，不做重试 —— 与 PytdxFetcher / EfinanceFetcher 的网络重试
  语义不同。
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import pandas as pd

from .base import (
    STANDARD_COLUMNS,
    BaseFetcher,
    DataFetchError,
    normalize_stock_code,
)

logger = logging.getLogger(__name__)


DEFAULT_DATA_DIR_CANDIDATES = (
    "/app/tdx-chronos/data",
    "./tdx-chronos/data",
)

_A_SH_SH_PREFIXES = ("60", "68", "51", "52", "56", "58")
_A_SH_OTHER_PREFIXES = ("11", "12")  # 沪市可转债（不在本 fetcher 范围）
_A_SZ_STOCK_PREFIXES = ("00", "30", "002", "301")
_A_SZ_ETF_PREFIXES = ("15", "16", "18")
_A_SZ_OTHER_PREFIXES = ("12",)  # 深市可转债
_BSE_PREFIXES = ("92", "43", "81", "82", "83", "87", "88")


def _coerce_date_series(series: pd.Series) -> pd.Series:
    """统一把 tdx-chronos 的 date 字段归一为 'YYYY-MM-DD' 字符串。"""
    if series.dtype.kind in ("i", "u"):  # int / uint
        ints = series.astype("Int64").astype("string").str.zfill(8)
        parsed = pd.to_datetime(ints, format="%Y%m%d", errors="coerce")
        return pd.Series(parsed).dt.strftime("%Y-%m-%d")
    if series.dtype.kind == "M":  # datetime64
        return pd.Series(pd.to_datetime(series)).dt.strftime("%Y-%m-%d")
    as_str = series.astype(str).str.strip()
    has_dash = as_str.str.contains("-", regex=False)
    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    if has_dash.any():
        parsed.loc[has_dash] = pd.to_datetime(
            as_str.loc[has_dash], format="%Y-%m-%d", errors="coerce"
        )
    plain = (~has_dash) & as_str.str.fullmatch(r"\d{8}")
    if plain.any():
        parsed.loc[plain] = pd.to_datetime(
            as_str.loc[plain], format="%Y%m%d", errors="coerce"
        )
    fallback = ~has_dash & ~plain
    if fallback.any():
        parsed.loc[fallback] = pd.to_datetime(as_str.loc[fallback], errors="coerce")
    return parsed.dt.strftime("%Y-%m-%d")


class TdxChronosFetcher(BaseFetcher):
    """
    tdx-chronos 离线数据源 —— 仅为 A 股 stock + ETF 提供优先级 0 的 K 线拉取。

    入口：
        TdxChronosFetcher(data_dir=None, *, priority=None)
        - data_dir：覆盖 TDX_CHRONOS_DATA_DIR；为 None 时按 env 或内置候选路径探测。
        - priority：覆盖 TDX_CHRONOS_PRIORITY（默认 0）。

    不支持的代码类型（HK / US / JP / KR / TW / 可转债 / REITs 非 ETF 段 / 指数等）
    会在 _fetch_raw_data 抛 DataFetchError，由 DataFetcherManager 透明降级。
    """

    name = "TdxChronosFetcher"
    priority = int(os.getenv("TDX_CHRONOS_PRIORITY", "0"))

    def __init__(
        self,
        data_dir: Optional[str] = None,
        *,
        priority: Optional[int] = None,
    ):
        if priority is not None:
            try:
                self.priority = int(priority)
            except (TypeError, ValueError):
                logger.warning(
                    "[TdxChronosFetcher] 无效 priority=%r，使用默认 0", priority
                )
        # data_dir 解析顺序：显式 ctor 参数 > 环境变量 > 内置候选路径自动探测。
        # 显式传入空字符串视同 None（避免上游 cached config 传入 "" 时无法 fallback）。
        explicit = data_dir.strip() if isinstance(data_dir, str) else data_dir
        env_value = os.getenv("TDX_CHRONOS_DATA_DIR", "").strip() or None
        self._configured_data_dir = explicit or env_value
        self._client = None
        self._client_init_failed = False
        self._client_init_error: Optional[str] = None
        self._resolved_data_dir: Optional[str] = None
        self._available_etf_symbols: Optional[set] = None
        self._available_stock_symbols: Optional[set] = None

    @property
    def resolved_data_dir(self) -> Optional[str]:
        return self._resolved_data_dir

    def is_available(self) -> bool:
        """探测当前环境是否能成功初始化 tdx-chronos client。"""
        try:
            self._ensure_client()
            return self._client is not None
        except DataFetchError:
            return False

    def _resolve_data_dir(self) -> Optional[str]:
        if self._resolved_data_dir:
            return self._resolved_data_dir
        candidates = []
        if self._configured_data_dir:
            candidates.append(self._configured_data_dir)
        for c in DEFAULT_DATA_DIR_CANDIDATES:
            if c and c not in candidates:
                candidates.append(c)
        for cand in candidates:
            try:
                meta_db = os.path.join(cand, "meta", "meta.db")
                parquet_root = os.path.join(cand, "parquet_compact")
                if os.path.isfile(meta_db) and os.path.isdir(parquet_root):
                    self._resolved_data_dir = cand
                    logger.info(
                        "[TdxChronosFetcher] 自动探测到数据目录: %s", cand
                    )
                    return cand
            except OSError:
                continue
        return None

    def _ensure_client(self):
        """懒加载 TdxChronos；失败后缓存失败状态，避免反复探测。"""
        if self._client is not None:
            return self._client
        if self._client_init_failed:
            raise DataFetchError(
                f"TdxChronosFetcher 初始化已失败（{self._client_init_error}），跳过"
            )
        data_dir = self._resolve_data_dir()
        if not data_dir:
            self._client_init_failed = True
            self._client_init_error = "未找到可用的 TDX_CHRONOS_DATA_DIR"
            logger.debug(
                "[TdxChronosFetcher] 未找到可用的 tdx-chronos 数据目录，"
                "跳过本数据源（默认候选: %s）",
                ", ".join(DEFAULT_DATA_DIR_CANDIDATES),
            )
            raise DataFetchError(self._client_init_error)
        try:
            from tdx_chronos import TdxChronos  # type: ignore
        except Exception as exc:
            self._client_init_failed = True
            self._client_init_error = f"导入 tdx_chronos 失败: {exc}"
            logger.debug(
                "[TdxChronosFetcher] 导入 tdx_chronos 失败，跳过本数据源: %s", exc
            )
            raise DataFetchError(self._client_init_error) from exc
        try:
            client = TdxChronos(data_dir, readonly=True)
        except FileNotFoundError as exc:
            self._client_init_failed = True
            self._client_init_error = str(exc)
            logger.debug(
                "[TdxChronosFetcher] TdxChronos 初始化失败（data_dir=%s）: %s",
                data_dir,
                exc,
            )
            raise DataFetchError(f"TdxChronos 初始化失败: {exc}") from exc
        except Exception as exc:
            self._client_init_failed = True
            self._client_init_error = f"TdxChronos 初始化异常: {exc}"
            logger.warning(
                "[TdxChronosFetcher] TdxChronos 初始化异常（data_dir=%s）: %s",
                data_dir,
                exc,
            )
            raise DataFetchError(self._client_init_error) from exc
        self._client = client
        return self._client

    @staticmethod
    def _to_chronos_symbol(stock_code: str) -> str:
        """项目内裸 6 位代码 → tdx-chronos 带前缀代码。

        仅识别本 fetcher 范围内的 A 股 stock + ETF；其他代码段由调用方先过滤。
        """
        code = normalize_stock_code(stock_code)
        if not code.isdigit() or len(code) != 6:
            raise DataFetchError(
                f"TdxChronosFetcher 不支持非 6 位数字代码 {stock_code}"
            )

        # A 股 ETF（包含沪深 ETF / LOF / REITs / 货币 ETF）
        if code.startswith(_A_SH_ETF_PREFIXES := ("51", "52", "56", "58")):
            return f"sh{code}"
        if code.startswith(_A_SZ_ETF_PREFIXES := ("15", "16", "18")):
            return f"sz{code}"

        # A 股普通股 / 创业板 / 科创板
        if code.startswith(("60", "68")):
            return f"sh{code}"
        if code.startswith(("00", "30", "002", "301")):
            return f"sz{code}"

        # 北交所
        if code.startswith(_BSE_PREFIXES):
            return f"bj{code}"

        raise DataFetchError(
            f"TdxChronosFetcher 不支持代码 {stock_code}（不在 A 股 stock / ETF 段内）"
        )

    @staticmethod
    def _is_in_scope(stock_code: str) -> bool:
        """仅做范围识别，不抛异常（用于在 _fetch_raw_data 入口快速分流）。"""
        try:
            code = normalize_stock_code(stock_code)
        except Exception:
            return False
        if not code.isdigit() or len(code) != 6:
            return False
        if code.startswith(("51", "52", "56", "58", "15", "16", "18")):
            return True  # ETF / LOF / 场内基金（含 REITs 的 sz18 段也走这里）
        if code.startswith(("60", "68", "00", "30", "002", "301")):
            return True
        if code.startswith(_BSE_PREFIXES):
            return True
        return False

    def _fetch_raw_data(
        self, stock_code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        if not self._is_in_scope(stock_code):
            raise DataFetchError(
                f"TdxChronosFetcher 不支持代码 {stock_code}（非 A 股 stock / ETF 段）"
            )
        client = self._ensure_client()
        chronos_symbol = self._to_chronos_symbol(stock_code)
        try:
            df = client.kline(chronos_symbol, start=start_date, end=end_date)
        except Exception as exc:
            raise DataFetchError(
                f"TdxChronos kline 查询失败 {chronos_symbol}: {exc}"
            ) from exc
        if df is None or df.empty:
            raise DataFetchError(
                f"TdxChronosFetcher 未找到 {chronos_symbol} 的 K 线数据 "
                f"（{start_date} ~ {end_date}）"
            )
        return df

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """tdx-chronos → 项目内标准列。

        tdx-chronos 列：date, open, high, low, close, amount, vol, reserved,
        market, source_zip, ingested_at
        标准列：date, open, high, low, close, volume, amount, pct_chg

        date 字段兼容三种来源格式：
        - 'YYYY-MM-DD' 字符串
        - 'YYYYMMDD' 字符串
        - int64 YYYYMMDD（pyarrow 默认读取 parquet 的字符串列在某些版本会变 int）
        """
        if df is None or df.empty:
            return pd.DataFrame(columns=["code"] + STANDARD_COLUMNS)

        df = df.copy()

        if "date" in df.columns:
            df["date"] = _coerce_date_series(df["date"])
        rename_map = {"vol": "volume"}
        df = df.rename(columns=rename_map)

        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col not in df.columns:
                df[col] = 0
        if "pct_chg" not in df.columns and "close" in df.columns:
            df["pct_chg"] = df["close"].pct_change() * 100
            df["pct_chg"] = df["pct_chg"].fillna(0).round(2)
        elif "pct_chg" in df.columns:
            df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce").fillna(0)

        df["code"] = normalize_stock_code(stock_code)

        keep_cols = ["code"] + STANDARD_COLUMNS
        existing_cols = [c for c in keep_cols if c in df.columns]
        df = df[existing_cols]

        try:
            df = df.sort_values("date").reset_index(drop=True)
        except Exception:
            pass
        return df