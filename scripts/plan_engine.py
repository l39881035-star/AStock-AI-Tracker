# -*- coding: utf-8 -*-
"""If-Then 条件引擎 —— 条件生成 与 条件求值（早盘/盘后两轨共用）

═══════════════════════════════════════════════════════════════════════════
为什么单独成一个模块
═══════════════════════════════════════════════════════════════════════════
早盘「生成条件」和盘后「判定条件是否命中」**必须是同一套语义**。
若两处各写一份，迟早漂移成「早上说的条件 ≠ 晚上判的标准」——
那样的「对账」是假的，比不做对账更坏（会给人虚假的纪律感）。
两轨共用本模块，是「对账成立」的前提条件。

═══════════════════════════════════════════════════════════════════════════
红线
═══════════════════════════════════════════════════════════════════════════
R2 纯标准库（NAS 生产解释器 = /usr/bin/python3 = 3.11.2）
R3 算力隔离：本模块只做数值比较与排序，**不产出任何文学化措辞**。
             「上/下」「突破/失守」「命中/未命中」全部由代码判定。
             调用方只负责把结论词拼成人话，不得再改判。
R5 缺失不填：`level` 拿不到的条件【直接不生成】——
             宁可不给条件，也绝不生成一个假价位（假价位会导致假对账）。

═══════════════════════════════════════════════════════════════════════════
条件的数据结构（写进 plan_<date>.json 的就是它）
═══════════════════════════════════════════════════════════════════════════
    {
      "id":      "U1",         # 稳定标识，便于盘后逐条对应
      "side":    "up" | "down",
      "trigger": "touch" | "close",
      "level":   51.95,        # 元（两位小数）
      "ref":     "MA20",       # 该价位的来源标签，取自数据块
    }

求值语义（严格定义，无歧义）：
    side=up   trigger=touch → 命中 ⟺ 当日最高价 ≥ level   （盘中触及过）
    side=up   trigger=close → 命中 ⟺ 当日收盘价 ≥ level   （收在其上）
    side=down trigger=touch → 命中 ⟺ 当日最低价 ≤ level   （盘中跌破过）
    side=down trigger=close → 命中 ⟺ 当日收盘价 ≤ level   （收在其下）
"""

import json
import os

# 均线字段（顺序即展示顺序）
_MA_KEYS = [
    ("MA5", "ma5"), ("MA10", "ma10"), ("MA20", "ma20"), ("MA30", "ma30"),
    ("MA60", "ma60"), ("MA120", "ma120"), ("MA250", "ma250"),
]


