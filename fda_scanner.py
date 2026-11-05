
import requests
from datetime import date, timedelta, datetime
from bs4 import BeautifulSoup
import re
import yfinance as yf
import time
import os
import html
import tempfile
from deep_translator import GoogleTranslator


# ============================================================
# 环境配置
# ============================================================

TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")

DB_FILE = "seen_orphan_drugs.txt"

FDA_URL = (
    "https://www.accessdata.fda.gov/scripts/"
    "opdlisting/oopd/OOPD_Results.cfm"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

REQUEST_TIMEOUT = 30

# Telegram 单条消息长度预留空间
TELEGRAM_MAX_LENGTH = 3800


# ============================================================
# 日期转换
# ============================================================

def convert_date_to_chinese(date_str):
    """将 MM/DD/YYYY 转换为中文日期"""
    try:
        dt = datetime.strptime(
            date_str.strip(),
            "%m/%d/%Y"
        )
        return f"{dt.year}年{dt.month}月{dt.day}日"

    except Exception:
        return date_str


# ============================================================
# Telegram 推送
# ============================================================

def send_tg_message(text):
    """向所有 Telegram 目标推送消息，失败时抛出异常"""

    if not text:
        return

    if not TG_TOKEN or not TG_CHAT_ID:
        raise RuntimeError(
            "Telegram 配置缺失：TG_TOKEN 或 TG_CHAT_ID"
        )

    target_ids = [
        chat_id.strip()
        for chat_id in TG_CHAT_ID.split(",")
        if chat_id.strip()
    ]

    if not target_ids:
        raise RuntimeError(
            "TG_CHAT_ID 中没有有效的 Chat ID"
        )

    url = (
        f"https://api.telegram.org/"
        f"bot{TG_TOKEN}/sendMessage"
    )

    errors = []

    for chat_id in target_ids:

        try:
            response = requests.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False
                },
                timeout=20
            )

            response.raise_for_status()

            result = response.json()

            if not result.get("ok"):
                raise RuntimeError(
                    f"Telegram API 返回错误: {result}"
                )

            print(
                f"✅ Telegram 推送成功: {chat_id}"
            )

        except Exception as e:

            print(
                f"❌ Telegram 推送失败 "
                f"[{chat_id}]: {e}"
            )

            errors.append(
                f"{chat_id}: {e}"
            )

    if errors:
        raise RuntimeError(
            "Telegram 推送失败:\n"
            + "\n".join(errors)
        )


# ============================================================
# Telegram 长消息拆分
# ============================================================

def send_messages_in_chunks(header, blocks, footer):
    """按照 Telegram 消息长度限制拆分推送"""

    if not blocks:
        return

    messages = []
    current_blocks = []

    for block in blocks:

        candidate = (
            header
            + "\n\n"
            + "\n\n---------------\n\n".join(
                current_blocks + [block]
            )
            + footer
        )

        if (
            len(candidate) > TELEGRAM_MAX_LENGTH
            and current_blocks
        ):

            messages.append(
                header
                + "\n\n"
                + "\n\n---------------\n\n".join(
                    current_blocks
                )
                + footer
            )

            current_blocks = [block]

        else:
            current_blocks.append(block)

    if current_blocks:
        messages.append(
            header
            + "\n\n"
            + "\n\n---------------\n\n".join(
                current_blocks
            )
            + footer
        )

    for index, message in enumerate(messages, 1):

        if len(messages) > 1:
            message = (
                message
                + f"\n\n<i>消息分段: {index}/{len(messages)}</i>"
            )

        send_tg_message(message)


# ============================================================
# 公司名称标准化与匹配
# ============================================================

def normalize_name(name):
    """清理全球公司后缀，提取核心名称"""

    if not name:
        return []

    suffixes = (
        r'\b(inc|corp|corporation|ltd|limited|llc|co|'
        r'company|plc|lp|gmbh|sas|s\.a\.s|s\.a|'
        r'bv|nv|ag|ads|se)\b'
    )

    clean_str = re.sub(
        rf'(?i){suffixes}|\.|,|-|!',
        ' ',
        name
    )

    return [
        word
        for word in clean_str.upper().split()
        if len(word) > 1
    ]


