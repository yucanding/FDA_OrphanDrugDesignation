import requests
from datetime import date, timedelta
from bs4 import BeautifulSoup
import re
import yfinance as yf
import time
import os
from deep_translator import GoogleTranslator

# --- 环境配置 ---
TG_TOKEN = os.getenv('TG_TOKEN')
TG_CHAT_ID = os.getenv('TG_CHAT_ID')
DB_FILE = "seen_orphan_drugs.txt"

def send_tg_message(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=20)
    except:
        pass

# --- 智能相似度匹配引擎 ---
def normalize_name(name):
    if not name: return []
    clean_str = re.sub(r'(?i)\b(inc|corp|corporation|ltd|llc|co|company|plc|lp|gmbh)\b|\.|,|-|!', ' ', name)
    return [w for w in clean_str.upper().split() if len(w) > 1]

def is_company_match(app_name, yf_name):
    app_words = normalize_name(app_name)
    yf_words = normalize_name(yf_name)
    if not app_words or not yf_words: return False
    if app_words[0] not in yf_words[0] and yf_words[0] not in app_words[0]: return False
    app_str = ' '.join(app_words)
    yf_str = ' '.join(yf_words)
    if app_str in yf_str or yf_str in app_str: return True
    overlap = set(app_words).intersection(set(yf_words))
    if len(overlap) >= 2: return True
    if len(app_words) == 1 and len(overlap) == 1: return True
    return False

def get_stock_info_smart(name):
    try:
        search_q = ' '.join(name.split()[:2])
        search = yf.Search(search_q, max_results=3)
        if not search.quotes: return None
        for q in search.quotes:
            ticker = q.get('symbol', '')
            if "." not in ticker:
                short_name = q.get('shortname', '')
                long_name = q.get('longname', '')
                if is_company_match(name, short_name) or is_company_match(name, long_name):
                    s = yf.Ticker(ticker)
                    info = s.fast_info
                    return {
                        "ticker": ticker,
                        "price": round(info.last_price, 2),
                        "cap": round(info.market_cap / 1e9, 2)
                    }
        return None
    except: return None

# --- 1. 加载历史记录 ---
if not os.path.exists(DB_FILE):
    open(DB_FILE, 'w').close()
with open(DB_FILE, "r", encoding="utf-8") as f:
    seen_data = set(line.strip() for line in f if line.strip())

# --- 2. 抓取逻辑 ---
today = date.today()
start_date = (today - timedelta(days=7)).strftime('%m/%d/%Y')
end_date = today.strftime('%m/%d/%Y')

url = 'https://www.accessdata.fda.gov/scripts/opdlisting/oopd/OOPD_Results.cfm'
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://www.accessdata.fda.gov/scripts/opdlisting/oopd/index.cfm'
}

data = {
    'Product_name': '', 'sponsor_name': '', 'Designation': '',
    'Designation_Start_Date': start_date, 'Designation_End_Date': end_date,
    'Search_param': 'DESDATE', 'Output_format': 'Excel', 
    'Sort_order': 'Date_Reverse_Order', 'RecordsPerPage': '100', 'newSearch': 'Run Search'
}

