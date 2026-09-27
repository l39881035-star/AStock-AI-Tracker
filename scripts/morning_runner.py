# -*- coding: utf-8 -*-
"""早盘 If-Then 剧本生成器（08:45 触发）—— 双轨闭环「上半轨」· v3 模板直出

═══════════════════════════════════════════════════════════════════════════
v3 核心变更：彻底剥离 LLM，改走 Hermes `--no-agent` 模式
═══════════════════════════════════════════════════════════════════════════
v2 是「脚本出数据块 + 提示词 → 大模型写剧本 → 推送」。三个致命问题：
  ① LLM 挂在关键路径上 —— 一旦 429/拥塞，**推送直接消失**（实测 Vertex DSQ
     今日已 429 三十余次；兜底模型 Qwen3.8-27B 单轮 22~110 秒，且它自己也被限流）
  ② 模型自由发挥 —— 旧作业 09-26 产出过「做反 T 800 股 / 低吸 800 股」，
     直接触犯「不得给出加减仓建议」的红线；提示词约束不住模型
  ③ 白烧 token

v3：**脚本 stdout 原文 = 微信推送正文**，全链路 0 次 LLM 调用
    ⇒ 0 个 429 机会、100% 送达、措辞与数字 100% 可控。

═══════════════════════════════════════════════════════════════════════════
`--no-agent` 模式的语义（源码级，cron/scheduler.py:4270-4332）
═══════════════════════════════════════════════════════════════════════════
源码注释原文：
    - script stdout (trimmed) → delivered verbatim as the final message
    - empty stdout            → silent run (no delivery, success=True)
    - non-zero exit / timeout → delivered as an error alert, success=False
    - wakeAgent=false gate    → treated like empty stdout (silent)

实现：`if not _parse_wake_gate(output): return True, silent_doc, SILENT_MARKER, None`
⇒ ① 非交易日照样静默，0 投递（wake gate 在 no_agent 路径同样生效）
   ② ⚠️ **非零退出码会被当成错误告警投递** ⇒ main() 必须恒返回 0
   ③ .md 审计文件里 `---` 之后就是 stdout 原文（`doc = ... f"{output}"`）

═══════════════════════════════════════════════════════════════════════════
盘后怎么拿到今早的剧本（v3 改了机制）
═══════════════════════════════════════════════════════════════════════════
`--no-agent` 没有 prompt，所以 Hermes 的 `context_from` 注入机制失效
（它注入的是 prompt）。v3 改为**结构化落盘**：

  本脚本把【条件清单】写到  <ABS_SCRIPT_DIR>/state/plan_<交易日期>.json
  盘后脚本读它，用当日 OHLC 逐条程序化判定「命中 / 未触发」。

⚠️ 为什么不用「读早盘那篇散文再做文本匹配」：
   散文匹配会失真（同一句话换个措辞就匹配不上），那样的对账是假的，
   比不做对账更坏 —— 它会制造虚假的纪律感。只有结构化条件才能让对账成立。

═══════════════════════════════════════════════════════════════════════════
架构红线（100% 遵守）
═══════════════════════════════════════════════════════════════════════════
R1 绝对路径 ｜ R2 纯标准库（/usr/bin/python3 = 3.11.2）｜ R3 算力隔离
R4 节假日静默 ｜ R5 缺失值一律 '-' 或文字，绝不填 0

R4 补充：为什么不用 `__NO_PUSH__`（源码级证据）
───────────────────────────────────────────────────────────────────────────
全站 grep `__NO_PUSH__` / `NO_PUSH` / `no_push` 在 Hermes 源码
（/vol1/@appcenter/trim.hermes/runtime/python/lib/python3.11/site-packages/）
中【零命中】。那是旧 OpenClaw 时代的协议，Hermes 上根本不存在，
既不生效又白烧 token。Hermes 真正的静默通道是 wake gate
（`_parse_wake_gate`，scheduler.py:3418-3440；调用点 :4536 agent / :4353 no_agent）。

═══════════════════════════════════════════════════════════════════════════
送达率优先：什么时候「不推」、什么时候「推告警」
═══════════════════════════════════════════════════════════════════════════
· 确认非交易日（周末/节假日）        → 静默（R4，wake gate）
· 交易日 + 任何取数/配置故障         → **推一条简短告警**
  （理由：静默失败是这类定时任务最坏的结局。用户宁可收到「今天没剧本，
    因为数据源挂了」，也不要收到一条空白。）
⚠️ 告警文案明确写「这不是行情判断，是链路故障」，避免被误读为盘面信息。
"""

import json
import os
import sys
import datetime

