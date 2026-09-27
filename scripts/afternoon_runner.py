# -*- coding: utf-8 -*-
"""盘后对账与明日框架生成器（15:30 触发）—— 双轨闭环「下半轨」· v3 模板直出

═══════════════════════════════════════════════════════════════════════════
v3 核心变更：彻底剥离 LLM，改走 Hermes `--no-agent` 模式
═══════════════════════════════════════════════════════════════════════════
v2 是「脚本出数据块 + 提示词（含 context_from 注入的早盘剧本）→ 大模型对账」。
v3 改为 **脚本 stdout 原文 = 微信推送正文**，0 次 LLM 调用。

为什么必须这么做（不是偏好，是送达率）：
  · LLM 挂在 15:30 的关键路径上，一次 429 就丢掉整条推送
  · 「对账」这件事本来就不该由模型做 —— 判定「今天有没有触及 51.95」
    是一次纯数值比较，模型做这个只会引入不确定性
  · 模型还会自由发挥（旧作业产出过「做反 T 800 股」，触犯红线）

═══════════════════════════════════════════════════════════════════════════
今早的剧本是怎么来到这里的（v3 改了机制）
═══════════════════════════════════════════════════════════════════════════
v2 靠 Hermes 的 `context_from` 把早盘作业的 `## Response` 注入本作业 prompt。
**--no-agent 模式下没有 prompt ⇒ context_from 机制失效**（它注入的对象不存在）。

v3 改为读结构化文件：
    <脚本目录>/state/plan_<今日>.json      ← 早盘 08:45 落盘
    由 plan_engine.load_plan() 读取，路径规则与早盘共用同一份代码。

对账方式：用【今日的实际 OHLC】对【今早写下的每条条件】做程序化判定。
    side=up   trigger=touch → 命中 ⟺ 今日最高价 ≥ level
    side=up   trigger=close → 命中 ⟺ 今日收盘价 ≥ level
    side=down trigger=touch → 命中 ⟺ 今日最低价 ≤ level
    side=down trigger=close → 命中 ⟺ 今日收盘价 ≤ level
⇒ 对账结果 100% 可复算，不存在「模型印象里今早说过什么」。

🔴 剧本缺失时（早盘没跑/落盘失败/文件损坏）：
   必须在报告里**明说缺失并给出原因**，然后只做今日数据的定性诊断。
   ★ 绝对禁止编造「今早说过什么」—— 没有就是没有。

═══════════════════════════════════════════════════════════════════════════
架构红线（100% 遵守，与 morning_runner 同）
═══════════════════════════════════════════════════════════════════════════
R1 绝对路径 ｜ R2 纯标准库 ｜ R3 算力隔离 ｜ R4 节假日静默 ｜ R5 缺失不填 0

`--no-agent` 语义（cron/scheduler.py:4270-4332 源码确认）：
    stdout 原文 = 投递内容；空 stdout / wakeAgent=false = 静默；
    ⚠️ 非零退出码 = 错误告警投递 ⇒ main() 必须恒返回 0。

R4 静默：Hermes 没有 `__NO_PUSH__` 这个协议（全站 grep 零命中，旧 OpenClaw 遗留）。
         真正的静默通道是 wake gate（_parse_wake_gate，scheduler.py:3418-3440，
         调用点 :4536 agent / :4353 no_agent）。

送达率优先：确认非交易日 → 静默；交易日 + 任何故障 → 推简短告警（不静默失效）。
"""

import json
import os
import sys
import datetime

# ---------------------------------------------------------------------------
# R1 绝对路径
# ---------------------------------------------------------------------------
ABS_SCRIPT_DIR = '/vol1/@appdata/trim.hermes/hermes/scripts'

_OVERRIDE = os.environ.get('ASTOCK_SCRIPT_DIR')
if _OVERRIDE:
    ABS_SCRIPT_DIR = _OVERRIDE

if ABS_SCRIPT_DIR not in sys.path:
    sys.path.insert(0, ABS_SCRIPT_DIR)

CONFIG_PATH = os.path.join(ABS_SCRIPT_DIR, 'config.json')

