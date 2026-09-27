# -*- coding: utf-8 -*-
"""修复版 + 扩展版 post_market_panorama.py

基于 NAS 生产版本（sha256 a77ce22b...）修改。

修复清单：
  [E] 时间判断 —— 由「now.hour>=15」改为「是否交易日 + 盘中/盘后」
      （原版只看钟点，国庆等假期会照常出「盘后」文案）
  [G] 交易日闸门 —— 非交易日直接输出 __NO_PUSH__ 并退出，不生成报告
  [F] 资金流单位 —— 明确以「亿元」展示，并声明数据口径
  [H] 数据自披露首行 —— 固定输出数据截止日 / 数据源
  [I] 模块化 —— 执行逻辑收进 main()，import 时不再有副作用
      （原版在模块顶层直接 sys.exit，导致 import 本模块会终止调用方进程）

扩展清单（2026-09-27，用户批准）：
  [A] 技术指标展示 —— MACD / KDJ / RSI / BOLL / BIAS / WR + 60/250 日区间
  [B] 资金流累积展示 —— 5/10/20 日累积 + 方向标签（程序算好，模型只读）

⚠️ 输出协议（Hermes cron 依赖）：
    - 正常：打印报告正文（stdout 原文直发）
    - 不推送：第一行必须是 __NO_PUSH__ 前缀
    - 退出码恒为 0（cron 不据退出码判断，据 stdout 前缀）

⚠️ 架构红线：数字全由程序算，模型只读不算。
    凡「同向/反向」「在均线上方/下方」这类需要比较的结论，一律本程序算好，
    报告中直接给结论词，禁止让模型自己拿两个数字比。
"""
import json
import os
import sys
import datetime

import trading_calendar as tc
from fetch_full_panorama import (
    get_tencent_panorama,
    fetch_juchao_announcement,
    last_closed_trading_day,
    _f,
    _pe,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, 'config.json')

NO_PUSH = "__NO_PUSH__"


def _load_config():
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def _yi(v):
    """元 → 亿元字符串（带正负号）；None → '未取到'"""
    return "未取到" if v is None else f"{v/1e8:+.3f}亿"


# ⚠️ _pe 已于 2026-09-27 上移到 fetch_full_panorama.py（morning/afternoon_runner
#    都要用，放这里会让新脚本反向依赖旧入口）。此处改为 import，行为完全不变：
#      None → '-' ｜ 负数 → '亏损' ｜ 正数 → 两位小数
#    旧定义（已移除，留档防误找）：
#      def _pe(v):
#          if v is None: return '-'
#          if v < 0: return '亏损'
#          return f"{v:.2f}"


def _ma_pos(price, pairs):
    """均线相对现价 —— 程序判定「上方/下方」，模型不得自行比较。

    返回 (压力列表, 支撑列表, 位置整句)
    """
    above, below = [], []
    for nm, v in pairs:
        if v is None:
            continue
        (above if v > price else below).append(nm)
    parts = []
    if below:
        parts.append("现价在 %s 上方" % "、".join(below))
    if above:
        parts.append("在 %s 下方" % "、".join(above))
    pos = "，".join(parts) if parts else "现价与各均线基本重合"
    return above, below, pos