# ---------------------------------------------------------------------------
# R1 绝对路径：cron 唤醒时 CWD 不可控，全部用绝对路径
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
# Hermes 原生 wake gate：stdout 最后一行若为它 → agent 不启动 / no_agent 视为静默
WAKE_GATE_OFF = '{"wakeAgent": false}'

# 公告时间窗：往回 20 小时（覆盖「昨晚 15:00 → 今早 08:45」，留冗余）
ANNOUNCE_LOOKBACK_HOURS = 20

_WEEK = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')


# ============================================================ 工具

def _load_config():
    """读配置。失败抛异常 —— 上层转成告警，不拿空配置静默跑。"""
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def _yi(v):
    """元 → 亿元字符串（带符号）；None → '未取到'（R5）"""
    return "未取到" if v is None else "%+.3f亿" % (v / 1e8)


def _ma_pos(price, pairs):
    """均线相对现价 —— 程序判定「上方/下方」，渲染层只读结论（R3）

    返回 (压力列表, 支撑列表, 位置整句)
    """
    above, below = [], []
    for nm, v in pairs:
        if v is None:
            continue
        (above if v > price else below).append(nm)
    # 分句必须自带主语：只出现「在 X 下方」会变成一个没有主语的残句
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
    """超买/超卖状态 —— 程序判定，渲染层只读（R3）"""
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
    """量能描述 —— 用固定阈值程序判定（R3），不留给模型措辞"""
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


# ============================================================ 公告（带时间窗）

def _collect_announcements(name, since_ts, limit=3):
    """取某只票「昨晚到今早」的新公告。

    ⭐ 时间窗自己做：巨潮全文检索接口 sdate/edate 传空 = 返回全部历史，
       不支持「最近 N 小时」。故按 announcementTime（毫秒）在 Python 侧过滤。

    返回 (标题列表, 是否取数失败)
      · 取数失败 → ([失败说明], True)；上层明确标注，**不伪装成「无公告」**
    """
    res = fetch_juchao_announcement_by_search(name, limit=limit, since_ts=since_ts)
    if len(res) == 1 and ('失败' in res[0] or '疑似' in res[0]):
        return res, True
    # 🔴 单元素「接口健康说明」**不是公告** —— 必须按 0 条返回，
    #    否则渲染层 len() 会把它数成「新增公告 1 条」，而内容又写着「无新增公告」，
    #    同一条推送里数字自相矛盾（2026-09-27 真数据预演暴露，R5 精神：宁可说 0）。
    #    触发场景：①窗口内确有公告但全被 since_ts 过滤掉
    #                → fetch 返回 ["窗口内无新增公告（巨潮接口正常）"]
    #              ②该关键词本无任何公告记录
    #                → fetch 返回 ["该关键词下无公告记录（巨潮接口正常应答 0 条）"]
    if len(res) == 1 and ('无新增公告' in res[0] or '无公告记录' in res[0]):
        return [], False
    return res, False


# ============================================================ 渲染（模板直出）

def _alert_text(title, detail):
    """链路故障告警 —— 明确区别于行情信息，避免被误读"""
    return "\n".join([
        "⚠️ 今日早盘剧本生成失败",
        "",
        "原因：%s" % title,
        "详情：%s" % detail,
        "",
        "说明：这条【不是行情判断】，是复盘链路本身的故障告警。",
        "今天没有盘前剧本；盘后对账会标注「今早剧本缺失」，不会编造今早说过什么。",
    ])


