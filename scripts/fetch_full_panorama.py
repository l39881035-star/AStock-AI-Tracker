import urllib.request
import json
import sys
import datetime

def fetch_sina_7x24_news(limit=20):
    """抓取新浪 7x24 快讯，作为大盘情绪和突发消息样本"""
    try:
        url = f"https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size={limit}&zhibo_id=152"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode('utf-8'))
        
        msgs = data['result']['data']['feed']['list']
        news_list = []
        for m in msgs:
            rich_text = m.get('rich_text', '')
            if rich_text:
                # 简单清洗掉HTML标签
                clean_text = rich_text.replace('<br>', '').replace('&nbsp;', ' ')
                news_list.append(clean_text[:100] + '...') # 截取精华
        return news_list
    except Exception as e:
        return [f"新闻抓取异常: {str(e)}"]

def get_tencent_panorama(stock_id):
    """腾讯财经全景接口，包含量价、换手率、主力大单流入"""
    try:
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
        turnover = float(vals[38]) # 换手率
        pe_ttm = float(vals[39]) if vals[39] != '' else 0.0 # 动态市盈率
        
        # 资金流向 (腾讯接口大单流入流出，单位万)
        # 腾讯 qt 接口中: 43=主力流入(万), 44=主力流出(万), 45=主力净流入(万) - 注意这里用作近似替代，不同股票代码返回长度可能略有差异，做个容错
        try:
            main_net_inflow = float(vals[45]) if len(vals) > 45 and vals[45] != '' else 0.0
        except:
            main_net_inflow = "暂无数据"

        # 获取 60 日前复权 K 线，算支撑压力
        url_k = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={stock_id},day,,,60,qfq"
        req_k = urllib.request.Request(url_k, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req_k, timeout=5) as r:
            k_raw = json.loads(r.read().decode('utf-8'))
        
        k_data = k_raw['data'][stock_id]
        klines = k_data.get('qfqday') or k_data.get('day')
        
        last_date = klines[-1][0]
        closes = [float(x[2]) for x in klines]
        vols = [float(x[5]) for x in klines]
        highs = [float(x[3]) for x in klines]
        lows = [float(x[4]) for x in klines]
        
        ma5 = round(sum(closes[-5:])/5, 2)
        ma10 = round(sum(closes[-10:])/10, 2)
        ma20 = round(sum(closes[-20:])/20, 2)
        
        v5 = sum(vols[-5:])/5
        vr = round(vols[-1]/v5, 2) if v5 > 0 else 0
        
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
    # 这个块只做简单的本地输出测试
    pass