import trading_calendar as tc
import plan_engine as pe
from fetch_full_panorama import (
    get_tencent_panorama,
    fetch_juchao_announcement_by_search,
    last_closed_trading_day,
    _f,
    _pe,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
WAKE_GATE_OFF = '{"wakeAgent": false}'

# 盘后公告窗口：往回 15 小时（覆盖今日 00:00 → 15:30），与早盘「昨晚到今早」互补
ANNOUNCE_LOOKBACK_HOURS = 15

_WEEK = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')


# ============================================================ 工具

def _load_config():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def _yi(v):
    """元 → 亿元字符串（带符号）；None → '未取到'（R5）"""
    return "未取到" if v is None else "%+.3f亿" % (v / 1e8)


def _ma_pos(price, pairs):
    """均线相对现价 —— 程序判定「上方/下方」（R3）"""
    above, below = [], []
    for nm, v in pairs:
        if v is None:
            continue
        (above if v > price else below).append(nm)
    if below and above:
        pos = "现价在 %s 上方，在 %s 下方" % ("、".join(below), "、".join(above))
    elif below:
        pos = "现价在 %s 上方（上方已无均线压制）" % "、".join(below)
    elif above:
        pos = "现价在 %s 下方（下方已无均线支撑）" % "、".join(above)
    else:
        pos = "现价与各均线基本重合"
    return above, below, pos


def _ob_os(data):
    """超买/超卖状态 —— 程序判定（R3）"""
    k, d = data.get('kdj_k'), data.get('kdj_d')
    r6 = data.get('rsi6')
    wr14 = data.get('wr14')
    bits = []
    if k is not None and d is not None:
        if k > 80 and d > 80:
            bits.append("KDJ 超买区(K、D 均>80)")
        elif k < 20 and d < 20:
            bits.append("KDJ 超卖区(K、D 均<20)")
        else:
            bits.append("KDJ 中性区")
    if r6 is not None:
        if r6 > 80:
            bits.append("RSI6 超买(>80)")
        elif r6 < 20:
            bits.append("RSI6 超卖(<20)")
        else:
            bits.append("RSI6 中性")
    if wr14 is not None:
        if wr14 > 80:
            bits.append("WR14 超卖(>80)")
        elif wr14 < 20:
            bits.append("WR14 超买(<20)")
    return " ｜ ".join(bits) if bits else "数据不足"


def _vol_tag(data):
    """量能描述 —— 固定阈值程序判定（R3）"""
    vr = data.get('vol_ratio')
    if vr is None:
        tag = "量比未取到"
    elif vr >= 1.5:
        tag = "明显放量"
    elif vr <= 0.8:
        tag = "明显缩量"
    else:
        tag = "量能平稳"
    vt, va = data.get('vol_today'), data.get('vol_avg20')
    r20 = (vt / va) if (isinstance(vt, (int, float)) and isinstance(va, (int, float)) and va) else None
    s = "换手 %s%% ｜ 量比 %s" % (_f(data.get('turnover')), _f(vr))
    if r20 is not None:
        s += " ｜ 20日均量倍数 %.2f" % r20
    return s + " → " + tag


def _shape_today(data):
    """今日 K 线形态 —— 收盘在当日振幅中的位置由程序算（R3）"""
    hi, lo, op, close = data.get('high'), data.get('low'), data.get('open'), data.get('price')
    if None in (hi, lo, op, close) or hi <= lo:
        return "振幅/收盘位置：数据不足"
    amp = (hi - lo) / lo * 100
    pos_pct = (close - lo) / (hi - lo) * 100
    if pos_pct >= 70:
        pos_txt = "收于当日振幅上沿（尾盘偏强）"
    elif pos_pct <= 30:
        pos_txt = "收于当日振幅下沿（尾盘偏弱）"
    else:
        pos_txt = "收于当日振幅中部（多空胶着）"
    return "振幅 %.2f%% ｜ 收盘位于当日区间 %.1f%% 处 → %s" % (amp, pos_pct, pos_txt)


def _key_levels(data):
    """头顶压力 / 脚下支撑，程序排序（R3）。返回 (ups, downs)，元素=(名称, 价位)"""
    return pe.price_candidates(data)


def _collect_announcements(name, since_ts, limit=3):
    """取今日公告。返回 (标题列表, 是否取数失败)"""
    res = fetch_juchao_announcement_by_search(name, limit=limit, since_ts=since_ts)
    if len(res) == 1 and ('失败' in res[0] or '疑似' in res[0]):
        return res, True
    # 🔴 单元素「接口健康说明」不是公告 —— 按 0 条返回（与 morning_runner 同口径）。
    #    不区分时渲染层会写「今日新增公告 1 条」而内容是「无新增公告」，数字自相矛盾。
    if len(res) == 1 and ('无新增公告' in res[0] or '无公告记录' in res[0]):
        return [], False
    return res, False


def _alert_text(title, detail):
    """链路故障告警 —— 明确区别于行情信息"""
    return "\n".join([
        "⚠️ 今日盘后对账生成失败",
        "",
        "原因：%s" % title,
        "详情：%s" % detail,
        "",
        "说明：这条【不是行情判断】，是复盘链路本身的故障告警。",
    ])


# ============================================================ 渲染

def render_reconcile(L, data, plan_stock, plan_meta):
    """渲染【对账】段：今早每条条件 × 今日实际 → 程序化判定"""
    ap = L.append
    ap("【对账：今早剧本逐条核验】")

    if plan_stock is None:
        ap("  ⚠️ 今早盘前剧本缺失，无法逐条对账。")
        if plan_meta.get('why'):
            ap("  原因：%s" % plan_meta['why'])
        ap("  ⇒ 以下只对今日数据做定性诊断，**绝不编造「今早说过什么」**。")
        return None

    conds = plan_stock.get('conds') or []
    if not conds:
        ap("  今早该标的下未生成任何条件（无可用价位参照），无对账项。")
        return None

    base_date = plan_stock.get('base_date') or plan_meta.get('base_date') or '未知'
    ap("  基准：%s 收盘 %s（今早 %s 生成）" % (
        base_date,
        _f(plan_stock.get('base_price')),
        plan_meta.get('generated_at') or '时间未知'))

    results = []
    hits = {}
    for c in conds:
        r = pe.evaluate(c, data)
        results.append(r)
        hits[c.get('id')] = r.get('hit')
        ap("  %s %s → %s" % (c.get('id'), pe.cond_text(c), pe.result_text(c, r)))

    t = pe.tally(results)
    ap("  程序统计：共 %d 条 ｜ 触发 %d ｜ 未触发 %d ｜ 无法判定 %d"
       % (t['total'], t['hit'], t['miss'], t['unknown']))
    ap("  形态结论（由命中组合查表得出，非主观判断）：%s" % pe.shape(hits))
    return hits


def render_stock(L, data, plan_stock, plan_meta, ann_titles, ann_failed, style):
    """渲染单只标的的【盘后对账 + 诊断 + 明日框架】"""
    ap = L.append

    ap("━━━ %s %s ━━━" % (data['name'], data['code']))
    ap("【今日收盘】%s 元（开 %s / 高 %s / 低 %s）" % (
        _f(data.get('price')), _f(data.get('open')),
        _f(data.get('high')), _f(data.get('low'))))
    ap("  · 形态：%s" % _shape_today(data))
    ap("  · 量能：%s" % _vol_tag(data))
    ap("  · 技术：%s" % _ob_os(data))

    hits = render_reconcile(L, data, plan_stock, plan_meta)

    # ---- 今日诊断 ----
    ap("【今日诊断】")
    ups, downs = _key_levels(data)
    price = data.get('price')
    if ups:
        ap("  · 头顶最近压力（程序已排序）：" + " ｜ ".join(
            "%s %.2f(距收盘 %+.2f%%)" % (nm, v, (v - price) / price * 100)
            for nm, v in ups[:3]))
    else:
        ap("  · 头顶最近压力：无（收盘站上全部均线）")
    if downs:
        ap("  · 脚下最近支撑（程序已排序）：" + " ｜ ".join(
            "%s %.2f(距收盘 %+.2f%%)" % (nm, v, -(price - v) / price * 100)
            for nm, v in downs[:3]))
    else:
        ap("  · 脚下最近支撑：无（收盘跌破全部均线）")

    mf = data.get('main_net_inflow') or {}
    v = mf.get('value')
    if v is None:
        ap("  · 资金：今日主力净额未取到（%s）｜ PE(TTM) %s"
           % (mf.get('error', '原因不明'), _pe(data.get('pe_ttm'))))
    else:
        line = "  · 资金：今日主力 %s（netamount 四档合计，截止 %s）" % (_yi(v), mf.get('date'))
        if mf.get('acc5') is not None:
            line += "｜ 5日 %s ｜ 10日 %s ｜ 20日 %s" % (
                _yi(mf['acc5']), _yi(mf['acc10']), _yi(mf['acc20']))
        ap(line)
        ap("    方向（程序已判定，禁止再改判）：当日vs5日=%s ｜ 综合=%s ｜ 标签=%s ｜ PE(TTM) %s"
           % (mf.get('dir5'), mf.get('flow_dir'), mf.get('flow_tag'),
              _pe(data.get('pe_ttm'))))
        ser = mf.get('series') or []
        if ser:
            ap("    近6日净额：" + " ｜ ".join(
                "%s %+.2f亿" % (x['date'][5:], x['value'] / 1e8) for x in ser[:6]))

    if ann_failed:
        ap("  · 消息面：读取失败 —— %s" % ann_titles[0])
        ap("    ⚠️ 这是取数故障，**不能**据此推断「公司无公告」。")
    else:
        ap("  · 消息面：今日新增公告 %d 条" % len(ann_titles))
        for i, t in enumerate(ann_titles, 1):
            ap("    %d. %s" % (i, t))

    # ---- 明日框架（用今日数据重新生成条件，语义与早盘完全一致）----
    tomorrow = pe.build_conditions(data)
    ap("【明日 If-Then 框架】基准 %s（今日收盘）" % _f(price))
    if not tomorrow:
        ap("  · 无可用价位参照，明日不出条件")
    else:
        for c in tomorrow:
            ap("  %s %s" % (c['id'], pe.cond_text(c)))
        if len(ups) > 1:
            ap("  · 次级压力：%s" % "、".join("%s %.2f" % (n, x) for n, x in ups[1:3]))
        if len(downs) > 1:
            ap("  · 次级支撑：%s" % "、".join("%s %.2f" % (n, x) for n, x in downs[1:3]))

    if hits is None:
        ap("【对账状态】今早剧本缺失 → 今日未进行逐条核验（不是「全部未触发」）")

    if style:
        ap("【你的既定纪律（原样引用，不解释）】%s" % style)
    ap("")


# ============================================================ 主流程

def build_report(now=None):
    """构造盘后对账报告。

    返回 (should_push: bool, text: str)
      · should_push=True  → text 为【推送正文】
      · should_push=False → text 为【静默原因】，main() 写 stderr + 打 wake gate
    """
    if now is None:
        now = datetime.datetime.now()
    today_str = now.strftime('%Y-%m-%d')

    # ---- R4 交易日闸门（最先判）----
    try:
        istd = tc.is_trading_day(now.date())
    except Exception:
        istd = None

    if istd is None:
        if now.weekday() >= 5:
            return False, "%s 交易日历不可用，但今日为周末，静默" % today_str
        return True, _alert_text(
            "交易日历不可用，无法判定今天是否为交易日",
            "日历覆盖范围: %s" % (tc.coverage() if hasattr(tc, 'coverage') else '未知'))

    if istd is False:
        kind = "周末" if now.weekday() >= 5 else "节假日"
        return False, "%s 非交易日（%s），无盘后" % (today_str, kind)

    # ================= 以下是交易日：任何故障都推告警 =================
    try:
        expect = last_closed_trading_day(now)
        if expect is None:
            return True, _alert_text("无法确定「最近已收盘交易日」", "交易日历超出覆盖范围")

        # 盘后必须确认「今日确实已收盘」：expect 应等于今天。
        # 不等说明日历认为今天没收盘（异常），但也不能静默 —— 静默＝用户不知道今天没报告
        if expect != today_str:
            return True, _alert_text(
                "日历状态异常：盘后基准日 ≠ 今日",
                "基准日 %s，今日 %s" % (expect, today_str))

        try:
            config = _load_config()
        except Exception as e:
            return True, _alert_text(
                "配置文件读取失败", "%s: %s" % (type(e).__name__, str(e)[:120]))

        stocks = [s for s in config.get('stocks', []) if s.get('enabled', True)]
        if not stocks:
            return True, _alert_text("config.json 中没有启用中的标的", CONFIG_PATH)

        style = config.get('style', '')

        # ---- 读今早的计划（缺失也要明说，不静默、不编造）----
        plan, why = pe.load_plan(today_str)
        if plan is None:
            plan_meta = {'why': why, 'base_date': None, 'generated_at': None}
            plan_stocks = {}
        else:
            plan_meta = {'why': None,
                         'base_date': plan.get('base_date'),
                         'generated_at': plan.get('generated_at')}
            plan_stocks = {s.get('code'): s for s in plan.get('stocks', [])
                           if isinstance(s, dict)}

        # 公告窗口：now - 15h
        since_dt = now - datetime.timedelta(hours=ANNOUNCE_LOOKBACK_HOURS)
        since_ts = int(since_dt.timestamp() * 1000)

        L = []
        L.append("📊 盘后对账与明日框架 · %s（%s）" % (today_str, _WEEK[now.weekday()]))
        L.append("━" * 34)
        L.append("数据基准：%s 收盘 ｜ 前复权 ｜ 价格源:腾讯 ｜ 资金源:新浪netamount ｜ 公告源:巨潮全文检索"
                 % expect)
        L.append("生成时间：%s（已收盘）" % now.strftime('%Y-%m-%d %H:%M'))
        if plan is None:
            L.append("对账依据：⚠️ 今早剧本缺失 —— %s" % why)
        else:
            L.append("对账依据：plan_%s.json（今早 %s 生成，基准 %s 收盘）"
                     % (today_str, plan.get('generated_at') or '?', plan.get('base_date') or '?'))
        L.append("")

        failed = []
        got = 0
        seen = set()
        for s in stocks:
            data = get_tencent_panorama(s['code'], now)
            if not data.get('pass'):
                failed.append((s['code'], data.get('error', '未知错误')))
                continue
            got += 1
            code = data.get('code') or s['code']
            seen.add(code)
            name = data.get('name') or s.get('name', '')
            ann_titles, ann_failed = _collect_announcements(name, since_ts, limit=3)
            render_stock(L, data, plan_stocks.get(code), plan_meta,
                         ann_titles, ann_failed, style)

        # 计划里有、今天却没取的（配置改过 or 代码对不上）—— 必须说出来，不能悄悄少对一条
        orphan = [c for c in plan_stocks if c not in seen]
        if orphan:
            L.append("【今早有剧本但今日未核验的标的】%s" % "、".join(orphan))
            L.append("  （原因：今日取数失败，或已从 config.json 移除）")
            L.append("")

        if failed:
            L.append("【本次未取到的标的】")
            for code, err in failed:
                L.append("  - %s: %s" % (code, err))
            L.append("")

        if got == 0:
            detail = "；".join("%s(%s)" % (c, e) for c, e in failed) or "无标的"
            return True, _alert_text("全部标的取数失败，今日无盘后对账", detail)

        L.append("【纪律】以上全部为「条件 → 状态」的观察框架，由程序按数据块价位生成；")
        L.append("       不含任何买入/卖出/加仓/减仓/止损/止盈指令。")
        L.append("       本报告由 Python 模板直出（未经大模型），对账判定可逐条复算。")
        return True, "\n".join(L)

    except Exception as e:
        return True, _alert_text(
            "生成过程未预期异常", "%s: %s" % (type(e).__name__, str(e)[:160]))


def main():
    """stdout 协议（Hermes cron --no-agent 依赖）：
      · 正常 → 打印报告正文（stdout 原文即微信推送内容）
      · 静默 → stderr 写原因，stdout 最后一行打 wakeAgent 门
    退出码恒 0 —— ⚠️ no_agent 模式下非零退出会被当【错误告警】投递给用户。
    """
    try:
        should_push, text = build_report()
    except Exception as e:
        sys.stderr.write("[afternoon_runner] build_report 崩溃: %s: %s\n"
                         % (type(e).__name__, str(e)[:200]))
        print(WAKE_GATE_OFF)
        return 0

    if not should_push:
        sys.stderr.write("[afternoon_runner] 静默不推送: %s\n" % text)
        print(WAKE_GATE_OFF)
        return 0

    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