def _num(v):
    """取一个「可用作价位」的数：None / 非数 / ≤0 一律视为不可用（R5）"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    return None


def price_candidates(data):
    """把数据块里的价位候选分成「压力（高于现价）」与「支撑（低于现价）」。

    ★ R3：排序由程序完成，调用方不得自行比较。
    返回 (ups, downs)
        ups   —— 由近到远（升序），元素 = (名称, 价位)
        downs —— 由近到远（降序），元素 = (名称, 价位)
    """
    price = _num(data.get("price"))
    ups, downs = [], []
    if price is None:
        return ups, downs

    for nm, key in _MA_KEYS:
        v = _num(data.get(key))
        if v is None:
            continue
        (ups if v > price else downs).append((nm, v))

    # 并入「前20日压力/支撑」（不含当日）—— 与均线同池排序，避免两套参照打架
    r = _num(data.get("resistance"))
    if r is not None and r > price:
        ups.append(("前20日压力", r))
    s = _num(data.get("support"))
    if s is not None and s < price:
        downs.append(("前20日支撑", s))

    ups.sort(key=lambda x: x[1])       # 压力：由近到远 = 升序
    downs.sort(key=lambda x: -x[1])    # 支撑：由近到远 = 降序
    return ups, downs


def build_conditions(data):
    """生成某只标的的 If-Then 条件清单（早盘用；盘后的「明日框架」也用它）。

    取「最近的压力位」与「最近的支撑位」各 1 个，各派生 2 条条件：
        压力：U1 盘中上冲触及 / U2 收盘站上
        支撑：D1 盘中下探触及 / D2 收盘失守
    → 每个方向都覆盖「盘中试探」与「收盘确认」两档，避免只有一条而无法定性。

    返回 list[dict]；候选价位缺失时**少生成**（R5），不补假价位。
    """
    price = _num(data.get("price"))
    if price is None:
        return []

    ups, downs = price_candidates(data)
    conds = []

    if ups:
        nm, lv = ups[0]
        lv = round(lv, 2)
        conds.append({"id": "U1", "side": "up", "trigger": "touch",
                      "level": lv, "ref": nm})
        conds.append({"id": "U2", "side": "up", "trigger": "close",
                      "level": lv, "ref": nm})

    if downs:
        nm, lv = downs[0]
        lv = round(lv, 2)
        conds.append({"id": "D1", "side": "down", "trigger": "touch",
                      "level": lv, "ref": nm})
        conds.append({"id": "D2", "side": "down", "trigger": "close",
                      "level": lv, "ref": nm})

    return conds


def evaluate(cond, day):
    """用【某一日】的行情判定单条条件是否命中。

    day 只需要三个字段：high / low / price（当日收盘价）。
    返回 {"hit": True|False|None, "why": "<程序算出的依据>"}
        hit=None → 数据不足，判定不了。★ 绝不猜、绝不默认 True/False（R5）
    """
    lv = _num(cond.get("level"))
    if lv is None:
        return {"hit": None, "why": "条件价位缺失"}

    hi = _num(day.get("high"))
    lo = _num(day.get("low"))
    close = _num(day.get("price"))

    side, trig = cond.get("side"), cond.get("trigger")

    if side == "up":
        if trig == "touch":
            if hi is None:
                return {"hit": None, "why": "当日最高价缺失"}
            return {"hit": hi >= lv, "why": "日内最高 %.2f" % hi}
        if close is None:
            return {"hit": None, "why": "当日收盘价缺失"}
        return {"hit": close >= lv, "why": "收盘 %.2f" % close}

    if side == "down":
        if trig == "touch":
            if lo is None:
                return {"hit": None, "why": "当日最低价缺失"}
            return {"hit": lo <= lv, "why": "日内最低 %.2f" % lo}
        if close is None:
            return {"hit": None, "why": "当日收盘价缺失"}
        return {"hit": close <= lv, "why": "收盘 %.2f" % close}

    return {"hit": None, "why": "未知条件类型 %r/%r" % (side, trig)}


def cond_text(c):
    """条件的「剧本句式」—— 早盘推送和盘后回顾必须用同一句，故在此统一生成。"""
    lv = c.get("level")
    if not isinstance(lv, (int, float)):
        return "（价位缺失，条件未生成）"
    ref = c.get("ref") or ""
    tail = ("（%s）" % ref) if ref else ""
    side, trig = c.get("side"), c.get("trigger")
    if trig == "touch":
        verb = "上冲触及" if side == "up" else "下探触及"
        return "若盘中%s %.2f%s" % (verb, lv, tail)
    verb = "站上" if side == "up" else "失守"
    return "若收盘%s %.2f%s" % (verb, lv, tail)


def result_text(c, r):
    """把求值结果写成结论词。命中/未命中由程序给定，不得改写。"""
    hit = r.get("hit")
    why = r.get("why") or ""
    if hit is None:
        return "❓ 无法判定（%s）" % why
    if hit:
        return "✅ 已触发（%s）" % why
    return "⬜ 未触发（%s）" % why


def tally(results):
    """统计命中/未命中/无法判定 —— 计数也必须由程序做（R3：模型不会数数）。"""
    hit = sum(1 for r in results if r.get("hit") is True)
    miss = sum(1 for r in results if r.get("hit") is False)
    unk = sum(1 for r in results if r.get("hit") is None)
    return {"hit": hit, "miss": miss, "unknown": unk, "total": len(results)}


def shape(hits):
    """把「哪几条命中」归纳成一个固定的形态结论 —— 纯查表，不含任何主观判断。

    hits: {"U1": True/False/None, "U2": ..., "D1": ..., "D2": ...}
    逻辑上的必然关系（不必假设，直接由条件语义推出）：
        收盘站上压力位 ⇒ 必然已触及过压力位（U2 ⇒ U1）
        收盘失守支撑位 ⇒ 必然已跌破过支撑位（D2 ⇒ D1）
    所以「U1 命中而 U2 未命中」= 冲高回落，这是可靠的程序结论。
    """
    up_touch = hits.get("U1") is True
    up_close = hits.get("U2") is True
    dn_touch = hits.get("D1") is True
    dn_close = hits.get("D2") is True

    segs = []
    if up_close:
        segs.append("收盘站上压力位（压力转支撑）")
    elif up_touch:
        segs.append("盘中触及压力位但收盘未站稳（冲高回落）")
    if dn_close:
        segs.append("收盘失守支撑位（破位）")
    elif dn_touch:
        segs.append("盘中跌破支撑位后收回（探底回升）")

    if not segs:
        if any(v is None for v in hits.values()):
            return "数据不足，无法归纳形态"
        return "两端条件均未触发：价格在既定区间内运行，结构未变"
    return " ｜ ".join(segs)


# ============================================================ 落盘协议（唯一定义处）
#
# ⚠️ 路径与读写只在这里定义一次。早盘写、盘后读必须走同一份代码，
#    否则两边各写一份实现迟早漂移（文件名/目录/编码任一不一致就静默失联）。
#    R1 绝对路径：cron 唤醒时 CWD 不可控，一律用绝对路径。

def _script_dir():
    """生产脚本目录。与两个 runner 使用同一条解析规则（可用环境变量覆盖）。"""
    return (os.environ.get("ASTOCK_SCRIPT_DIR")
            or "/vol1/@appdata/trim.hermes/hermes/scripts")


def state_dir():
    return os.path.join(_script_dir(), "state")


def plan_path(trade_date):
    """某交易日的计划文件绝对路径"""
    return os.path.join(state_dir(), "plan_%s.json" % trade_date)


def save_plan(plan):
    """原子写：先写 .tmp 再 os.replace —— 避免盘后读到半截文件。"""
    d = state_dir()
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    p = plan_path(plan["trade_date"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return p


def load_plan(trade_date):
    """读计划文件。返回 (plan | None, 说明文字)

    ⚠️ 任何异常都必须转成「(None, 原因)」而不是抛出去 ——
       盘后脚本的职责是「找不到就明说缺失」，不是崩掉。
    """
    p = plan_path(trade_date)
    if not os.path.isfile(p):
        return None, "未找到计划文件 %s（今早作业可能未运行或落盘失败）" % p
    try:
        with open(p, "r", encoding="utf-8") as f:
            plan = json.load(f)
    except Exception as e:
        return None, "计划文件解析失败 %s: %s" % (type(e).__name__, str(e)[:80])
    if not isinstance(plan, dict):
        return None, "计划文件结构异常（顶层不是对象）"
    if not isinstance(plan.get("stocks"), list):
        return None, "计划文件结构异常（stocks 不是数组）"
    if plan.get("trade_date") != trade_date:
        return None, "计划文件日期不符（文件内 %r ≠ 今日 %r）" % (
            plan.get("trade_date"), trade_date)
    return plan, "ok"
