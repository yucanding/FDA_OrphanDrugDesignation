import requests
from datetime import date, timedelta, datetime
from bs4 import BeautifulSoup
import re
import yfinance as yf
import time
import os
from deep_translator import GoogleTranslator

# --- 环境配置 ---
TG_TOKEN = os.getenv('TG_TOKEN')
TG_CHAT_ID = os.getenv('TG_CHAT_ID')  # 支持逗号分隔的多个 ID
DB_FILE = "seen_orphan_drugs.txt"

def convert_date_to_chinese(date_str):
    """将日期 03/10/2026 (MM/DD/YYYY) 转换为 2026年3月10日"""
    try:
        dt = datetime.strptime(date_str.strip(), "%m/%d/%Y")
        return f"{dt.year}年{dt.month}月{dt.day}日"
    except:
        return date_str

def send_tg_message(text):
    if not TG_TOKEN or not TG_CHAT_ID or not text:
        return
    
    target_ids = [chat_id.strip() for chat_id in TG_CHAT_ID.split(',') if chat_id.strip()]
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    
    for chat_id in target_ids:
        try:
            requests.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=20
            )
        except:
            pass

# --- 智能相似度与美股过滤引擎 ---
def normalize_name(name):
    """清理全球公司后缀，确保母子公司核心名一致"""
    if not name: return []
    # 覆盖欧美及澳洲主流后缀
    suffixes = r'\b(inc|corp|corporation|ltd|limited|llc|co|company|plc|lp|gmbh|sas|s\.a\.s|s\.a|bv|nv|ag|ads|se)\b'
    clean_str = re.sub(rf'(?i){suffixes}|\.|,|-|!', ' ', name)
    return [w for w in clean_str.upper().split() if len(w) > 1]

def is_company_match(app_name, yf_name):
    """匹配核心名一致且至少有一个核心词重叠"""
    app_words = normalize_name(app_name)
    yf_words = normalize_name(yf_name)
    if not app_words or not yf_words: return False
    
    # 品牌核心词校验
    if app_words[0] not in yf_words[0] and yf_words[0] not in app_words[0]:
        return False
    
    overlap = set(app_words).intersection(set(yf_words))
    return len(overlap) >= 1

def get_stock_info_smart(name):
    """
    检索逻辑：
    1. 搜索公司前两个核心词
    2. 检查 Sector 是否为 Healthcare
    3. 检查交易货币是否为 USD (关键：过滤韩股、澳股等干扰)
    """
    try:
        core_words = normalize_name(name)
        if not core_words: return None
        
        search_q = ' '.join(core_words[:2])
        search = yf.Search(search_q, max_results=6)
        if not search.quotes: return None

        # 优先检索无后缀主板，再检索带点后缀
        for q in search.quotes:
            ticker = q.get('symbol', '')
            if is_company_match(name, q.get('shortname', '')) or is_company_match(name, q.get('longname', '')):
                s = yf.Ticker(ticker)
                info = s.info
                
                # 获取行业和货币
                sector = info.get('sector', '')
                currency = info.get('currency', info.get('financialCurrency', ''))
                
                # 只有行业对标且在美股（USD交易）才通过
                if sector == 'Healthcare' and currency == 'USD':
                    f_info = s.fast_info
                    return {
                        "ticker": ticker,
                        "price": round(f_info.last_price, 2) if f_info.last_price else 0,
                        "cap": round(f_info.market_cap / 1e9, 2) if f_info.market_cap else 0
                    }
        return None
    except:
        return None

# --- 主逻辑区 ---

# 1. 加载历史数据
if not os.path.exists(DB_FILE):
    open(DB_FILE, 'w').close()
with open(DB_FILE, "r", encoding="utf-8") as f:
    seen_data = set(line.strip() for line in f if line.strip())

# 2. 爬取 FDA 最近 7 天数据
today = date.today()
start_date = (today - timedelta(days=7)).strftime('%m/%d/%Y')
end_date = today.strftime('%m/%d/%Y')

url = 'https://www.accessdata.fda.gov/scripts/opdlisting/oopd/OOPD_Results.cfm'
headers = {'User-Agent': 'Mozilla/5.0'}
payload = {
    'Product_name': '', 'sponsor_name': '', 'Designation': '',
    'Designation_Start_Date': start_date, 'Designation_End_Date': end_date,
    'Search_param': 'DESDATE', 'Output_format': 'Excel',
    'Sort_order': 'Date_Reverse_Order', 'RecordsPerPage': '100', 'newSearch': 'Run Search'
}

try:
    response = requests.post(url, data=payload, headers=headers, timeout=30)
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
                        parsed_records.append(dict(zip(headers_list, cells)))

        # 3. 匹配、翻译与推送
        records_to_send = []
        translator = GoogleTranslator(source='en', target='zh-CN')
        
        for rec in parsed_records:
            applicant = rec.get('Sponsor Company', 'N/A')
            drug_en = rec.get('Orphan Designation', 'N/A')
            date_desig = rec.get('Date Designated', 'N/A')
            cfgridkey = rec.get('CF Grid Key', '')
            
            # 以 CF Grid Key 或药名作为去重唯一 ID
            unique_key = cfgridkey if cfgridkey else drug_en
            
            if unique_key and unique_key not in seen_data:
                # 核心检索
                stock = get_stock_info_smart(applicant)
                time.sleep(0.4) # 防止 API 请求过快
                
                if stock:
                    try:
                        drug_cn = translator.translate(drug_en)
                    except:
                        drug_cn = drug_en
                    
                    link = f"https://www.accessdata.fda.gov/scripts/opdlisting/oopd/detailedIndex.cfm?cfgridkey={cfgridkey}" if cfgridkey else "N/A"
                    
                    records_to_send.append({
                        "date": convert_date_to_chinese(date_desig),
                        "ticker": stock['ticker'],
                        "applicant": applicant,
                        "drug": drug_cn,
                        "cap": stock['cap'],
                        "price": stock['price'],
                        "link": link
                    })
                
                # 记录已扫描，不论是否匹配成功（防止次日重复扫描已知非上市药企）
                seen_data.add(unique_key)

        # 4. 发送合并消息
        if records_to_send:
            final_msg = f"<b>🧬 FDA孤儿药认证更新 ({len(records_to_send)}家上市企业)</b>\n\n"
            msg_blocks = []
            for idx, item in enumerate(records_to_send, 1):
                block = (f"{idx}. 📅日期: {item['date']}\n"
                         f"    🏢公司: <b>${item['ticker']}</b> ({item['applicant']})\n"
                         f"    💊适应症: {item['drug']}\n"
                         f"    💰市值: ${item['cap']}B\n"
                         f"    💵股价: ${item['price']}\n"
                         f'    🔗<a href="{item["link"]}">点击查看公告</a>')
                msg_blocks.append(block)
            
            final_msg += "\n\n---------------\n\n".join(msg_blocks)
            final_msg += "\n\n#FDA #OrphanDrug"
            send_tg_message(final_msg)
            print(f"✅ 已向 Telegram 推送 {len(records_to_send)} 条更新。")

    # 5. 更新本地去重数据库
    with open(DB_FILE, "w", encoding="utf-8") as f:
        for item in sorted(seen_data):
            f.write(f"{item}\n")

except Exception as e:
    print(f"❌ 程序运行出错: {e}")
