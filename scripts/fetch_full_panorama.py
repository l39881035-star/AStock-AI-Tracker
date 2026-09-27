# -*- coding: utf-8 -*-
"""修复版 + 扩展版 fetch_full_panorama.py

基于 NAS 生产版本（sha256 f32fd161...）修改，保留其结构与新增函数，仅修正确错误处。

修复清单（对照生产版行号）：
  [B] get_sina_money_flow  —— 改用 netamount（原 r0+r1 漏 r2/r3，实测 8 票 7 票有偏），
                              失败返回 None（原返回字符串 "暂无数据"/"抓取异常"）
  [C] last_closed_trading_day —— 改由 trading_calendar 模块按官方月历判定
                              （原仅按 weekday 回退，遇节假日必错）
  [D] K线日期断言 —— 由「加字符串提示照样放行」改为「返回 pass:False 阻断」
  [F] 单位统一 —— main_net_inflow 返回「元」，由展示层决定怎么显示

扩展清单（2026-09-27，用户批准）：
  [J] K线根数 60 → 260（支撑 MA/250日区间；腾讯接口上限内）
  [K] 技术指标 —— MACD / KDJ / RSI / BOLL / BIAS / WR，全部本地纯计算
  [L] 60日 / 250日 区间高低（不含当日，避免「当日自己就是最高」的假象）
  [M] 资金流 5/10/20 日累积（净额口径，实测 num=20 一次可拉 20 个交易日）
  [N] 方向判定 —— 同向/反向/背离由程序算，禁止模型自行比较
      （实测教训：模型会把「与5日反向」写成「同向」，比较类结论一律程序产出）

⚠️ 数据源分层（本程序只依赖这两个，均实测可用）：
    价格/K线：腾讯   qt.gtimg.cn + web.ifzq.gtimg.cn
    资金流  ：新浪   vip.stock.finance.sina.com.cn MoneyFlow.ssl_qsfx_lscjfb
    东财/巨潮：本项目【不使用】（NAS 上不可达，2026-09-27 确认）
"""
import urllib.request
import urllib.parse
import json
import math
import sys
import datetime
import re

import trading_calendar as tc

MISSING = []


# ============================================================ 基础工具

def _num(vals, idx, name, default=None):
    """带告警的字段转换：失败不抛异常，只记录缺失"""
    try:
        s = vals[idx]
        return float(s) if s not in ('', '-', '--', 'None') else default
    except (ValueError, IndexError):
        MISSING.append(f"{name}(idx={idx})")
        return default


def _f(x, nd=2):
    """格式化：None → '-'（缺失与 0 必须可区分）"""
    return '-' if x is None else f"{x:.{nd}f}"


def _pe(v):
    """PE(TTM) 显示：None → '-'（缺失）；负数 → '亏损'；正数 → 两位小数。

    ⭐ 2026-09-27 口径已实测定案：腾讯 v[39] = PE(TTM) = 最新总市值 ÷ TTM 归母净利。
       亏损股腾讯返回负值（如新钢股份 -12.41），此时 PE 无「贵/便宜」含义，
       统一显示「亏损」，绝不显示负的 PE 数值（会被误读）。
       v 为 None（停牌/缺失）显示 '-'，绝不填 0（R5）。

    ⚠️ 本函数原先住在 post_market_panorama.py。
       2026-09-27 双轨改造时上移到本模块 —— 因为 morning_runner 与
       afternoon_runner 都要用它，放展示层会导致两个新脚本反向依赖旧入口。
       旧文件保留同名函数定义处的 import 兼容（见 post_market_panorama.py）。
    """
    if v is None:
        return '-'
    if v < 0:
        return '亏损'
    return f"{v:.2f}"


# ============================================================ 技术指标（纯本地计算）
#
# 硬边界：全部由程序算。模型只允许照抄，不得自行比较大小。
# 与旧程序 daily.py 的口径保持一致，便于两套报告互相印证。