try:
    response = requests.post(url, data=data, headers=headers, timeout=30)
    
    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')
        table = soup.find('table')
        
        parsed_records = []
        
        if table:
            rows = table.find_all('tr')
            if len(rows) > 1:
                headers_list = [th.get_text(strip=True) for th in rows[0].find_all(['th', 'td'])]
                for row in rows[1:]:
                    cells = [td.get_text(strip=True) for td in row.find_all('td')]
                    if len(cells) == len(headers_list):
                        row_dict = dict(zip(headers_list, cells))
                        parsed_records.append(row_dict)
        else:
            lines = [line.strip() for line in response.text.splitlines() if line.strip()]
            if len(lines) > 1:
                headers_list = lines[0].split('\t')
                for line in lines[1:]:
                    cells = line.split('\t')
                    if len(cells) == len(headers_list):
                        parsed_records.append(dict(zip(headers_list, cells)))

        # --- 3. 筛选、翻译并合并消息 ---
        records_to_send = []
        translator = GoogleTranslator(source='en', target='zh-CN')
        
        for rec in parsed_records:
            applicant = rec.get('Sponsor Company', 'N/A')
            drug_en = rec.get('Orphan Designation', 'N/A')
            date_desig = rec.get('Date Designated', 'N/A')
            cfgridkey = rec.get('CF Grid Key', '')
            
            # 使用 CF Grid Key 作为唯一去重凭证
            unique_key = cfgridkey if cfgridkey else drug_en
            
            if unique_key and unique_key not in seen_data:
                if cfgridkey:
                    details_link = f"https://www.accessdata.fda.gov/scripts/opdlisting/oopd/detailedIndex.cfm?cfgridkey={cfgridkey}"
                else:
                    details_link = "无法获取详细链接"
                    
                stock_data = get_stock_info_smart(applicant)
                time.sleep(0.4) 
                
                if stock_data:
                    try:
                        drug_cn = translator.translate(drug_en)
                    except:
                        drug_cn = drug_en

                    records_to_send.append({
                        "date": date_desig,
                        "ticker": stock_data['ticker'],
                        "applicant": applicant,
                        "drug": drug_cn,
                        "cap": stock_data['cap'],
                        "price": stock_data['price'],
                        "link": details_link
                    })
                
                # 无论是否上市，只要核查过就存入数据库，防止明天重复请求雅虎API
                seen_data.add(unique_key)

        if records_to_send:
            final_msg = f"<b>🧬 FDA孤儿药认证更新 ({len(records_to_send)} 家上市企业)</b>\n\n"
            msg_blocks = []
            
            for idx, item in enumerate(records_to_send, 1):
                block = (f"{idx}. 📅日期: {item['date']}\n"
                         f"    🏢公司: ${item['ticker']} ({item['applicant']})\n"
                         f"    💊适应症: {item['drug']}\n"
                         f"    💰市值: ${item['cap']}B\n"
                         f"    💵股价: ${item['price']}\n"
                         f"    🔗链接: {item['link']}")
                msg_blocks.append(block)
            
            final_msg += "\n\n---------------\n\n".join(msg_blocks)
            send_tg_message(final_msg)
            print(f"✅ 发送了 {len(records_to_send)} 条上市企业获批信息。")

    # --- 4. 更新数据库 ---
    with open(DB_FILE, "w", encoding="utf-8") as f:
        for item in sorted(seen_data):
            f.write(f"{item}\n")

except Exception as e:
    pass # 保持静默

# import requests
# from datetime import date, timedelta
# from bs4 import BeautifulSoup
# import re
# import os
# import time

# # --- 环境配置 ---
# TG_TOKEN = os.getenv('TG_TOKEN')
# TG_CHAT_ID = os.getenv('TG_CHAT_ID')
# DB_FILE = "seen_designations.txt"

# def send_tg_message(text):
#     """发送汇总后的 TG 消息"""
#     if not TG_TOKEN or not TG_CHAT_ID:
#         print("⚠️ 调试提醒: 未发现 TG 凭证，仅执行本地解析。")
#         return
#     url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
#     try:
#         # Telegram 单条消息上限约 4096 字符
#         res = requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=20)
#         print(f"📡 TG 发送状态: {res.status_code}")
#     except Exception as e:
#         print(f"❌ TG 发送异常: {e}")

# def clean_sponsor_name(raw_name):
#     """严格执行用户提供的正则清理逻辑"""
#     if not raw_name or raw_name == 'N/A':
#         return 'N/A'
    
#     # 优先级1：公司后缀截断
#     company_endings = [
#         r'\bInc\b', r'\bInc\.\b', r'\bCo\b', r'\bCo\.\b',
#         r'\bLtd\b', r'\bLtd\.\b', r'\bLLC\b', r'\bCorp\b', 
#         r'\bCorp\.\b', r'\bLP\b', r'\bLimited\b'
#     ]
    
#     clean_name = raw_name
#     cut_pos = 0
#     for ending in company_endings:
#         match = re.search(ending, clean_name, re.IGNORECASE)
#         if match:
#             cut_pos = match.end()
#             break 
            
#     if cut_pos > 0:
#         clean_name = clean_name[:cut_pos].strip()
#     else:
#         # 优先级2：地址模式截断
#         address_patterns = [
#             r'\d{3,}', r'\d+\s?', r',\s*[A-Z][a-z]+\s?', r'BR/', 
#             r'The sponsor address listed', r'United States', 
#             r'Room\s?', r'\d+F\s?', r'No\.\s?', r'Building\s?', 
#             r'Floor\s?', r'Ste\s?', r'Unit\s?', r'Parc\s?'
#         ]
#         for pat in address_patterns:
#             match = re.search(pat, clean_name, re.IGNORECASE)
#             if match:
#                 clean_name = clean_name[:match.start()].strip()
#                 break

