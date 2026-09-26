import urllib.request
import json
import sys
import datetime
import re

MISSING = []

def _num(vals, idx, name, default=None):
    """带告警的字段转换：失败不抛异常，只记录缺失"""
    try:
        s = vals[idx]
        return float(s) if s not in ('', '-', '--', 'None') else default
    except (ValueError, IndexError):
        MISSING.append(f"{name}(idx={idx})")
        return default

def fetch_juchao_announcement(stock_code, stock_name, limit=3):
    """巨潮资讯：定向抓取个股盘后公告，避免全球新闻干扰"""
    try:
        # 去掉 sz/sh 前缀
        pure_code = stock_code[2:] if stock_code.startswith(('sz', 'sh')) else stock_code
        url = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
        # 巨潮 stock 参数格式：300308,gssz0300308
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
            news_list.append(title)
        
        return news_list if news_list else ["今日盘后暂无重大公告。"]
    except Exception as e:
        return [f"公告抓取异常: {str(e)}"]

def get_sina_money_flow(stock_code):
    """新浪 MoneyFlow 接口获取真实主力大单净流入 (单位:万元)"""
    try:
        url = f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_lscjfb?page=1&num=1&sort=opendate&asc=0&daima={stock_code}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read().decode('utf-8')
            # 新浪返回的可能是纯数组文本，需严谨解析
            if not raw or raw == 'null': return "暂无数据"
            d = json.loads(raw)
            if not d: return "暂无数据"
            row = d[0]
            # 大单+特大单净流入 (r0_net + r1_net)，新浪单位是元，需转万
            r0_net = float(row.get('r0_net', 0))
            r1_net = float(row.get('r1_net', 0))
            main_net = (r0_net + r1_net) / 10000.0
            return round(main_net, 2)
    except Exception as e:
        return "抓取异常"

def last_closed_trading_day(now):
    """粗略获取上一个已收盘交易日 (仅用于断言告警)"""
    # 假设 15:00 后为当天收盘，否则取昨日。遇到周末往前推。
    target = now if now.hour >= 15 else now - datetime.timedelta(days=1)
    while target.weekday() > 4: # 5=Sat, 6=Sun
        target -= datetime.timedelta(days=1)
    return target.strftime('%Y-%m-%d')

def get_tencent_panorama(stock_id):
    """主数据源：腾讯量价。修正了量比和主力资金流向"""
    try:
        url_rt = f"http://qt.gtimg.cn/q={stock_id}"
        req = urllib.request.Request(url_rt, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as r:
            rt_raw = r.read().decode('gbk', errors='ignore')
        vals = rt_raw.split('=')[1].strip('"').split('~')
        
        name = vals[1]
        price = _num(vals, 3, 'price')
        if not price: return {"pass": False, "code": stock_id, "error": "停牌或无数据"}
        
        open_p = _num(vals, 5, 'open')
        high_p = _num(vals, 33, 'high')
        low_p = _num(vals, 34, 'low')
        turnover = _num(vals, 38, 'turnover')
        # 腾讯自带权威量比 v[49]
        vr = _num(vals, 49, 'vol_ratio')
        # 取 TTM 市盈率 (腾讯口径为 39，避免错乱)
        pe_ttm = _num(vals, 39, 'pe_ttm')
        
        # 真实主力净流入从新浪获取
        main_net_inflow = get_sina_money_flow(stock_id)

        # 获取 60 日前复权 K 线，算支撑压力
        url_k = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={stock_id},day,,,60,qfq"
        req_k = urllib.request.Request(url_k, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req_k, timeout=5) as r:
            k_raw = json.loads(r.read().decode('utf-8'))
        
        k_data = k_raw['data'][stock_id]
        klines = k_data.get('qfqday') or k_data.get('day')
        
        last_date = klines[-1][0]
        
        # 【P0-3 熔断保护】
        expect = last_closed_trading_day(datetime.datetime.now())
        if last_date != expect:
            # 记录异常但暂时通过，交给外层脚本或者人类发现，防止误伤（如遇到中秋国庆，粗略计算不准）
            last_date += f" (⚠️数据可能陈旧, 预计应为 {expect})"

        closes = [float(x[2]) for x in klines]
        highs = [float(x[3]) for x in klines]
        lows = [float(x[4]) for x in klines]
        
        ma5 = round(sum(closes[-5:])/5, 2)
        ma10 = round(sum(closes[-10:])/10, 2)
        ma20 = round(sum(closes[-20:])/20, 2)
        
        # 【P1-2 修复】20日最高最低，不含当日
        res = round(max(highs[-21:-1]), 2) if len(highs) > 20 else round(max(highs), 2)
        sup = round(min(lows[-21:-1]), 2) if len(lows) > 20 else round(min(lows), 2)
        
        return {
            "pass": True,
            "code": stock_id,
            "name": name,
            "date": last_date,
            "price": price,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "ma5": ma5, "ma10": ma10, "ma20": ma20,
            "vol_ratio": vr,
            "turnover": turnover,
            "pe_ttm": pe_ttm,
            "main_net_inflow": main_net_inflow,
            "resistance": res,
            "support": sup
        }
    except Exception as e:
        return {"pass": False, "code": stock_id, "error": str(e)}

if __name__ == '__main__':
    # 测试
    print(get_tencent_panorama("sz300308"))