def ma(v, k):
    """简单移动平均（取最后 k 个）"""
    return sum(v[-k:]) / k if len(v) >= k else None


def ema_series(v, k):
    """指数移动平均（全序列）"""
    a = 2.0 / (k + 1)
    e = v[0]
    r = [e]
    for x in v[1:]:
        e = x * a + e * (1 - a)
        r.append(e)
    return r


def macd(v):
    """返回 (DIF, DEA, MACD柱)。柱 = (DIF-DEA)*2"""
    if len(v) < 35:
        return None, None, None
    ef, es = ema_series(v, 12), ema_series(v, 26)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema_series(dif, 9)
    return dif[-1], dea[-1], (dif[-1] - dea[-1]) * 2


def kdj(h, l, c, k=9):
    """返回 (K, D, J)。J = 3K-2D"""
    if len(c) < k:
        return None, None, None
    K = D = 50.0
    for i in range(len(c)):
        lo = min(l[max(0, i - k + 1):i + 1])
        hi = max(h[max(0, i - k + 1):i + 1])
        K = (2 * K + (50.0 if hi == lo else (c[i] - lo) / (hi - lo) * 100.0)) / 3.0
        D = (2 * D + K) / 3.0
    return K, D, 3 * K - 2 * D


def rsi(c, k=6):
    """标准 RSI（最近 k 根的平均涨跌幅）"""
    if len(c) < k + 1:
        return None
    g = ls = 0.0
    for i in range(len(c) - k, len(c)):
        ch = c[i] - c[i - 1]
        if ch > 0:
            g += ch
        else:
            ls -= ch
    return 100.0 if ls == 0 else 100.0 - 100.0 / (1.0 + (g / k) / (ls / k))


def boll(c, k=20, m=2):
    """返回 (上轨, 中轨, 下轨)"""
    mm = ma(c, k)
    if mm is None:
        return None, None, None
    sd = math.sqrt(sum((x - mm) ** 2 for x in c[-k:]) / k)
    return mm + m * sd, mm, mm - m * sd


def bias(c, k):
    """乖离率 %"""
    mm = ma(c, k)
    return None if not mm else (c[-1] / mm - 1) * 100.0


def wr(h, l, c, k=14):
    """威廉指标 %"""
    if len(c) < k:
        return None
    hi, lo = max(h[-k:]), min(l[-k:])
    return None if hi == lo else (hi - c[-1]) / (hi - lo) * 100.0


def _sub_dir(a, b):
    """单个周期相对基准：同向 / 反向 / 持平（程序判定）"""
    if a is None or b is None or a == 0 or b == 0:
        return "持平"
    return "同向" if (a > 0) == (b > 0) else "反向"


def flow_dir(today, d5, d10, d20=None):
    """三档（或四档）资金的方向关系 —— 由程序判定，禁止模型自行比较。

    实测教训（daily.py 注释同源）：模型会把「近5日与当日反向」写成「与当日同向」，
    同向/反向也是比较，必须程序算好。

    ⚠️ 三档全为 0（含 None＝无数据）时，绝不能报「三档同向」——
        「都没方向」与「方向一致」是两回事，后者会被模型当作持续性证据。
    """
    def sg(x):
        if x is None or x == 0:
            return 0
        return 1 if x > 0 else -1

    a, b, c = sg(today), sg(d5), sg(d10)
    if a == 0 and b == 0 and c == 0:
        return "三档均无方向（数据缺失或全为 0）"

    if d20 is not None:
        d = sg(d20)
        if a == b == c == d and a != 0:
            return "四档同向"
        if a == b == c and a != 0:
            return "今日/5日/10日同向、与20日反向"
        if a == d and a != 0:
            return "今日与20日同向、5日10日不同向"
        return "多档方向背离"

    if a == b == c and a != 0:
        return "三档同向"
    if a == b and a != 0:
        return "今日与5日同向、与10日反向"
    if a == c and a != 0:
        return "今日与10日同向、与5日反向"
    if b == c and b != 0:
        return "5日与10日同向、与今日反向"
    return "三档互不同向"