#     # 最终清理末尾符号
#     clean_name = re.sub(r'[,.]\s*$', '', clean_name).strip()
#     return clean_name

# # 1. 加载去重数据库 (存储格式改为: 公司_药名)
# if not os.path.exists(DB_FILE):
#     open(DB_FILE, 'w').close()
# with open(DB_FILE, "r", encoding="utf-8") as f:
#     seen_data = set(line.strip() for line in f if line.strip())

# # 2. 设置扫描日期 (最近 7 天)
# today = date.today()
# start_date = (today - timedelta(days=7)).strftime('%m/%d/%Y')
# end_date = today.strftime('%m/%d/%Y')

# print(f"--- 🚀 启动扫描 | 范围: {start_date} - {end_date} ---")

# url = 'https://www.accessdata.fda.gov/scripts/opdlisting/oopd/OOPD_Results.cfm'
# headers = {
#     'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
#     'Referer': 'https://www.accessdata.fda.gov/scripts/opdlisting/oopd/index.cfm'
# }
# data = {
#     'Product_name': '', 'sponsor_name': '', 'Designation': '',
#     'Designation_Start_Date': start_date, 'Designation_End_Date': end_date,
#     'Search_param': 'DESDATE', 'Output_format': 'Detailed',
#     'Sort_order': 'Date_Reverse_Order', 'RecordsPerPage': '100', 'newSearch': 'Run Search'
# }

# # 3. 执行抓取解析
# try:
#     response = requests.post(url, data=data, headers=headers, timeout=30)
#     if response.status_code == 200:
#         soup = BeautifulSoup(response.text, 'html.parser')
#         table = soup.find('table', class_='resultstable')

#         if table:
#             rows = table.find_all('tr')
#             current_record = {}
#             parsed_records = []
            
#             # 解析垂直表格逻辑
#             for row in rows:
#                 cells = row.find_all(['th', 'td'])
#                 if len(cells) < 2: continue

#                 label = cells[0].get_text(strip=True).replace('\xa0', ' ').strip()
#                 value = ' '.join(cells[1].get_text(strip=True).replace('\xa0', ' ').split())

#                 if "Result Number:" in label:
#                     if current_record and 'drug' in current_record:
#                         parsed_records.append(current_record)
#                     current_record = {'id': value}
#                 elif "Date Designated:" in label:
#                     current_record['date'] = value
#                 elif "Orphan Designation:" in label:
#                     current_record['drug'] = value
#                 elif "Sponsor:" in label:
#                     current_record['sponsor'] = value
            
#             # 结算最后一条
#             if current_record and 'drug' in current_record:
#                 parsed_records.append(current_record)

#             # --- 过滤与合并消息 ---
#             new_records_to_send = []
#             for rec in parsed_records:
#                 clean_co = clean_sponsor_name(rec.get('sponsor', 'N/A'))
#                 drug_content = rec.get('drug', 'N/A')
                
#                 # 【关键】复合去重键：公司 + 药名
#                 unique_key = f"{clean_co}_{drug_content}"
                
#                 if unique_key not in seen_data:
#                     new_records_to_send.append({
#                         "date": rec.get('date', 'N/A'),
#                         "company": clean_co,
#                         "content": drug_content
#                     })
#                     seen_data.add(unique_key)

#             if new_records_to_send:
#                 # 按照日期进行一次反向排序（确保最新的在最上面）
#                 final_msg = f"<b>🧬 FDA 孤儿药更新报告 ({len(new_records_to_send)} 条)</b>\n\n"
#                 for idx, item in enumerate(new_records_to_send, 1):
#                     final_msg += (f"{idx}. 📅 <b>日期:</b> {item['date']}\n"
#                                   f"    🏢 <b>公司:</b> {item['company']}\n"
#                                   f"    📝 <b>内容:</b> {item['content']}\n\n")
                
#                 send_tg_message(final_msg)
#                 print(f"✅ 成功发送 {len(new_records_to_send)} 条新记录。")
#             else:
#                 print("💡 今日无新增记录。")

#     # 4. 更新数据库文件
#     with open(DB_FILE, "w", encoding="utf-8") as f:
#         for item in sorted(seen_data):
#             f.write(f"{item}\n")

# except Exception as e:
#     print(f"🛑 运行异常: {e}")


