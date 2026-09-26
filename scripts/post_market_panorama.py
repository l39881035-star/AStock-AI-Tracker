import json
import os
from fetch_full_panorama import get_tencent_panorama, fetch_sina_7x24_news

script_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(script_dir, 'config.json')

with open(config_path, 'r', encoding='utf-8') as f:
    config = json.load(f)

print("【系统背景指令】")
print(f"你是一个冷酷、理性的 A 股实战交易幕僚。结合技术面、资金面和消息面，做系统级推演。")
print(f"用户的底牌与操作纪律：{config['style']}\n")
print("【当前时间状态】⚠️ 警告：现在 A 股已经收盘！所有的应对剧本，必须是指向【明天开盘后】的操作预案！")
print("以下是收盘后的 3D 真实全景数据（涵盖技术/主力/舆情）。请生成推送到微信的《全景复盘与推演报告》。")
print("【输出结构要求】：")
print("1. [消息与情绪过滤]：一句话总结大盘今日情绪与是否有突发新闻。")
print("2. [资金与技术共振诊断]：不仅要看均线支撑，必须点出主力资金（净流入/净流出）和换手率背后的逻辑（例如：假突破真出货，还是缩量洗盘）。")
print("3. [明日推演与 If-Then 剧本]：给出明确的条件触发预案。\n")

print("================ [宏观消息面] 7x24 最新异动扫描 ================")
news = fetch_sina_7x24_news(limit=5)
for n in news:
    print("-", n)

print("\n================ [个股全景数据输入] ================")
for s in config['stocks']:
    data = get_tencent_panorama(s['code'])
    if not data['pass']:
        print(f"抓取失败: {s['code']} - {data['error']}")
        continue
        
    print(f"🎯 标的：{data['name']} ({data['code']})")
    print(f"基准日期: {data['date']}")
    print(f"收盘价: {data['price']} 元 (开:{data['open']}, 高:{data['high']}, 低:{data['low']})")
    print(f"[技术面] 均线阵列: MA5={data['ma5']}, MA10={data['ma10']}, MA20={data['ma20']}")
    print(f"[技术面] 前20日真实压力位: {data['resistance']} 元 | 支撑位: {data['support']} 元")
    print(f"[筹码资金] 换手率: {data['turnover']}% | 量比: {data['vol_ratio']}")
    print(f"[主力动向] 大单净流入(近似值): {data['main_net_inflow']} 万元 | 动态PE: {data['pe_ttm']}")
    print("-" * 40)