def build_stock_block(L, s, data, mf):
    """拼装单只标的数据块（含技术指标与资金流累积）。"""
    ap = L.append

    ap(f"🎯 标的：{data['name']} ({data['code']})")
    ap(f"基准日期: {data['date']} ｜ K线: 前复权 {data.get('bars', '?')} 根")
    ap(f"收盘价: {data['price']} 元 (开:{data['open']}, 高:{data['high']}, 低:{data['low']})"
       f" ｜ 涨跌: {_f(data.get('chg5'), 2)}%(5日) {_f(data.get('chg10'), 2)}%(10日) "
       f"{_f(data.get('chg20'), 2)}%(20日) {_f(data.get('chg60'), 2)}%(60日)")
    ap(f"[筹码资金] 换手率: {data['turnover']}% | 腾讯量比: {data['vol_ratio']}"
       f" | 20日均量倍数: "
       + (_f(data['vol_today'] / data['vol_avg20'], 2) if data.get('vol_avg20') else '-'))

    # ---------------- [A] 技术面 ----------------
    ap("[技术面] 均线阵列: "
       f"MA5={_f(data['ma5'])} MA10={_f(data['ma10'])} MA20={_f(data['ma20'])} "
       f"MA30={_f(data['ma30'])} MA60={_f(data['ma60'])} "
       f"MA120={_f(data['ma120'])} MA250={_f(data['ma250'])}")

    # 均线位置：程序算好「上方/下方」，模型只读取结论
    pairs = [("MA5", data['ma5']), ("MA10", data['ma10']), ("MA20", data['ma20']),
             ("MA30", data['ma30']), ("MA60", data['ma60']),
             ("MA120", data['ma120']), ("MA250", data['ma250'])]
    above, below, pos = _ma_pos(data['price'], pairs)
    ap(f"[技术面] 均线相对现价（口径: 均线价高于现价=压力/在上方；低于=支撑/在下方）: "
       f"压力 {'/'.join(above) or '无'} ｜ 支撑 {'/'.join(below) or '无'}")
    ap(f"[技术面] 位置（整句照抄，禁止改写方位）: {pos}")

    ap(f"[技术面] 前20日压力位(不含当日): {data['resistance']} 元 | "
       f"支撑位: {data['support']} 元")
    ap(f"[技术面] 区间(不含当日): "
       f"60日 高{data['high60']}/低{data['low60']} "
       f"(现价距高 {_f((data['price']/data['high60']-1)*100, 1)}%, "
       f"距低 {_f((data['price']/data['low60']-1)*100, 1)}%) | "
       f"250日 高{data['high250']}/低{data['low250']}")

    ap(f"[技术面] MACD: DIF={_f(data['macd_dif'], 3)} DEA={_f(data['macd_dea'], 3)} "
       f"柱={_f(data['macd_bar'], 3)} ｜ KDJ: K={_f(data['kdj_k'], 1)} "
       f"D={_f(data['kdj_d'], 1)} J={_f(data['kdj_j'], 1)}")
    ap(f"[技术面] RSI: RSI6={_f(data['rsi6'], 1)} RSI12={_f(data['rsi12'], 1)} "
       f"RSI24={_f(data['rsi24'], 1)} ｜ 超买超卖状态(程序判定): {_ob_os(data)}")
    ap(f"[技术面] BOLL: 上轨={_f(data['boll_up'])} 中轨={_f(data['boll_mid'])} "
       f"下轨={_f(data['boll_low'])} ｜ BIAS6={_f(data['bias6'], 1)}% "
       f"BIAS12={_f(data['bias12'], 1)}% WR14={_f(data['wr14'], 1)}")

    # ---------------- [B] 资金流 ----------------
    v = mf.get('value')
    if v is None:
        ap(f"[真实主力动向] 主力净额: 未取到（{mf.get('error', '原因不明')}） | "
           f"PE(TTM): {_pe(data['pe_ttm'])}")
    else:
        ap(f"[真实主力动向] 主力净额(当日): {_yi(v)} "
           f"(口径:新浪netamount=超大+大+中+小四档合计, 截止{mf.get('date')}) | "
           f"PE(TTM): {_pe(data['pe_ttm'])}")
        if mf.get('acc5') is not None:
            ap(f"[资金持续性] 累积净额: 5日 {_yi(mf['acc5'])} ｜ 10日 {_yi(mf['acc10'])} ｜ "
               f"20日 {_yi(mf['acc20'])}")
            ap(f"[资金持续性] 方向判定(程序已算好，禁止模型自行比较): "
               f"当日vs5日={mf.get('dir5')} ｜ 当日vs10日={mf.get('dir10')} ｜ "
               f"当日vs20日={mf.get('dir20')} ｜ 综合={mf.get('flow_dir')} ｜ "
               f"标签={mf.get('flow_tag')}")
            # 逐日明细（近 6 日）
            ser = mf.get('series') or []
            if ser:
                ap("[资金持续性] 近6日净额明细: "
                   + " | ".join(f"{x['date'][5:]} {x['value']/1e8:+.2f}亿" for x in ser[:6]))

    ap("[巨潮最新公告监控]:")
    for n in fetch_juchao_announcement(s['code'], data['name'], limit=2):
        ap("  - " + n)
    ap("-" * 40)


