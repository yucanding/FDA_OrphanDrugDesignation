import requests
from datetime import date, timedelta
from bs4 import BeautifulSoup
import re
import os
import time

# --- 环境配置 ---
TG_TOKEN = os.getenv('TG_TOKEN')
TG_CHAT_ID = os.getenv('TG_CHAT_ID')
DB_FILE = "seen_designations.txt"

def send_tg_message(text):
    """发送单条汇总消息"""
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ 调试提醒: 未配置 TG 参数。")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        res = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=20)
        print(f"📡 TG 发送状态: {res.status_code}")
    except Exception as e:
        print(f"❌ TG 发送异常: {e}")

# 1. 加载去重数据库
if not os.path.exists(DB_FILE):
    open(DB_FILE, 'w').close()
with open(DB_FILE, "r", encoding="utf-8") as f:
    seen_data = set(line.strip() for line in f if line.strip())

# 2. 设置扫描日期 (最近 7 天)
today = date.today()
start_date = (today - timedelta(days=7)).strftime('%m/%d/%Y')
end_date = today.strftime('%m/%d/%Y')

print(f"--- 🚀 开始扫描 | 范围: {start_date} - {end_date} ---")

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

# 3. 执行抓取
try:
    response = requests.post(url, data=data, headers=headers, timeout=30)
    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')
        table = soup.find('table', class_='resultstable')

        if table:
            rows = table.find_all('tr')
            current_record = {}
            new_records_list = []

            for row in rows:
                cells = row.find_all(['th', 'td'])
                if len(cells) < 2: continue

                label = cells[0].get_text(strip=True).replace('\xa0', ' ').strip()
                value = ' '.join(cells[1].get_text(strip=True).replace('\xa0', ' ').split())

                if "Result Number:" in label:
                    if current_record:
                        # --- Sponsor Name 正则清理逻辑 ---
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

                        # --- 去重逻辑 ---
                        designation = current_record.get('Orphan Designation', 'N/A')
                        if designation not in seen_data:
                            new_records_list.append({
                                "date": current_record.get('Date Designated', 'N/A'),
                                "company": clean_sponsor,
                                "content": designation
                            })
                            seen_data.add(designation)
                        else:
                            # 增加这一行打印，在 GitHub Actions 的日志里就能看到跳过了哪些
                            print(f"⏭️ 跳过重复记录: {designation[:30]}...")

                    current_record = {}
                elif "Date Designated:" in label: current_record['Date Designated'] = value
                elif "Orphan Designation:" in label: current_record['Orphan Designation'] = value
                elif "Sponsor:" in label: current_record['Sponsor'] = value

            # --- 合并消息发送 ---
            if new_records_list:
                final_msg = f"<b>🧬 FDA 孤儿药更新报告 ({len(new_records_list)} 条)</b>\n\n"
                # 按时间倒序或顺序编排，这里按抓取到的顺序（通常是倒序）编号
                for idx, item in enumerate(new_records_list, 1):
                    final_msg += (f"{idx}. 📅 <b>日期:</b> {item['date']}\n"
                                  f"    🏢 <b>公司:</b> {item['company']}\n"
                                  f"    📝 <b>内容:</b> {item['content']}\n\n")
                
                # Telegram 单条消息上限约 4000 字符，通常够用
                send_tg_message(final_msg)
                print(f"✅ 已合并发送 {len(new_records_list)} 条新记录。")
            else:
                print("💡 今日无新数据更新。")

    # 保存历史记录
    with open(DB_FILE, "w", encoding="utf-8") as f:
        for item in sorted(seen_data):
            f.write(f"{item}\n")

except Exception as e:
    print(f"🛑 运行错误: {e}")

