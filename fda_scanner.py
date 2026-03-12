import requests
from datetime import date, timedelta
from bs4 import BeautifulSoup
import re
import os
import yfinance as yf
import time

# 配置
TG_TOKEN = os.getenv('TG_TOKEN')
TG_CHAT_ID = os.getenv('TG_CHAT_ID')
DB_FILE = "seen_designations.txt"

def load_seen_data():
    if not os.path.exists(DB_FILE):
        return set()
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def save_seen_data(seen_set):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        for item in sorted(seen_set):
            f.write(f"{item}\n")

def send_tg_message(text):
    if not TG_TOKEN or not TG_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)

def get_stock_info(name):
    try:
        search_q = ' '.join(name.split()[:2])
        search = yf.Search(search_q, max_results=2)
        if not search.quotes: return None
        ticker = search.quotes[0].get('symbol', '')
        if "." not in ticker:
            s = yf.Ticker(ticker)
            return {"ticker": ticker, "price": round(s.fast_info.last_price, 2), "cap": round(s.fast_info.market_cap/1e9, 2)}
    except: return None
    return None

# 加载已发送的历史记录
seen_data = load_seen_data()
new_seen_data = seen_data.copy()

# FDA 爬取逻辑 (保持 7 天范围，确保不漏掉周末更新)
today = date.today()
start_date = (today - timedelta(days=7)).strftime('%m/%d/%Y')
end_date = today.strftime('%m/%d/%Y')

url = 'https://www.accessdata.fda.gov/scripts/opdlisting/oopd/OOPD_Results.cfm'
headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.accessdata.fda.gov/scripts/opdlisting/oopd/index.cfm'}
data = {
    'Designation_Start_Date': start_date, 'Designation_End_Date': end_date,
    'Search_param': 'DESDATE', 'Output_format': 'Detailed',
    'Sort_order': 'Date_Reverse_Order', 'RecordsPerPage': '100', 'newSearch': 'Run Search'
}

response = requests.post(url, data=data, headers=headers, timeout=30)
if response.status_code == 200:
    soup = BeautifulSoup(response.text, 'html.parser')
    table = soup.find('table', class_='resultstable')
    if table:
        current_record = {}
        for row in table.find_all('tr'):
            cells = row.find_all(['th', 'td'])
            if len(cells) < 2: continue
            label = cells[0].get_text(strip=True)
            value = ' '.join(cells[1].get_text(strip=True).split())

            if "Result Number:" in label:
                if current_record:
                    # 使用 Designation 内容作为唯一特征 ID
                    designation_content = current_record.get('Orphan Designation', 'N/A')
                    
                    # --- 去重逻辑 ---
                    if designation_content not in seen_data:
                        sponsor_raw = current_record.get('Sponsor', 'N/A')
                        # 正则清理 (此处简略，保留你之前的完整正则即可)
                        clean_sponsor = re.sub(r'\b(Inc|Ltd|LLC|Corp)\b.*', r'\1', sponsor_raw, flags=re.I).strip()
                        
                        stock = get_stock_info(clean_sponsor)
                        msg = (f"<b>💊 FDA 孤儿药更新</b>\n"
                               f"<b>日期:</b> {current_record.get('Date Designated')}\n"
                               f"<b>公司:</b> {clean_sponsor}\n")
                        if stock:
                            msg += f"<b>🚀 美股上市:</b> <code>{stock['ticker']}</code> | ${stock['price']} | {stock['cap']}B\n"
                        msg += f"<b>内容:</b> {designation_content}"
                        
                        send_tg_message(msg)
                        new_seen_data.add(designation_content)
                        time.sleep(1)

                current_record = {}
            elif "Date Designated:" in label: current_record['Date Designated'] = value
            elif "Orphan Designation:" in label: current_record['Orphan Designation'] = value
            elif "Sponsor:" in label: current_record['Sponsor'] = value

# 保存更新后的历史记录
save_seen_data(new_seen_data)