def render_stock(L, data, ann_titles, ann_failed, conds, style):
    """渲染单只标的的【盘前剧本】（纯模板，无 LLM）"""
    ap = L.append

    ap("━━━ %s %s ━━━" % (data['name'], data['code']))
    ap("【昨日结构】昨收 %s 元（开 %s / 高 %s / 低 %s）" % (
        _f(data.get('price')), _f(data.get('open')),
        _f(data.get('high')), _f(data.get('low'))))

    pairs = [("MA5", data.get('ma5')), ("MA10", data.get('ma10')),
             ("MA20", data.get('ma20')), ("MA30", data.get('ma30')),
             ("MA60", data.get('ma60')), ("MA120", data.get('ma120')),
             ("MA250", data.get('ma250'))]
    price = data.get('price')
    _above, _below, pos = _ma_pos(price, pairs) if price is not None else ([], [], "现价未取到")
    ap("  · 位置：%s" % pos)
    ap("  · 量能：%s" % _vol_tag(data))

    # 60/250 日区间位置（程序算好，避免出现「高低点靠猜」）
    h60, l60 = data.get('high60'), data.get('low60')
    if price and h60:
        seg = "距60日高 %+.1f%%" % ((price / h60 - 1) * 100)
        if l60:
            seg += "，距60日低 %+.1f%%" % ((price / l60 - 1) * 100)
        ap("  · 区间：%s" % seg)

    ap("  · 技术：%s" % _ob_os(data))

    mf = data.get('main_net_inflow') or {}
    v = mf.get('value')
    if v is None:
        ap("  · 资金：昨日主力净额未取到（%s）｜ PE(TTM) %s"
           % (mf.get('error', '原因不明'), _pe(data.get('pe_ttm'))))
    else:
        line = "  · 资金：昨日主力 %s（netamount 四档合计，截止 %s）" % (_yi(v), mf.get('date'))
        if mf.get('acc5') is not None:
            line += "｜ 5日 %s ｜ 10日 %s ｜ 20日 %s" % (
                _yi(mf['acc5']), _yi(mf['acc10']), _yi(mf['acc20']))
        ap(line)
        ap("    方向（程序已判定，禁止再改判）：当日vs5日=%s ｜ 当日vs10日=%s ｜ "
           "当日vs20日=%s ｜ 综合=%s ｜ 标签=%s"
           % (mf.get('dir5'), mf.get('dir10'), mf.get('dir20'),
              mf.get('flow_dir'), mf.get('flow_tag')))
        ap("    PE(TTM) %s" % _pe(data.get('pe_ttm')))

    # ---- 消息面（R5：取不到要说清）----
    if ann_failed:
        ap("【消息面】读取失败：%s" % ann_titles[0])
        ap("  ⚠️ 这是取数故障，**不能**据此推断「公司无公告」。")
    else:
        ap("【消息面】窗口内新增公告 %d 条" % len(ann_titles))
        for i, t in enumerate(ann_titles, 1):
            ap("  %d. %s" % (i, t))

    # ---- 今日 If-Then 剧本（条件由 plan_engine 生成，此处只做模板拼装）----
    ap("【今日 If-Then 剧本】（条件 → 状态观察框架，非操作指令）")
    if not conds:
        ap("  · 无可用价位参照（均线与20日高低点均未取到），今日不出条件")
    else:
        ups, downs = pe.price_candidates(data)
        for c in conds:
            ap("  %s %s" % (c['id'], pe.cond_text(c)))
            if c['id'] == 'U1':
                ap("       → 若同时量比≥1.5 视为放量试探；量比<1 按「无量遇阻」看，不做方向判断")
            elif c['id'] == 'U2':
                ap("       → 该位由压力转为支撑，属短期结构改善信号（观察，不作买卖依据）")
            elif c['id'] == 'D1':
                ap("       → 观察是否被承接：若快速收回该位上方，视为支撑有效")
            elif c['id'] == 'D2':
                nxt = downs[1][0] + " " + ("%.2f" % downs[1][1]) if len(downs) > 1 else "下方无近端均线参照"
                ap("       → 短期支撑失效，下一参照：%s" % nxt)
        # 次级参照（只给价位，不派生条件）
        if len(ups) > 1:
            ap("  · 次级压力：%s" % "、".join("%s %.2f" % (n, v) for n, v in ups[1:3]))
        if len(downs) > 1:
            ap("  · 次级支撑：%s" % "、".join("%s %.2f" % (n, v) for n, v in downs[1:3]))

    if style:
        ap("【你的既定纪律（原样引用，不解释）】%s" % style)
    ap("")


def build_plan(now, base_date, items, style):
    """构造落盘的【结构化条件清单】（盘后对账的唯一依据）"""
    stocks = []
    for data, conds in items:
        stocks.append({
            "code": data.get('code'),
            "name": data.get('name'),
            "base_date": data.get('date'),
            "base_price": data.get('price'),
            "conds": conds,
        })
    return {
        "schema": "astock.plan.v1",
        "trade_date": now.strftime('%Y-%m-%d'),
        "base_date": base_date,
        "generated_at": now.strftime('%Y-%m-%d %H:%M:%S'),
        "style": style,
        "stocks": stocks,
    }


def plan_path(trade_date):
    """计划文件的绝对路径 —— 委托 plan_engine，保证早晚两轨用同一份路径规则"""
    return pe.plan_path(trade_date)


# ============================================================ 主流程

