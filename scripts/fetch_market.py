import urllib.request
import json
import sys

def get_tencent_data(stock_id):
    try:
        # 获取实时/盘后快照数据
        url_rt = f"http://qt.gtimg.cn/q={stock_id}"
        req = urllib.request.Request(url_rt, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as r:
            rt_raw = r.read().decode('gbk', errors='ignore')
        vals = rt_raw.split('=')[1].strip('"').split('~')
        
        name = vals[1]
        price = float(vals[3])
        open_p = float(vals[5])
        high_p = float(vals[33])
        low_p = float(vals[34])
        
        # 获取 60 日前复权 K 线，用于本地计算均线和支撑压力
        url_k = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={stock_id},day,,,60,qfq"
        req_k = urllib.request.Request(url_k, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req_k, timeout=5) as r:
            k_raw = json.loads(r.read().decode('utf-8'))
        
        k_data = k_raw['data'][stock_id]
        klines = k_data.get('qfqday') or k_data.get('day')
        
        last_date = klines[-1][0] # 末根 K 线日期
        
        closes = [float(x[2]) for x in klines]
        vols = [float(x[5]) for x in klines]
        highs = [float(x[3]) for x in klines]
        lows = [float(x[4]) for x in klines]
        
        # 核心算法：本地硬算，杜绝幻觉
        ma5 = round(sum(closes[-5:])/5, 2)
        ma10 = round(sum(closes[-10:])/10, 2)
        ma20 = round(sum(closes[-20:])/20, 2)
        
        v5 = sum(vols[-5:])/5
        vr = round(vols[-1]/v5, 2) if v5 > 0 else 0
        
        # 20日内最高价为压力，最低价为支撑
        res = round(max(highs[-20:]), 2)
        sup = round(min(lows[-20:]), 2)
        
        return {
            "pass": True,
            "code": stock_id,
            "name": name,
            "date": last_date,
            "price": price,
            "open": open_p,
            "high": high_p,
            "low": low_p,
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
            "vol_ratio": vr,
            "resistance": res,
            "support": sup
        }
    except Exception as e:
        return {"pass": False, "code": stock_id, "error": str(e)}
