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
    """发送汇总后的 TG 消息"""
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ 调试提醒: 未发现 TG 凭证，仅执行本地解析。")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        # Telegram 单条消息上限约 4096 字符
        res = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=20)
        print(f"📡 TG 发送状态: {res.status_code}")
    except Exception as e:
        print(f"❌ TG 发送异常: {e}")

def clean_sponsor_name(raw_name):
    """严格执行用户提供的正则清理逻辑"""
    if not raw_name or raw_name == 'N/A':
        return 'N/A'
    
    # 优先级1：公司后缀截断
    company_endings = [
        r'\bInc\b', r'\bInc\.\b', r'\bCo\b', r'\bCo\.\b',
        r'\bLtd\b', r'\bLtd\.\b', r'\bLLC\b', r'\bCorp\b', 
        r'\bCorp\.\b', r'\bLP\b', r'\bLimited\b'
    ]
    
    clean_name = raw_name
    cut_pos = 0
    for ending in company_endings:
        match = re.search(ending, clean_name, re.IGNORECASE)
        if match:
            cut_pos = match.end()
            break 
            
    if cut_pos > 0:
        clean_name = clean_name[:cut_pos].strip()
    else:
        # 优先级2：地址模式截断
        address_patterns = [
            r'\d{3,}', r'\d+\s?', r',\s*[A-Z][a-z]+\s?', r'BR/', 
            r'The sponsor address listed', r'United States', 
            r'Room\s?', r'\d+F\s?', r'No\.\s?', r'Building\s?', 
            r'Floor\s?', r'Ste\s?', r'Unit\s?', r'Parc\s?'
        ]
        for pat in address_patterns:
            match = re.search(pat, clean_name, re.IGNORECASE)
            if match:
                clean_name = clean_name[:match.start()].strip()
                break

    # 最终清理末尾符号
    clean_name = re.sub(r'[,.]\s*$', '', clean_name).strip()
    return clean_name

# 1. 加载去重数据库 (存储格式改为: 公司_药名)
if not os.path.exists(DB_FILE):
    open(DB_FILE, 'w').close()
with open(DB_FILE, "r", encoding="utf-8") as f:
    seen_data = set(line.strip() for line in f if line.strip())

# 2. 设置扫描日期 (最近 7 天)
today = date.today()
start_date = (today - timedelta(days=7)).strftime('%m/%d/%Y')
end_date = today.strftime('%m/%d/%Y')

print(f"--- 🚀 启动扫描 | 范围: {start_date} - {end_date} ---")

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

# 3. 执行抓取解析
try:
    response = requests.post(url, data=data, headers=headers, timeout=30)
    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')
        table = soup.find('table', class_='resultstable')

        if table:
            rows = table.find_all('tr')
            current_record = {}
            parsed_records = []
            
            # 解析垂直表格逻辑
            for row in rows:
                cells = row.find_all(['th', 'td'])
                if len(cells) < 2: continue

                label = cells[0].get_text(strip=True).replace('\xa0', ' ').strip()
                value = ' '.join(cells[1].get_text(strip=True).replace('\xa0', ' ').split())

                if "Result Number:" in label:
                    if current_record and 'drug' in current_record:
                        parsed_records.append(current_record)
                    current_record = {'id': value}
                elif "Date Designated:" in label:
                    current_record['date'] = value
                elif "Orphan Designation:" in label:
                    current_record['drug'] = value
                elif "Sponsor:" in label:
                    current_record['sponsor'] = value
            
            # 结算最后一条
            if current_record and 'drug' in current_record:
                parsed_records.append(current_record)

            # --- 过滤与合并消息 ---
            new_records_to_send = []
            for rec in parsed_records:
                clean_co = clean_sponsor_name(rec.get('sponsor', 'N/A'))
                drug_content = rec.get('drug', 'N/A')
                
                # 【关键】复合去重键：公司 + 药名
                unique_key = f"{clean_co}_{drug_content}"
                
                if unique_key not in seen_data:
                    new_records_to_send.append({
                        "date": rec.get('date', 'N/A'),
                        "company": clean_co,
                        "content": drug_content
                    })
                    seen_data.add(unique_key)

            if new_records_to_send:
                # 按照日期进行一次反向排序（确保最新的在最上面）
                final_msg = f"<b>🧬 FDA 孤儿药更新报告 ({len(new_records_to_send)} 条)</b>\n\n"
                for idx, item in enumerate(new_records_to_send, 1):
                    final_msg += (f"{idx}. 📅 <b>日期:</b> {item['date']}\n"
                                  f"    🏢 <b>公司:</b> {item['company']}\n"
                                  f"    📝 <b>内容:</b> {item['content']}\n\n")
                
                send_tg_message(final_msg)
                print(f"✅ 成功发送 {len(new_records_to_send)} 条新记录。")
            else:
                print("💡 今日无新增记录。")

    # 4. 更新数据库文件
    with open(DB_FILE, "w", encoding="utf-8") as f:
        for item in sorted(seen_data):
            f.write(f"{item}\n")

except Exception as e:
    print(f"🛑 运行异常: {e}")