def _ob_os(data):
    """超买/超卖状态 —— 程序判定，模型只读"""
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


def build_report(now=None):
    """构造报告正文。

    返回 (should_push: bool, text: str)
      should_push=False 时 text 为 __NO_PUSH__ 原因（单行）。
    """
    if now is None:
        now = datetime.datetime.now()
    today_str = now.strftime('%Y-%m-%d')

    # ---- [G] 交易日闸门 ----
    istd = tc.is_trading_day(now.date())
    if istd is None:
        return False, (f"{NO_PUSH} 交易日历不可用，无法判定 {today_str} 是否为交易日"
                       f"（覆盖范围: {tc.coverage()}）")
    if istd is False:
        kind = "周末" if now.weekday() >= 5 else "节假日"
        return False, f"{NO_PUSH} {today_str} 非交易日（{kind}）"

    expect = last_closed_trading_day(now)
    if expect is None:
        return False, f"{NO_PUSH} 无法确定最近已收盘交易日（日历超范围）"

    # ---- [E] 盘中/盘后 ----
    phase = "盘后" if now.hour >= 15 else "盘中"
    if phase == "盘后":
        time_warning = (f"【当前时间状态】现在是盘后（{now:%Y-%m-%d %H:%M}）。"
                        f"数据截止 {expect} 收盘。所有应对剧本必须指向【下一交易日开盘后】。")
    else:
        time_warning = (f"【当前时间状态】现在是盘中（{now:%Y-%m-%d %H:%M}）。"
                        f"数据截止 {expect} 收盘，重点监控支撑与压力位的得失。")

    config = _load_config()
    L = []
    ap = L.append

    # ---- [H] 数据自披露 ----
    ap(f"数据截止 {expect} 收盘 ｜ 前复权 ｜ 价格源:腾讯 ｜ "
       f"资金源:新浪MoneyFlow(netamount) ｜ 公告源:巨潮\n")
    ap("【系统背景指令】")
    ap("你是一个冷酷、理性的 A 股实战交易幕僚。结合技术面、资金面和个股消息面，做系统级推演。")
    ap(f"用户的底牌与操作纪律：{config['style']}\n")
    ap(time_warning)
    ap("以下是绝对真实的 3D 全景数据（涵盖技术/主力/舆情）。请生成推送到微信的《全景复盘与推演报告》。")
    ap("【输出结构要求】：")
    ap("1. [个股公告与情绪]：总结巨潮传来的最新个股公告，一句话定性情绪。")
    ap("2. [资金与技术共振诊断]：不仅要看均线支撑，必须点出真实主力资金"
       "（净流入/净流出）和换手率背后的逻辑（例如：假突破真出货，还是缩量洗盘）。")
    ap("3. [明日推演与 If-Then 剧本]：必须带有具体点位的条件触发预案。")
    ap("【硬性纪律】：")
    ap("  · 凡数据块中标注「程序已算好」「程序判定」「整句照抄」的结论词"
       "（如 同向/反向/背离/上方/下方/超买/超卖），一律直接引用，禁止自行拿两个数字比较。")
    ap("  · 禁止引入数据块之外的信息（不得引用任何外部新闻、传闻或历史记忆）。")
    ap("  · 禁止给出买入/卖出/加仓/减仓/止损/止盈等操作指令。\n")

    ap("================ [个股全景数据输入] ================")
    failed = []
    got = 0
    for s in config['stocks']:
        data = get_tencent_panorama(s['code'], now)
        if not data['pass']:
            failed.append((s['code'], data['error']))
            ap(f"抓取失败: {s['code']} - {data['error']}")
            continue
        got += 1
        build_stock_block(L, s, data, data['main_net_inflow'])

    if failed:
        ap("")
        ap("【本次未取到的标的】")
        for code, err in failed:
            ap(f"  - {code}: {err}")

    # ⚠️ 全部标的取数失败 → 无有效数据，不得让模型凭空生成报告
    if got == 0:
        return False, (f"{NO_PUSH} 全部标的取数失败（{len(failed)} 只），"
                       f"无有效数据，不生成报告")

    return True, "\n".join(L)


def main():
    should_push, text = build_report()
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
