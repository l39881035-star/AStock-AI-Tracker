# -*- coding: utf-8 -*-
"""A股交易日判定 —— 只读本目录下的交易日历 JSON。

设计原则：
1. 用深交所官方月历生成的数据（trading_days.json），不靠「周末」猜。
2. 日历覆盖范围外一律返回 None（未知），绝不猜测 —— 宁可不发，不发明知可能错的。
3. 只做判定，不做网络请求。

⚠️ 维护：calendar/trading_days.json 需在每年 11-12 月国务院公布次年安排后更新。
   文件里带 coverage 字段，超出范围时本模块返回 None。
"""
import datetime
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_CAL_PATHS = [
    os.path.join(_HERE, "trading_days.json"),
    os.path.join(_HERE, "calendar", "trading_days.json"),
]

_DATA = None
_COVERAGE = None
_LOAD_ERR = None


def _load():
    global _DATA, _COVERAGE, _LOAD_ERR
    if _DATA is not None or _LOAD_ERR is not None:
        return
    for p in _CAL_PATHS:
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
            _DATA = raw["days"]
            _COVERAGE = raw.get("coverage", {})
            return
        except Exception as e:
            _LOAD_ERR = "%s: %s" % (p, e)
            return
    _LOAD_ERR = "交易日历文件不存在（找过: %s）" % ", ".join(_CAL_PATHS)


def is_trading_day(d):
    """d: datetime.date 或 'YYYY-MM-DD'。返回 True/False；日历外返回 None（未知）。"""
    _load()
    if _DATA is None:
        return None
    if isinstance(d, datetime.datetime):
        d = d.date()
    if isinstance(d, str):
        key = d
    else:
        key = d.strftime("%Y-%m-%d")
    rec = _DATA.get(key)
    if rec is None:
        return None
    return bool(rec.get("open"))


def calendar_ok():
    """日历是否加载成功。"""
    _load()
    return _DATA is not None


def calendar_error():
    _load()
    return _LOAD_ERR


def coverage():
    _load()
    return _COVERAGE


def last_closed_trading_day(now=None):
    """返回「最近一个已收盘的交易日」YYYY-MM-DD。

    规则：
      - 15:00 之前：回退到前一交易日（当天尚未收盘）
      - 15:00 及以后：当天若为交易日则返回当天，否则回退
      - 回退时逐日往前找 open=True 的日期
      - 向前最多找 30 天；仍找不到返回 None（绝不猜）

    ⚠️ 与旧实现的关键差异：旧版只看 weekday()，遇节假日必错
       （例：2026-09-25 中秋休市，旧版会把 9-25 当成交易日）。
    """
    _load()
    if _DATA is None:
        return None
    if now is None:
        now = datetime.datetime.now()
    if isinstance(now, datetime.date) and not isinstance(now, datetime.datetime):
        now = datetime.datetime.combine(now, datetime.time(16, 0))

    d = now.date()
    if now.hour < 15:
        d = d - datetime.timedelta(days=1)

    for _ in range(30):
        if is_trading_day(d) is True:
            return d.strftime("%Y-%m-%d")
        d = d - datetime.timedelta(days=1)
    return None


if __name__ == "__main__":
    print("日历加载:", "OK" if calendar_ok() else "FAIL(%s)" % calendar_error())
    print("覆盖范围:", coverage())
    print()
    print("今日:", datetime.date.today())
    print("今天是否交易日:", is_trading_day(datetime.date.today()))
    print("最近已收盘交易日:", last_closed_trading_day())
    print()
    for s in ["2026-09-24", "2026-09-25", "2026-09-27", "2026-09-28",
              "2026-10-01", "2026-10-05", "2026-10-08"]:
        print("  %s -> 交易日=%s" % (s, is_trading_day(s)))
    print()
    fake = datetime.datetime(2026, 9, 27, 16, 0)
    print("模拟 2026-09-27 16:00 -> 最近已收盘交易日 =",
          last_closed_trading_day(fake), "(应为 2026-09-24)")