def flow_tag(today, d5, d10):
    """短标签（≤5字）—— 直接回答「有没有持续性」

    ⚠️ 与 flow_dir 同理：全 0/无数据不得回「三档流入/流出」。
    """
    def sg(x):
        if x is None or x == 0:
            return 0
        return 1 if x > 0 else -1

    a, b, c = sg(today), sg(d5), sg(d10)
    if a != 0 and a == b == c:
        return "三档流入" if a > 0 else "三档流出"
    if a == 0 and b == 0 and c == 0:
        return "无数据"
    return "背离"


# ============================================================ 公告（巨潮全文检索 · 新）
#
# ⭐ 2026-09-27 起：改用巨潮【全文检索】接口，替代原来拼 orgId 的 hisAnnouncement/query。
#
#    为什么要换（旧接口的三个毛病）：
#      1. 旧接口要自己拼 `stock=603667,gssh0603667` 这种 orgId 形式的标识 ——
#         这个 `gssh0/gssz0` 前缀的拼法在深市/北交所/新代码段上并不稳定，
#         一旦拼错接口会静默返回空列表，表现为「今日盘后暂无重大公告」（假的安静）。
#      2. 旧接口靠 `column=szse` + stock 参数定位，属于「定向查询」；
#         新接口是「全文检索 + 股票简称」，走的是巨潮自己的搜索引擎，不吃拼串。
#      3. 新接口返回字段更全（announcementTime / secCode / announcementId），
#         可以自己做时间窗过滤与真实性校验。
#
#    ✅ NAS 实测（2026-09-27，victoryno1 身份，NAS 直连）：
#         五洲新春 totalRecordNum=1678、科华数据=2263，均正常返回 announcements。
#
#    🔴 关键设计约束：**本接口不支持按「昨晚到今早」的时间范围过滤** ——
#       `sdate` / `edate` 传空时返回该股票的全部历史公告（按 pubdate 降序）。
#       所以「盘前只看昨晚到今早的新公告」这件事，**必须由本函数自己按
#       announcementTime 过滤**，不能指望接口参数。见 `since_ts` 参数。