def build_report(now=None):
    """构造早盘剧本。

    返回 (should_push: bool, text: str, plan: dict|None)
      · should_push=True  → text 为【推送正文】（no_agent 下 stdout 原文直发），
                            plan 非空表示需要落盘
      · should_push=False → text 为【静默原因】，由 main() 写 stderr + 打 wake gate
    """
    if now is None:
        now = datetime.datetime.now()
    today_str = now.strftime('%Y-%m-%d')

    # ---- R4 交易日闸门（必须最先判：判完之后任何异常都只可能发生在交易日）----
    try:
        istd = tc.is_trading_day(now.date())
    except Exception:
        istd = None

    if istd is None:
        # 日历读不到 → 无法判定。周末按静默（避免噪声），工作日必须告警
        if now.weekday() >= 5:
            return False, "%s 交易日历不可用，但今日为周末，静默" % today_str, None
        return True, _alert_text(
            "交易日历不可用，无法判定今天是否为交易日",
            "日历覆盖范围: %s" % (tc.coverage() if hasattr(tc, 'coverage') else '未知')), None

    if istd is False:
        kind = "周末" if now.weekday() >= 5 else "节假日"
        return False, "%s 非交易日（%s），无早盘" % (today_str, kind), None

    # ================= 以下是交易日：任何故障都要推告警（送达率优先）=================
    try:
        expect = last_closed_trading_day(now)
        if expect is None:
            return True, _alert_text("无法确定「最近已收盘交易日」", "交易日历超出覆盖范围"), None

        try:
            config = _load_config()
        except Exception as e:
            return True, _alert_text(
                "配置文件读取失败", "%s: %s" % (type(e).__name__, str(e)[:120])), None

        stocks = [s for s in config.get('stocks', []) if s.get('enabled', True)]
        if not stocks:
            return True, _alert_text("config.json 中没有启用中的标的", CONFIG_PATH), None

        style = config.get('style', '')

        # 公告时间窗：now - 20h
        since_dt = now - datetime.timedelta(hours=ANNOUNCE_LOOKBACK_HOURS)
        since_ts = int(since_dt.timestamp() * 1000)

        L = []
        L.append("📋 盘前 If-Then 作战剧本 · %s（%s）"
                 % (today_str, _WEEK[now.weekday()]))
        L.append("━" * 34)
        L.append("数据基准：%s 收盘 ｜ 前复权 ｜ 价格源:腾讯 ｜ 资金源:新浪netamount ｜ 公告源:巨潮全文检索" % expect)
        L.append("生成时间：%s（开盘前）" % now.strftime('%Y-%m-%d %H:%M'))
        L.append("公告窗口：%s 之后发布" % since_dt.strftime('%Y-%m-%d %H:%M'))
        L.append("")

        failed = []
        got = 0
        items = []
        for s in stocks:
            data = get_tencent_panorama(s['code'], now)
            if not data.get('pass'):
                failed.append((s['code'], data.get('error', '未知错误')))
                continue
            got += 1
            name = data.get('name') or s.get('name', '')
            ann_titles, ann_failed = _collect_announcements(name, since_ts, limit=3)
            conds = pe.build_conditions(data)
            items.append((data, conds))
            render_stock(L, data, ann_titles, ann_failed, conds, style)

        if failed:
            L.append("【本次未取到的标的】")
            for code, err in failed:
                L.append("  - %s: %s" % (code, err))
            L.append("")

        if got == 0:
            detail = "；".join("%s(%s)" % (c, e) for c, e in failed) or "无标的"
            return True, _alert_text("全部标的取数失败，今日无盘前剧本", detail), None

        L.append("【纪律】以上全部为「条件 → 状态」的观察框架，由程序按数据块价位生成；")
        L.append("       不含任何买入/卖出/加仓/减仓/止损/止盈指令。")
        L.append("       本报告由 Python 模板直出（未经大模型），数字与措辞均程序固定。")

        plan = build_plan(now, expect, items, style)
        return True, "\n".join(L), plan

    except Exception as e:
        return True, _alert_text(
            "生成过程未预期异常", "%s: %s" % (type(e).__name__, str(e)[:160])), None


def main():
    """stdout 协议（Hermes cron --no-agent 依赖）：
      · 正常 → 打印剧本正文（stdout 原文即微信推送内容）
      · 静默 → stderr 写原因，stdout 最后一行打 wakeAgent 门
    退出码恒 0 —— ⚠️ no_agent 模式下非零退出会被当【错误告警】投递给用户。
    """
    try:
        should_push, text, plan = build_report()
    except Exception as e:
        # 连 build_report 都炸了（理论上不会）：静默，绝不把 traceback 推给用户
        sys.stderr.write("[morning_runner] build_report 崩溃: %s: %s\n"
                         % (type(e).__name__, str(e)[:200]))
        print(WAKE_GATE_OFF)
        return 0

    if not should_push:
        sys.stderr.write("[morning_runner] 静默不推送: %s\n" % text)
        print(WAKE_GATE_OFF)
        return 0

    # 先落盘再打印：盘后对账依赖它。落盘失败不影响今天的推送（只是盘后无法逐条对账）
    if plan:
        try:
            p = pe.save_plan(plan)
            sys.stderr.write("[morning_runner] 计划已落盘: %s\n" % p)
        except Exception as e:
            sys.stderr.write("[morning_runner] 计划落盘失败(不影响今日推送): %s: %s\n"
                             % (type(e).__name__, str(e)[:160]))

    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