def is_company_match(app_name, yf_name):
    """匹配核心名称"""

    app_words = normalize_name(app_name)
    yf_words = normalize_name(yf_name)

    if not app_words or not yf_words:
        return False

    if (
        app_words[0] not in yf_words[0]
        and yf_words[0] not in app_words[0]
    ):
        return False

    overlap = set(app_words).intersection(
        set(yf_words)
    )

    return len(overlap) >= 1


# ============================================================
# Yahoo Finance 公司检索
# ============================================================

def get_stock_info_smart(name):
    """
    返回三种状态：

    match:
        找到符合条件的美股公司。

    no_match:
        检索成功，但没有符合条件的公司。

    error:
        查询过程中出现异常，应该下次重新尝试。
    """

    try:

        core_words = normalize_name(name)

        if not core_words:
            return {
                "status": "no_match"
            }

        search_q = " ".join(core_words[:2])

        print(
            f"🔎 Yahoo Finance 搜索: {search_q}"
        )

        search = yf.Search(
            search_q,
            max_results=6
        )

        if not search.quotes:
            return {
                "status": "no_match"
            }

        query_errors = []

        for q in search.quotes:

            ticker = q.get("symbol", "")

            if not ticker:
                continue

            short_name = q.get("shortname", "")
            long_name = q.get("longname", "")

            matched = (
                is_company_match(name, short_name)
                or is_company_match(name, long_name)
            )

            if not matched:
                continue

            print(
                f"🔍 公司名称匹配: {name} -> {ticker}"
            )

            try:

                stock = yf.Ticker(ticker)
                info = stock.info

                sector = info.get("sector", "")

                currency = (
                    info.get("currency")
                    or info.get("financialCurrency")
                    or ""
                )

                # 行业或货币信息缺失：
                # 无法确认是否符合条件，保留重试机会
                if not sector or not currency:

                    query_errors.append(
                        f"{ticker}: 行业或货币信息缺失"
                    )

                    continue

                # 非 Healthcare 或非 USD：
                # 确认不符合筛选条件
                if (
                    sector != "Healthcare"
                    or currency != "USD"
                ):

                    print(
                        f"⏭️ 排除 {ticker}: "
                        f"Sector={sector}, "
                        f"Currency={currency}"
                    )

                    continue

                fast_info = stock.fast_info

                last_price = fast_info.last_price
                market_cap = fast_info.market_cap

                return {
                    "status": "match",
                    "ticker": ticker,
                    "price": (
                        round(last_price, 2)
                        if last_price is not None
                        else 0
                    ),
                    "cap": (
                        round(market_cap / 1e9, 2)
                        if market_cap is not None
                        else 0
                    )
                }

            except Exception as e:

                print(
                    f"⚠️ Yahoo Finance 查询失败 "
                    f"[{ticker}]: {e}"
                )

                query_errors.append(
                    f"{ticker}: {e}"
                )

        # 如果有匹配候选公司查询失败，
        # 不能简单地认定该企业不符合条件
        if query_errors:

            print(
                f"⚠️ 公司检索结果不确定 [{name}]"
            )

            return {
                "status": "error",
                "reason": "; ".join(query_errors)
            }

        return {
            "status": "no_match"
        }

    except Exception as e:

        print(
            f"⚠️ Yahoo Finance 搜索异常 [{name}]: {e}"
        )

        return {
            "status": "error",
            "reason": str(e)
        }


# ============================================================
# FDA 数据获取
# ============================================================