def fetch_juchao_announcement_by_search(stock_name, limit=3, since_ts=None):
    """使用巨潮全文检索接口抓取最新官方公告。

    接口地址：http://www.cninfo.com.cn/new/fulltextSearch/full
    POST 参数（URL 编码）：
        searchkey=股票汉字简称&sdate=&edate=&isfulltext=false
        &sortName=pubdate&sortType=desc&pageNum=1

    参数：
      stock_name : 股票汉字简称（如「五洲新春」）。⚠️ 传名称而非代码 ——
                   该接口的 searchkey 是全文检索关键词，传代码会命中无关公告。
      limit      : 返回条数上限（接口固定返回 10 条/页，本函数只截前 limit 条）。
      since_ts   : 只保留 `announcementTime >= since_ts` 的公告，单位【毫秒】时间戳。
                   None = 不过滤（返回最新 limit 条）。这是实现「昨晚到今早」窗口的正确方式。

    返回：
      成功 → 纯净标题列表（已剥掉 <em> 高亮标签）。
      失败 → 单元素列表，元素形如 "公告检索失败: URLError: ..."（**带具体异常名**，
             便于上层区分「网络不通」与「接口结构变了」，绝不伪造空列表冒充「无公告」）。

    设计红线（与全项目一致）：
      · 绝不用空列表冒充「确实没有公告」——「取不到」与「没有」必须可区分。
      · 纯标准库（urllib.request / json），NAS /usr/bin/python3 3.11.2 可直接跑。
    """
    url = "http://www.cninfo.com.cn/new/fulltextSearch/full"

    # 注意：参数顺序与官方一致；sdate/edate 留空表示不限日期（时间窗由 since_ts 兜底）
    body = urllib.parse.urlencode({
        'searchkey': stock_name,
        'sdate': '',
        'edate': '',
        'isfulltext': 'false',
        'sortName': 'pubdate',
        'sortType': 'desc',
        'pageNum': '1',
    }).encode('utf-8')

    try:
        req = urllib.request.Request(url, data=body, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Referer': 'http://www.cninfo.com.cn/',
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode('utf-8', errors='replace')

        res = json.loads(raw)
        items = res.get('announcements') or []

        if not items:
            # 走到这里有两种可能，必须分开说明，不能含糊：
            #   a) 接口真的答了 0 条（totalRecordNum == 0）
            #   b) 接口结构变了（拿不到 announcements 键）—— 这是故障，不是「没公告」
            total = res.get('totalRecordNum')
            if total == 0:
                return ["该关键词下无公告记录（巨潮接口正常应答 0 条）"]
            return [f"公告检索返回空(字段: {list(res.keys())[:6]})，疑似接口结构变更"]

        titles = []
        for it in items:
            t = it.get('announcementTitle') or ''
            # 剥掉 <em> / </em> 高亮标签（旧接口没有，新接口全文检索必有）
            t = t.replace('<em>', '').replace('</em>', '').strip()
            if not t:
                continue
            # 去重前缀：巨潮标题常为「五洲新春：五洲新春关于…」（简称 + 冒号 + 全称开头）
            # 循环剥离，因为可能叠好几层，只留真正的事件描述
            for _ in range(3):
                changed = False
                if t.startswith(stock_name + '：'):
                    t = t[len(stock_name) + 1:].strip()
                    changed = True
                if t.startswith(stock_name + ':'):
                    t = t[len(stock_name) + 1:].strip()
                    changed = True
                if t.startswith(stock_name) and len(t) > len(stock_name):
                    t = t[len(stock_name):].lstrip('：: ').strip()
                    changed = True
                if not changed:
                    break
            if not t:
                continue

            # ---- 时间窗过滤（实现「昨晚到今早」）----
            if since_ts is not None:
                ts = it.get('announcementTime')
                if isinstance(ts, (int, float)) and ts < since_ts:
                    continue     # 早于窗口的公告直接丢弃

            titles.append(t)
            if len(titles) >= limit:
                break

        if titles:
            return titles
        # 有公告但全被时间窗过滤掉 —— 这是正常情况，明确说出来
        return ["窗口内无新增公告（巨潮接口正常）"]

    except Exception as e:
        # ⚠️ 必须带异常名：上层据此区分「网络不可达」与「代码 bug」，不许吞掉
        return [f"公告检索失败: {type(e).__name__}: {str(e)[:100]}"]


# ============================================================ 公告（旧接口，保留不删）
#
# ⚠️ 2026-09-27：已被上面的全文检索版取代（蓝绿部署，旧入口不删以便回滚）。
#    旧的 post_market_panorama.py 仍 import 本函数，删掉会让现网作业崩 —— 故保留。
#    新脚本（morning_runner / afternoon_runner）一律用 by_search 版。

def fetch_juchao_announcement(stock_code, stock_name, limit=3):
    """巨潮资讯：定向抓取个股盘后公告。失败返回 [失败提示]，调用方需自行判断。

    ⚠️ 未实测：本函数在 NAS 上的真实返回尚未验证。
       若持续取不到，展示层会原样打印返回内容，不会伪造。
    """
    try:
        pure_code = stock_code[2:] if stock_code.startswith(('sz', 'sh')) else stock_code
        url = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
        stock_param = f"{pure_code},gssz0{pure_code}" if stock_code.startswith('sz') else f"{pure_code},gssh0{pure_code}"
        data = f"pageNum=1&pageSize={limit}&column=szse&tabName=fulltext&plate=&stock={stock_param}&searchkey=&secid=&category=&trade=&seDate=&sortName=&sortType=&isHLtitle=true"

        req = urllib.request.Request(url, data=data.encode('utf-8'), headers={
            'User-Agent': 'Mozilla/5.0',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
        })
        with urllib.request.urlopen(req, timeout=5) as r:
            res = json.loads(r.read().decode('utf-8'))

        items = res.get('announcements') or []
        news_list = []
        for i in items:
            title = i.get('announcementTitle', '').replace('<em>', '').replace('</em>', '')
            if title:
                news_list.append(title)
        if news_list:
            return news_list
        # ⚠️ 关键区分：接口「真的答了 0 条」与「接口结构不对」是两回事。
        #    但本接口在 NAS 上的可用性【从未验证】⇒ 即使拿到 totalRecordNum==0，
        #    也不能断言「确实没有公告」，必须带上「未验证」标记，避免误导读者。
        total = res.get('totalRecordNum')
        if total == 0:
            return ["本窗口内无公告记录（注：巨潮接口可用性未验证，此项仅供参考）"]
        return [f"公告接口返回空(结构: {list(res.keys())[:6]})"]
    except Exception as e:
        return [f"公告抓取不可用: {type(e).__name__}: {str(e)[:80]}"]


# ============================================================ 资金流（新浪）

def get_sina_money_flow(stock_code, days=20):
    """新浪 MoneyFlow —— 主力净额（多日）。

    返回 dict:
      {'value': float|None,      # 当日净额，单位：元
       'date':  str|None,        # 当日对应交易日
       'series': [{'date','value'}],   # 近 N 日（降序，最新在前）
       'acc5': float|None, 'acc10': float|None, 'acc20': float|None,
       'dir5': str|None, 'dir10': str|None, 'dir20': str|None,
       'source': 'sina_moneyflow',
       'error': str              # 仅失败时存在
      }

    ⭐ 取值口径：用 netamount（= r0+r1+r2+r3 四档之和，已实测 20/20 行精确成立）。
       生产版原用 r0_net+r1_net，漏掉中单(r2)与小单(r3)，实测 8 只样本 7 只有偏差，
       最大偏差 0.21 亿元（紫金矿业）。netamount 是接口在服务端算好的全档净额。

    ⭐ 多日累积：实测 num=20 一次返回 20 个交易日（降序），四档字段齐全，
       故 5/10/20 日累积无需多次请求。⚠️ 切片必须用「降序前 N 个」（d[:5]），
       不是升序的 d[-5:]。
    """
    url = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
           f"/MoneyFlow.ssl_qsfx_lscjfb?page=1&num={days}&sort=opendate&asc=0&daima={stock_code}")
    out = {'value': None, 'date': None, 'series': [],
           'acc5': None, 'acc10': None, 'acc20': None,
           'dir5': None, 'dir10': None, 'dir20': None,
           'source': 'sina_moneyflow'}
    try:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://finance.sina.com.cn/',
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode('utf-8')
        if not raw or raw.strip() in ('null', ''):
            out['error'] = '接口返回空'
            return out
        d = json.loads(raw)
        if not d:
            out['error'] = '接口返回空数组'
            return out

        # 接口恒为降序（最新在前）。显式排序一次，避免依赖服务端顺序。
        rows = sorted(d, key=lambda x: str(x.get('opendate') or ''), reverse=True)

        series = []
        for row in rows:
            na = row.get('netamount')
            if na is None or na == '':          # 单行缺失：记录但不污染整条序列
                MISSING.append(f"netamount({row.get('opendate')})")
                continue
            series.append({'date': row.get('opendate'), 'value': float(na)})

        if not series:
            out['error'] = 'netamount 字段全缺失'
            return out

        out['series'] = series
        out['value'] = series[0]['value']
        out['date'] = series[0]['date']

        # ---- 累积（降序切片：最新 N 个交易日）----
        def _acc(n):
            if len(series) < n:
                return None                      # 数据不足 → None，不拿短窗口冒充
            return sum(x['value'] for x in series[:n])

        out['acc5'] = _acc(5)
        out['acc10'] = _acc(10)
        out['acc20'] = _acc(20)

        # ---- 方向（程序判定，禁止模型比较）----
        if out['value'] is not None and out['acc5'] is not None:
            out['dir5'] = _sub_dir(out['value'], out['acc5'])
        if out['value'] is not None and out['acc10'] is not None:
            out['dir10'] = _sub_dir(out['value'], out['acc10'])
        if out['value'] is not None and out['acc20'] is not None:
            out['dir20'] = _sub_dir(out['value'], out['acc20'])

        # ---- 三档/四档综合判定 ----
        if out['acc5'] is not None and out['acc10'] is not None:
            out['flow_dir'] = flow_dir(out['value'], out['acc5'], out['acc10'], out.get('acc20'))
            out['flow_tag'] = flow_tag(out['value'], out['acc5'], out['acc10'])

        # 附带上四档明细（当日），便于人工核对（不参与计算）
        out['detail'] = {
            'r0_net': rows[0].get('r0_net'),
            'r1_net': rows[0].get('r1_net'),
            'r2_net': rows[0].get('r2_net'),
            'r3_net': rows[0].get('r3_net'),
        }
        return out
    except Exception as e:
        out['error'] = f"{type(e).__name__}: {str(e)[:80]}"
        return out


