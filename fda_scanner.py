
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

# FDA 请求配置
REQUEST_CONNECT_TIMEOUT = 15
REQUEST_READ_TIMEOUT = 90

# 最多请求次数（包含第一次）
FDA_MAX_RETRIES = 4

# 重试等待时间（秒）
FDA_RETRY_DELAYS = [5, 15, 30]

# 可重试的 HTTP 状态码
FDA_RETRY_STATUS_CODES = {
    429, 500, 502, 503, 504
}

# Telegram 消息长度预留空间
TELEGRAM_MAX_LENGTH = 3000


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
                f"Telegram 推送成功: {chat_id}"
            )

        except Exception as e:

            print(
                f"Telegram 推送失败 [{chat_id}]: {e}"
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

        candidate_blocks = (
            current_blocks + [block]
        )

        candidate = (
            header
            + "\n\n"
            + "\n\n---------------\n\n".join(
                candidate_blocks
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
            message += (
                f"\n\n<i>消息分段: "
                f"{index}/{len(messages)}</i>"
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
        查询过程中出现异常，下次重新尝试。
    """

    try:

        core_words = normalize_name(name)

        if not core_words:
            return {
                "status": "no_match"
            }

        search_q = " ".join(
            core_words[:2]
        )

        print(
            f"Yahoo Finance 搜索: {search_q}"
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

            short_name = q.get(
                "shortname", ""
            )

            long_name = q.get(
                "longname", ""
            )

            matched = (
                is_company_match(
                    name,
                    short_name
                )
                or is_company_match(
                    name,
                    long_name
                )
            )

            if not matched:
                continue

            print(
                f"公司名称匹配: {name} -> {ticker}"
            )

            try:

                stock = yf.Ticker(ticker)

                info = stock.info

                sector = info.get(
                    "sector", ""
                )

                currency = (
                    info.get("currency")
                    or info.get("financialCurrency")
                    or ""
                )

                # 信息缺失时不能确认是否符合条件
                if not sector or not currency:

                    query_errors.append(
                        f"{ticker}: 行业或货币信息缺失"
                    )

                    continue

                # 非 Healthcare 或非 USD
                if (
                    sector != "Healthcare"
                    or currency != "USD"
                ):

                    print(
                        f"排除 {ticker}: "
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
                    f"Yahoo Finance 查询失败 "
                    f"[{ticker}]: {e}"
                )

                query_errors.append(
                    f"{ticker}: {e}"
                )

        # 候选公司查询失败，保留重试机会
        if query_errors:

            print(
                f"公司检索结果不确定 [{name}]"
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
            f"Yahoo Finance 搜索异常 [{name}]: {e}"
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
        f"正在获取 FDA 数据: "
        f"{start_date} - {end_date}"
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Referer": FDA_URL,
        "Connection": "close"
    }

    response = None
    last_error = None

    # ==================================================
    # FDA 请求与重试
    # ==================================================

    with requests.Session() as session:

        for attempt in range(1, FDA_MAX_RETRIES + 1):

            try:

                print(
                    f"FDA 请求第 {attempt}/"
                    f"{FDA_MAX_RETRIES} 次..."
                )

                response = session.post(
                    FDA_URL,
                    data=payload,
                    headers=headers,
                    timeout=(
                        REQUEST_CONNECT_TIMEOUT,
                        REQUEST_READ_TIMEOUT
                    )
                )

                # HTTP 状态码检查
                if response.status_code in FDA_RETRY_STATUS_CODES:

                    raise requests.exceptions.HTTPError(
                        f"FDA HTTP {response.status_code}"
                    )

                response.raise_for_status()

                # 检查响应内容
                if not response.content:

                    raise RuntimeError(
                        "FDA 返回空响应"
                    )

                # 检查是否为 HTML
                content_type = response.headers.get(
                    "Content-Type", ""
                ).lower()

                if (
                    "html" not in content_type
                    and "text" not in content_type
                ):

                    raise RuntimeError(
                        f"FDA 返回异常 Content-Type: "
                        f"{content_type}"
                    )

                print(
                    f"FDA 请求成功，HTTP "
                    f"{response.status_code}，"
                    f"响应大小: "
                    f"{len(response.content)} bytes"
                )

                break

            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.HTTPError
            ) as e:

                last_error = e

                print(
                    f"FDA 请求失败 "
                    f"[{attempt}/{FDA_MAX_RETRIES}]: {e}"
                )

                if attempt >= FDA_MAX_RETRIES:
                    break

                # 对 HTTP 错误进行区分
                if isinstance(
                    e,
                    requests.exceptions.HTTPError
                ):

                    status_code = (
                        e.response.status_code
                        if e.response is not None
                        else None
                    )

                    if (
                        status_code is not None
                        and status_code not in FDA_RETRY_STATUS_CODES
                    ):

                        raise RuntimeError(
                            f"FDA 返回不可重试的 HTTP "
                            f"错误: {status_code}"
                        ) from e

                delay = FDA_RETRY_DELAYS[
                    min(
                        attempt - 1,
                        len(FDA_RETRY_DELAYS) - 1
                    )
                ]

                print(
                    f"等待 {delay} 秒后重试..."
                )

                time.sleep(delay)

    # 所有请求均失败
    if response is None:

        raise RuntimeError(
            f"FDA 数据获取失败，已重试 "
            f"{FDA_MAX_RETRIES} 次。"
            f"最后错误: {last_error}"
        ) from last_error

    # ==================================================
    # HTML 解析
    # ==================================================

    soup = BeautifulSoup(
        response.content,
        "html.parser"
    )

    table = soup.find("table")

    if table is None:

        raise RuntimeError(
            "FDA 页面没有找到数据表格，"
            "可能是网页结构变化、反爬拦截"
            "或服务器返回异常页面"
        )

    rows = table.find_all("tr")

    if not rows:

        raise RuntimeError(
            "FDA 页面数据表格为空"
        )

    headers_list = [
        th.get_text(strip=True)
        for th in rows[0].find_all(
            ["th", "td"]
        )
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
        f"FDA 获取完成，解析到 "
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

    # CF Grid Key 缺失时使用组合键
    return "|".join([
        record.get(
            "Sponsor Company", ""
        ).strip(),

        record.get(
            "Orphan Designation", ""
        ).strip(),

        record.get(
            "Date Designated", ""
        ).strip()
    ])


# ============================================================
# 主程序
# ============================================================

def run():

    print("=" * 55)
    print("FDA Orphan Drug Scanner")
    print("=" * 55)

    # 1. 加载历史数据库
    seen_data = load_seen_data()

    print(
        f"已加载历史记录: {len(seen_data)} 条"
    )

    # 2. 获取 FDA 数据
    parsed_records = fetch_fda_records()

    # 3. 初始化
    translator = GoogleTranslator(
        source="en",
        target="zh-CN"
    )

    records_to_send = []

    # 确认成功处理的非上市公司记录
    processed_this_run = set()

    # Yahoo Finance 查询失败的记录数
    retry_count = 0

    # 4. 遍历 FDA 记录
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
                "跳过缺少唯一标识的 FDA 记录"
            )
            continue

        if unique_key in seen_data:
            continue

        if unique_key in processed_this_run:
            continue

        if not applicant or applicant == "N/A":

            print(
                f"缺少 Sponsor Company: {unique_key}"
            )

            continue

        print(
            f"\n正在处理: {applicant}"
        )

        # 5. 查询公司
        stock = get_stock_info_smart(
            applicant
        )

        time.sleep(0.4)

        # 查询失败：不写入数据库，下次重试
        if stock["status"] == "error":

            retry_count += 1

            print(
                f"暂不标记，等待下次重试: "
                f"{applicant}"
            )

            continue

        # 查询成功但不符合条件
        if stock["status"] == "no_match":

            print(
                f"未找到符合条件的美股公司: "
                f"{applicant}"
            )

            processed_this_run.add(
                unique_key
            )

            continue

        # 6. 翻译药物名称
        try:

            drug_cn = translator.translate(
                drug_en
            )

            if not drug_cn:
                drug_cn = drug_en

        except Exception as e:

            print(
                f"翻译失败 [{drug_en}]: {e}"
            )

            drug_cn = drug_en

        # 7. 生成 FDA 公告链接
        cfgridkey = rec.get(
            "CF Grid Key",
            ""
        ).strip()

        if cfgridkey:

            link = (
                "https://www.accessdata.fda.gov/"
                "scripts/opdlisting/oopd/"
                "detailedIndex.cfm"
                f"?cfgridkey={cfgridkey}"
            )

        else:
            link = FDA_URL

        # 8. 保存推送数据
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
            f"捕获符合条件的公司: "
            f"{stock['ticker']}"
        )

    # 9. 发送 Telegram
    if records_to_send:

        print(
            f"\n准备推送 "
            f"{len(records_to_send)} 条更新"
        )

        blocks = []

        for idx, item in enumerate(
            records_to_send,
            1
        ):

            # HTML 转义
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

        # 如果发送失败，抛出异常
        # 不会继续更新去重数据库
        send_messages_in_chunks(
            header,
            blocks,
            footer
        )

        print(
            f"Telegram 推送完成: "
            f"{len(records_to_send)} 条更新"
        )

    else:

        print(
            "\n本次没有发现符合条件的新记录"
        )

    # 10. 更新去重数据库
    #
    # 确认不符合条件的记录：写入
    # 符合条件且推送成功的记录：写入
    # 查询失败的记录：不写入，等待下次重试

    updated_seen_data = (
        seen_data | processed_this_run
    )

    for item in records_to_send:

        updated_seen_data.add(
            item["unique_key"]
        )

    save_seen_data(
        updated_seen_data
    )

    # 11. 运行统计
    print("\n" + "=" * 55)

    print("扫描结束")

    print(
        f"FDA 原始记录: {len(parsed_records)}"
    )

    print(
        f"符合条件的推送: {len(records_to_send)}"
    )

    print(
        f"确认不符合条件: {len(processed_this_run)}"
    )

    print(
        f"待重试记录: {retry_count}"
    )

    print(
        f"当前去重数据库: "
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
            f"\nFDA Scanner 运行异常: {e}"
        )

        raise
