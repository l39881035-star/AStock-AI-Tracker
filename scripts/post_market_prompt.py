import json
from fetch_market import get_tencent_data

with open('config.json', 'r', encoding='utf-8') as f:
    config = json.load(f)

print("【系统背景指令】")
print(f"你是一个冷酷、理性的 A 股实战交易幕僚。")
print(f"用户的底牌与操作纪律：{config['style']}\n")
print("以下是收盘后的绝对真实数据（由底层 Python 程序通过腾讯 API 硬算得出，不存在幻觉）。")
print("请你阅读这些数据，直接生成一份推送到用户微信的《盘后对账与复盘报告》。")
print("输出要求：结构清晰，包含 [实际走势]、[盘后诊断]、[应对纪律修正] 三个板块。语气要像军师一样果断。\n")

print("================ 实盘数据输入 ================")
for s in config['stocks']:
    data = get_tencent_data(s['code'])
    if not data['pass']:
        print(f"抓取失败: {s['code']} - {data['error']}")
        continue
        
    print(f"🎯 标的：{data['name']} ({data['code']})")
    print(f"基准日期: {data['date']}")
    print(f"今日收盘价: {data['price']} 元 (今日开盘:{data['open']}, 最高:{data['high']}, 最低:{data['low']})")
    print(f"均线阵列: MA5={data['ma5']}, MA10={data['ma10']}, MA20={data['ma20']}")
    print(f"量比(今日/5日均): {data['vol_ratio']}")
    print(f"前20日真实压力位: {data['resistance']} 元")
    print(f"前20日真实支撑位: {data['support']} 元")
    print("-" * 40)