# ============================================================ 交易日

def last_closed_trading_day(now=None):
    """最近一个已收盘交易日。委托 trading_calendar（官方月历）。
    日历不可用或超出覆盖范围时返回 None。"""
    return tc.last_closed_trading_day(now)


# ============================================================ 主取数

KLINE_BARS = 260          # [J] 260 根：支撑 MA250 / 250 日区间


def get_tencent_panorama(stock_id, now=None):
    """主数据源：腾讯量价+K线 + 新浪资金流。

    ⚠️ now 必须由调用方传入（build_report 自己的 now），禁止本函数内部再取
       datetime.now() —— 否则「报告首行的数据截止日」与「K线新鲜度断言的期望日」
       会来自两个时间源，测试时无法注入、生产时也埋着不一致的隐患。
    """
    if now is None:
        now = datetime.datetime.now()
    try:
        url_rt = f"http://qt.gtimg.cn/q={stock_id}"
        req = urllib.request.Request(url_rt, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as r:
            rt_raw = r.read().decode('gbk', errors='ignore')
        vals = rt_raw.split('=')[1].strip('"').split('~')

        name = vals[1]
        price = _num(vals, 3, 'price')
        if not price:
            return {"pass": False, "code": stock_id, "error": "停牌或无数据"}

        open_p = _num(vals, 5, 'open')
        high_p = _num(vals, 33, 'high')
        low_p = _num(vals, 34, 'low')
        turnover = _num(vals, 38, 'turnover')
        # 腾讯自带权威量比 v[49]
        vr = _num(vals, 49, 'vol_ratio')
        pe_ttm = _num(vals, 39, 'pe_ttm')

        # 新浪主力净额（dict，含多日序列）
        mf = get_sina_money_flow(stock_id)

        # [J] 前复权日 K 线 260 根
        url_k = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                 f"?param={stock_id},day,,,{KLINE_BARS},qfq")
        req_k = urllib.request.Request(url_k, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req_k, timeout=8) as r:
            k_raw = json.loads(r.read().decode('utf-8'))

        # 腾讯偶发把数据放在 data[stock_id]['qfqday']，偶发放在顶层 ['day']；
        # 个别情况返回 data[stock_id] 直接是列表 —— 三种都兼容
        node = k_raw.get('data', {}).get(stock_id)
        if isinstance(node, list):
            klines = node
        elif isinstance(node, dict):
            klines = node.get('qfqday') or node.get('day')
        else:
            klines = None
        if not klines:
            return {"pass": False, "code": stock_id, "error": "K线为空"}

        last_date = klines[-1][0]

        # [D] K线末根日期断言 —— 阻断式，不再放行
        expect = last_closed_trading_day(now)
        if expect is None:
            return {"pass": False, "code": stock_id,
                    "error": "交易日历不可用（trading_days.json 缺失或超范围）"}
        if last_date != expect:
            # 真阻断：数据源返回旧窗口时绝不能照常出报告
            return {"pass": False, "code": stock_id,
                    "error": f"K线末根 {last_date} ≠ 最近已收盘交易日 {expect}（数据源可能返回旧窗口）"}

        # 资金流日期一致性（两源必须同一交易日）
        if mf.get('date') and mf['date'] != last_date:
            return {"pass": False, "code": stock_id,
                    "error": f"数据源日期不一致: K线={last_date} 资金流={mf['date']}"}

        closes = [float(x[2]) for x in klines]
        highs = [float(x[3]) for x in klines]
        lows = [float(x[4]) for x in klines]
        vols = [float(x[5]) for x in klines] if len(klines[0]) > 5 else []

        ma5 = ma(closes, 5)
        ma10 = ma(closes, 10)
        ma20 = ma(closes, 20)
        ma30 = ma(closes, 30)
        ma60 = ma(closes, 60)
        ma120 = ma(closes, 120)
        ma250 = ma(closes, 250)

        # 20日最高最低，不含当日
        res = round(max(highs[-21:-1]), 2) if len(highs) > 20 else round(max(highs), 2)
        sup = round(min(lows[-21:-1]), 2) if len(lows) > 20 else round(min(lows), 2)

        # [L] 60日 / 250日 区间高低（不含当日）
        def _rng(arr, n):
            if len(arr) > n:
                w = arr[-(n + 1):-1]
            else:
                w = arr
            return round(max(w), 2), round(min(w), 2)

        h60, l60 = _rng(highs, 60)
        h250, l250 = _rng(highs, 250)
        _, l60b = _rng(lows, 60)
        _, l250b = _rng(lows, 250)
        low60 = round(min(l60b, l60), 2)
        low250 = round(min(l250b, l250), 2)

        # [K] 技术指标
        dif, dea, macd_bar = macd(closes)
        k_val, d_val, j_val = kdj(highs, lows, closes)
        bu, bm, bl = boll(closes, 20, 2)
        r6, r12, r24 = rsi(closes, 6), rsi(closes, 12), rsi(closes, 24)
        b6, b12, b24 = bias(closes, 6), bias(closes, 12), bias(closes, 24)
        w14 = wr(highs, lows, closes, 14)

        # 多周期涨跌
        def _chg(w):
            return (closes[-1] / closes[-1 - w] - 1) * 100 if len(closes) > w else None

        # 量能：20 日均量倍数（不含当日）
        avg20 = sum(vols[-21:-1]) / 20.0 if len(vols) > 21 else None

        return {
            "pass": True,
            "code": stock_id,
            "name": name,
            "date": last_date,
            "price": price,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "ma5": round(ma5, 2) if ma5 else None,
            "ma10": round(ma10, 2) if ma10 else None,
            "ma20": round(ma20, 2) if ma20 else None,
            "ma30": round(ma30, 2) if ma30 else None,
            "ma60": round(ma60, 2) if ma60 else None,
            "ma120": round(ma120, 2) if ma120 else None,
            "ma250": round(ma250, 2) if ma250 else None,
            "vol_ratio": vr,
            "turnover": turnover,
            "pe_ttm": pe_ttm,
            "main_net_inflow": mf,      # dict（value 单位：元）
            "resistance": res,
            "support": sup,
            # ---- [L] 区间 ----
            "high60": h60, "low60": low60,
            "high250": h250, "low250": low250,
            # ---- [K] 指标 ----
            "macd_dif": dif, "macd_dea": dea, "macd_bar": macd_bar,
            "kdj_k": k_val, "kdj_d": d_val, "kdj_j": j_val,
            "boll_up": bu, "boll_mid": bm, "boll_low": bl,
            "rsi6": r6, "rsi12": r12, "rsi24": r24,
            "bias6": b6, "bias12": b12, "bias24": b24,
            "wr14": w14,
            "chg5": _chg(5), "chg10": _chg(10), "chg20": _chg(20), "chg60": _chg(60),
            "vol_avg20": avg20,
            "vol_today": vols[-1] if vols else None,
            "bars": len(klines),
        }
    except Exception as e:
        return {"pass": False, "code": stock_id, "error": str(e)}


