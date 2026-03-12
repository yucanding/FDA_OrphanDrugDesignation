import requests
from datetime import date, timedelta
from bs4 import BeautifulSoup
import re
import os
import yfinance as yf
import time

# --- 环境配置 ---
TG_TOKEN = os.getenv('TG_TOKEN')
TG_CHAT_ID = os.getenv('TG_CHAT_ID')
DB_FILE = "seen_designations.txt"

def send_tg_message(text):
    """发送 TG 消息并打印调试状态"""
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ 调试提醒: 未发现 TG_TOKEN 或 TG_CHAT_ID，跳过发送。")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        res = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
        print(f"📡 TG 发送尝试 | 状态码: {res.status_code} | 响应内容: {res.text}")
    except Exception as e:
        print(f"❌ TG 发送发生异常: {e}")

# 1. 加载去重数据库
if not os.path.exists(DB_FILE):
    open(DB_FILE, 'w').close()
with open(DB_FILE, "r", encoding="utf-8") as f:
    seen_data = set(line.strip() for line in f if line.strip())

# 2. 设置搜索日期 (最近 7 天)
today = date.today()
seven_days_ago = today - timedelta(days=7)
start_date = seven_days_ago.strftime('%m/%d/%Y')
end_date = today.strftime('%m/%d/%Y')

print(f"--- 🚀 调试启动 | 范围: {start_date} - {end_date} ---")

url = 'https://www.accessdata.fda.gov/scripts/opdlisting/oopd/OOPD_Results.cfm'
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Referer': 'https://www.accessdata.fda.gov/scripts/opdlisting/oopd/index.cfm'
}
data = {
    'Product_name': '', 'sponsor_name': '', 'Designation': '',
    'Designation_Start_Date': start_date, 'Designation_End_Date': end_date,
    'Search_param': 'DESDATE', 'Output_format': 'Detailed',
    'Sort_order': 'Date_Reverse_Order', 'RecordsPerPage': '100', 'newSearch': 'Run Search'
}

# 3. 发起 FDA 请求
try:
    response = requests.post(url, data=data, headers=headers, timeout=30)
    print(f"🌐 FDA 响应状态码: {response.status_code}")
    
    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')
        table = soup.find('table', class_='resultstable')

        if not table:
            print("❌ 关键报错: 未在返回页面中找到 'resultstable' 表格。")
            print("🔍 页面内容预览 (前500字):")
            print(response.text[:500])
        else:
            rows = table.find_all('tr')
            current_record = {}
            record_count = 0
            new_push_count = 0

            for row in rows:
                cells = row.find_all(['th', 'td'])
                if len(cells) < 2: continue

                label = cells[0].get_text(strip=True).replace('\xa0', ' ').strip()
                value = ' '.join(cells[1].get_text(strip=True).replace('\xa0', ' ').split())

                if "Result Number:" in label:
                    if current_record:
                        # --- 执行你要求的 Sponsor Name 原样正则处理逻辑 ---
                        sponsor_raw = current_record.get('Sponsor', 'N/A')
                        company_endings = [
                            r'\bInc\b', r'\bInc\.\b', r'\bCo\b', r'\bCo\.\b',
                            r'\bLtd\b', r'\bLtd\.\b', r'\bLLC\b', r'\bCorp\b', 
                            r'\bCorp\.\b', r'\bLP\b', r'\bLimited\b'
                        ]
                        clean_sponsor = sponsor_raw
                        cut_pos = 0
                        for ending in company_endings:
                            match = re.search(ending, clean_sponsor, re.IGNORECASE)
                            if match:
                                cut_pos = match.end()
                                break
                        if cut_pos > 0:
                            clean_sponsor = clean_sponsor[:cut_pos].strip()
                        else:
                            address_patterns = [
                                r'\d{3,}', r'\d+\s?', r',\s*[A-Z][a-z]+\s?', r'BR/', 
                                r'The sponsor address listed', r'United States', 
                                r'Room\s?', r'\d+F\s?', r'No\.\s?', r'Building\s?', 
                                r'Floor\s?', r'Ste\s?', r'Unit\s?', r'Parc\s?'
                            ]
                            for pat in address_patterns:
                                match = re.search(pat, clean_sponsor, re.IGNORECASE)
                                if match:
                                    clean_sponsor = clean_sponsor[:match.start()].strip()
                                    break
                        clean_sponsor = re.sub(r'[,.]\s*$', '', clean_sponsor).strip()

                        # --- 去重与金融核查推送 ---
                        designation = current_record.get('Orphan Designation', 'N/A')
                        if designation not in seen_data:
                            new_push_count += 1
                            # 雅虎金融核查
                            search_q = ' '.join(clean_sponsor.split()[:2])
                            ticker_info = "⚪ 未在美股上市"
                            try:
                                s = yf.Search(search_q, max_results=1).quotes
                                if s and "." not in s[0]['symbol']:
                                    ticker = s[0]['symbol']
                                    f_info = yf.Ticker(ticker).fast_info
                                    ticker_info = f"🚀 <b>美股代码:</b> <code>{ticker}</code> | 股价: ${f_info.last_price:.2f} | 市值: ${f_info.market_cap/1e9:.2f}B"
                            except: pass

                            # 组装消息
                            msg = (f"<b>🧬 新发现 FDA 孤儿药指定</b>\n"
                                   f"📅 <b>日期:</b> {current_record.get('Date Designated', 'N/A')}\n"
                                   f"🏢 <b>赞助商:</b> {clean_sponsor}\n"
                                   f"{ticker_info}\n"
                                   f"📝 <b>内容:</b> {designation}")
                            
                            send_tg_message(msg)
                            seen_data.add(designation)
                        
                        record_count += 1
                    current_record = {}
                elif "Date Designated:" in label: current_record['Date Designated'] = value
                elif "Orphan Designation:" in label: current_record['Orphan Designation'] = value
                elif "Sponsor:" in label: current_record['Sponsor'] = value

            print(f"✅ 扫描完成 | 发现总记录: {record_count} | 新推送数量: {new_push_count}")

except Exception as e:
    print(f"🛑 脚本运行过程中发生致命错误: {e}")

# 4. 保存历史记录
with open(DB_FILE, "w", encoding="utf-8") as f:
    for item in sorted(seen_data):
        f.write(f"{item}\n")
print("💾 历史记录已更新到本地文件。")