def fetch_fda_records():

    today = date.today()

    start_date = (
        today - timedelta(days=7)
    ).strftime("%m/%d/%Y")

    end_date = today.strftime("%m/%d/%Y")

    payload = {
        "Product_name": "",
        "sponsor_name": "",
        "Designation": "",
        "Designation_Start_Date": start_date,
        "Designation_End_Date": end_date,
        "Search_param": "DESDATE",
        "Output_format": "Excel",
        "Sort_order": "Date_Reverse_Order",
        "RecordsPerPage": "100",
        "newSearch": "Run Search"
    }

    print(
        f"📡 正在获取 FDA 数据: "
        f"{start_date} - {end_date}"
    )

    response = requests.post(
        FDA_URL,
        data=payload,
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    table = soup.find("table")

    if table is None:
        raise RuntimeError(
            "FDA 页面没有找到数据表格，"
            "可能是网页结构发生变化或返回内容异常"
        )

    rows = table.find_all("tr")

    if not rows:
        raise RuntimeError(
            "FDA 页面数据表格为空"
        )

    headers_list = [
        th.get_text(strip=True)
        for th in rows[0].find_all(["th", "td"])
    ]

    if not headers_list:
        raise RuntimeError(
            "FDA 数据表格没有有效表头"
        )

    required_columns = {
        "Sponsor Company",
        "Orphan Designation",
        "Date Designated"
    }

    missing_columns = (
        required_columns - set(headers_list)
    )

    if missing_columns:
        raise RuntimeError(
            "FDA 数据表格缺少必要字段: "
            + ", ".join(missing_columns)
        )

    parsed_records = []

    for row in rows[1:]:

        cells = [
            td.get_text(strip=True)
            for td in row.find_all("td")
        ]

        if len(cells) != len(headers_list):
            continue

        parsed_records.append(
            dict(zip(headers_list, cells))
        )

    print(
        f"✅ FDA 获取完成，解析到 "
        f"{len(parsed_records)} 条记录"
    )

    return parsed_records


# ============================================================
# 去重数据库
# ============================================================

def load_seen_data():

    if not os.path.exists(DB_FILE):
        return set()

    with open(
        DB_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        return set(
            line.strip()
            for line in f
            if line.strip()
        )


def save_seen_data(seen_data):
    """原子写入数据库，避免异常导致文件损坏"""

    directory = os.path.dirname(
        os.path.abspath(DB_FILE)
    )

    fd, temp_path = tempfile.mkstemp(
        dir=directory,
        prefix="seen_orphan_",
        suffix=".tmp"
    )

    try:

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8"
        ) as f:

            for item in sorted(seen_data):
                f.write(f"{item}\n")

        os.replace(
            temp_path,
            DB_FILE
        )

    except Exception:

        if os.path.exists(temp_path):
            os.remove(temp_path)

        raise


def get_unique_key(record):
    """生成稳定的 FDA 记录唯一 ID"""

    cfgridkey = record.get(
        "CF Grid Key",
        ""
    ).strip()

    if cfgridkey:
        return cfgridkey

    # CF Grid Key 缺失时使用组合键，
    # 避免不同公司同名药物发生冲突
    return "|".join([
        record.get("Sponsor Company", "").strip(),
        record.get("Orphan Designation", "").strip(),
        record.get("Date Designated", "").strip()
    ])


# ============================================================
# 主程序
# ============================================================

def run():

    print("=" * 55)
    print("🧬 FDA Orphan Drug Scanner")
    print("=" * 55)

    seen_data = load_seen_data()

    print(
        f"📚 已加载历史记录: {len(seen_data)} 条"
    )

    # 1. 获取 FDA 数据
    parsed_records = fetch_fda_records()

    # 2. 初始化
    translator = GoogleTranslator(
        source="en",
        target="zh-CN"
    )

    records_to_send = []

    # 仅在确认处理完成后加入
    # 查询失败的记录保留到下一次重试
    processed_this_run = set()

    # 3. 遍历 FDA 记录
    for rec in parsed_records:

        applicant = rec.get(
            "Sponsor Company",
            "N/A"
        ).strip()

        drug_en = rec.get(
            "Orphan Designation",
            "N/A"
        ).strip()

        date_desig = rec.get(
            "Date Designated",
            "N/A"
        ).strip()

        unique_key = get_unique_key(rec)

        if not unique_key:
            print(
                "⚠️ 跳过缺少唯一标识的 FDA 记录"
            )
            continue

        if unique_key in seen_data:
            continue

        if unique_key in processed_this_run:
            continue

        if not applicant or applicant == "N/A":
            print(
                f"⚠️ 缺少 Sponsor Company: {unique_key}"
            )
            continue

        print(
            f"\n📋 正在处理: {applicant}"
        )

        # 4. 查询公司
        stock = get_stock_info_smart(applicant)

        time.sleep(0.4)

        # 查询失败：不写入数据库，下次重试
        if stock["status"] == "error":

            print(
                f"🔄 暂不标记，等待下次重试: "
                f"{applicant}"
            )

            continue

        # 查询成功但不符合条件：
        # 标记为已处理，避免重复查询
        if stock["status"] == "no_match":

            print(
                f"⏭️ 未找到符合条件的美股公司: "
                f"{applicant}"
            )

            processed_this_run.add(
                unique_key
            )

            continue

        # 5. 翻译药物名称
        try:

            drug_cn = translator.translate(
                drug_en
            )

            if not drug_cn:
                drug_cn = drug_en

        except Exception as e:

            print(
                f"⚠️ 翻译失败 [{drug_en}]: {e}"
            )

            drug_cn = drug_en

        # 6. 生成 FDA 公告链接
        cfgridkey = rec.get(
            "CF Grid Key",
            ""
        ).strip()

        link = (
            "https://www.accessdata.fda.gov/"
            "scripts/opdlisting/oopd/"
            f"detailedIndex.cfm?cfgridkey={cfgridkey}"
            if cfgridkey
            else FDA_URL
        )

        # 7. 保存推送数据
        records_to_send.append({

            "date": convert_date_to_chinese(
                date_desig
            ),

            "ticker": stock["ticker"],

            "applicant": applicant,

            "drug": drug_cn,

            "cap": stock["cap"],

            "price": stock["price"],

            "link": link,

            "unique_key": unique_key
        })

        print(
            f"✅ 捕获符合条件的公司: "
            f"{stock['ticker']}"
        )

    # 8. 发送 Telegram
    if records_to_send:

        print(
            f"\n📨 准备推送 "
            f"{len(records_to_send)} 条更新"
        )

        blocks = []

        for idx, item in enumerate(
            records_to_send,
            1
        ):

            # HTML 转义，避免特殊字符破坏 Telegram HTML
            ticker = html.escape(
                str(item["ticker"])
            )

            applicant = html.escape(
                str(item["applicant"])
            )

            drug = html.escape(
                str(item["drug"])
            )

            date_cn = html.escape(
                str(item["date"])
            )

            cap = html.escape(
                str(item["cap"])
            )

            price = html.escape(
                str(item["price"])
            )

            link = html.escape(
                str(item["link"]),
                quote=True
            )

            block = (
                f"{idx}. 📅日期: {date_cn}\n"
                f"    🏢公司: ${ticker} "
                f"({applicant})\n"
                f"    💊适应症: {drug}\n"
                f"    💰市值: ${cap}B\n"
                f"    💵股价: ${price}\n"
                f'    🔗<a href="{link}">'
                f"点击查看公告</a>"
            )

            blocks.append(block)

        header = (
            f"<b>🧬 FDA孤儿药认证更新 "
            f"({len(records_to_send)}家上市企业)</b>"
        )

        footer = "\n\n#FDA #OrphanDrug"

        # 发送失败会直接抛出异常，
        # 后续不会更新去重数据库
        send_messages_in_chunks(
            header,
            blocks,
            footer
        )

        print(
            f"✅ Telegram 推送完成: "
            f"{len(records_to_send)} 条更新"
        )

    else:

        print(
            "\n📭 本次没有发现符合条件的新记录"
        )

    # 9. 所有消息成功发送后，
    # 才将成功处理的记录写入数据库
    #
    # 没有匹配到美股公司的记录也会标记；
    # 查询失败的记录不会标记。
    updated_seen_data = (
        seen_data | processed_this_run
    )

    updated_seen_data.update(
        item["unique_key"]
        for item in records_to_send
    )

    save_seen_data(
        updated_seen_data
    )

    print("\n" + "=" * 55)

    print(
        f"✅ 扫描结束"
    )

    print(
        f"📊 FDA 原始记录: {len(parsed_records)}"
    )

    print(
        f"📨 符合条件的推送: {len(records_to_send)}"
    )

    print(
        f"⏭️ 确认不符合条件: "
        f"{len(processed_this_run)}"
    )

    print(
        f"🔄 待重试记录: "
        f"{sum(1 for rec in parsed_records "
        f"if get_unique_key(rec) "
        f"and get_unique_key(rec) not in seen_data "
        f"and get_unique_key(rec) not in processed_this_run "
        f"and not any(item['unique_key'] == get_unique_key(rec) "
        f"for item in records_to_send))}"
    )

    print(
        f"📚 当前去重数据库: "
        f"{len(updated_seen_data)} 条"
    )

    print("=" * 55)


# ============================================================
# 程序入口
# ============================================================

if __name__ == "__main__":

    try:
        run()

    except Exception as e:

        print(
            f"\n🚨 FDA Scanner 运行异常: {e}"
        )

        raise