if __name__ == '__main__':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    codes = sys.argv[1:] or ["sh603667", "sz002335"]
    print("日历:", "OK" if tc.calendar_ok() else "FAIL " + str(tc.calendar_error()))
    print("最近已收盘交易日:", last_closed_trading_day())
    print()
    _now = datetime.datetime.now()
    for c in codes:
        r = get_tencent_panorama(c, _now)
        if not r['pass']:
            print(f"  [{c}] 失败: {r['error']}")
            continue
        mf = r['main_net_inflow']
        v = mf.get('value')
        print(f"  [{c}] {r['name']}  日期={r['date']}  价={r['price']}  K线={r['bars']}根")
        _pe_disp = ('-' if r['pe_ttm'] is None
                    else ('亏损' if r['pe_ttm'] < 0 else f"{r['pe_ttm']:.2f}"))
        print(f"        量比={r['vol_ratio']}  换手={r['turnover']}%  PE(TTM)={_pe_disp}")
        if v is None:
            print(f"        资金流: 未取到（{mf.get('error')}）")
        else:
            print(f"        资金流: 当日 {v/1e8:+.3f}亿  5日 {mf['acc5']/1e8:+.3f}亿  "
                  f"10日 {mf['acc10']/1e8:+.3f}亿  20日 {mf['acc20']/1e8:+.3f}亿")
            print(f"        方向: {mf.get('flow_dir')} / 标签={mf.get('flow_tag')}")
            print(f"        序列({len(mf['series'])}日): {[x['date'] for x in mf['series'][:5]]} ...")
        print(f"        支撑={r['support']}  压力={r['resistance']}")
        print(f"        均线 MA5={r['ma5']} MA20={r['ma20']} MA60={r['ma60']} MA250={r['ma250']}")
        print(f"        MACD DIF={_f(r['macd_dif'],3)} DEA={_f(r['macd_dea'],3)} 柱={_f(r['macd_bar'],3)}")
        print(f"        KDJ K={_f(r['kdj_k'],1)} D={_f(r['kdj_d'],1)} J={_f(r['kdj_j'],1)}")
        print(f"        RSI6={_f(r['rsi6'],1)} RSI12={_f(r['rsi12'],1)} RSI24={_f(r['rsi24'],1)}")
        print(f"        BOLL 上={_f(r['boll_up'])} 中={_f(r['boll_mid'])} 下={_f(r['boll_low'])}")
        print(f"        BIAS6={_f(r['bias6'],1)}% BIAS12={_f(r['bias12'],1)}% WR14={_f(r['wr14'],1)}")
        print(f"        区间 60日 高{r['high60']}/低{r['low60']}  250日 高{r['high250']}/低{r['low250']}")
        print(f"        多周期 5日={_f(r['chg5'],2)}% 10日={_f(r['chg10'],2)}% 20日={_f(r['chg20'],2)}% 60日={_f(r['chg60'],2)}%")
        if MISSING:
            print(f"        ⚠️ 缺失字段: {MISSING}")
        print()
