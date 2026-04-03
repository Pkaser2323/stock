import os
import time
from datetime import datetime, timedelta, timezone

import requests
import twstock
import urllib3
import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InsecureRequestWarning
from urllib3.util.retry import Retry

load_dotenv()
app = FastAPI()

# --- 配置區 ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
MY_USER_ID = os.getenv("MY_USER_ID")
MY_STOCKS = ["2449", "3380", "8096"]
HTTP_TIMEOUT_SECONDS = 8
tracked_stocks = list(MY_STOCKS)
TW_TZ = timezone(timedelta(hours=8))

handler = WebhookHandler(LINE_CHANNEL_SECRET)
line_configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
urllib3.disable_warnings(InsecureRequestWarning)


def find_stock_id(user_input):
    if user_input.isdigit():
        return user_input
    for sid, info in twstock.codes.items():
        if info.name == user_input:
            return sid
    return None


def list_tracked_stocks_text():
    if not tracked_stocks:
        return "目前清單是空的"

    lines = ["目前追蹤清單："]
    for sid in tracked_stocks:
        stock_name = twstock.codes[sid].name if sid in twstock.codes else "未知名稱"
        lines.append(f"{sid} {stock_name}")
    return "\n".join(lines)


def handle_stock_command(user_text):
    tokens = user_text.split()
    command = tokens[0].lower()
    args = tokens[1:]

    if command == "/list":
        return list_tracked_stocks_text()

    if command not in {"/add", "/del"}:
        return None

    if not args:
        return "請提供股票代號或名稱"

    resolved_ids = []
    unknown_items = []
    for item in args:
        stock_id = find_stock_id(item.strip())
        if stock_id:
            resolved_ids.append(stock_id)
        else:
            unknown_items.append(item)

    if command == "/add":
        added = []
        already_exists = []
        for sid in resolved_ids:
            if sid in tracked_stocks:
                already_exists.append(sid)
            else:
                tracked_stocks.append(sid)
                added.append(sid)

        reply_parts = []
        if added:
            reply_parts.append(f"已加入：{', '.join(added)}")
        if already_exists:
            reply_parts.append(f"已存在：{', '.join(already_exists)}")
        if unknown_items:
            reply_parts.append(f"找不到：{', '.join(unknown_items)}")
        reply_parts.append(list_tracked_stocks_text())
        return "\n".join(reply_parts)

    removed = []
    not_in_list = []
    for sid in resolved_ids:
        if sid in tracked_stocks:
            tracked_stocks.remove(sid)
            removed.append(sid)
        else:
            not_in_list.append(sid)

    reply_parts = []
    if removed:
        reply_parts.append(f"已移除：{', '.join(removed)}")
    if not_in_list:
        reply_parts.append(f"清單中沒有：{', '.join(not_in_list)}")
    if unknown_items:
        reply_parts.append(f"找不到：{', '.join(unknown_items)}")
    reply_parts.append(list_tracked_stocks_text())
    return "\n".join(reply_parts)


def _build_http_session(verify_ssl=True):
    session = requests.Session()
    session.verify = verify_ssl
    no_retry = Retry(total=0, connect=0, read=0, redirect=0, status=0)
    adapter = HTTPAdapter(max_retries=no_retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _twstock_get_raw_once(stock_ids, verify_ssl=True):
    session = _build_http_session(verify_ssl=verify_ssl)
    session.get(
        twstock.realtime.SESSION_URL,
        proxies=twstock.proxy.get_proxies(),
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response = session.get(
        twstock.realtime.STOCKINFO_URL.format(
            stock_id=twstock.realtime._join_stock_id(stock_ids),
            time=int(time.time()) * 1000,
        ),
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    return response.json()


def safe_realtime_get(stock_ids):
    try:
        data = _twstock_get_raw_once(stock_ids, verify_ssl=True)
    except requests.exceptions.SSLError:
        print("TWSE SSL verify failed, fallback to verify=False once.")
        data = _twstock_get_raw_once(stock_ids, verify_ssl=False)
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": str(e)}

    if data.get("rtcode") == "5000":
        return {"success": False, "error": "json decode error"}
    if "msgArray" not in data:
        return {"success": False, "error": data.get("rtmessage", "No msgArray")}
    if not data["msgArray"]:
        return {"success": False, "error": "Empty Query."}

    result = {
        stock["info"]["code"]: stock
        for stock in map(twstock.realtime._format_stock_info, data["msgArray"])
    }
    result["success"] = True
    return result


def is_market_open_today():
    if not tracked_stocks:
        return False

    probe_id = tracked_stocks[0]
    probe_data = safe_realtime_get([probe_id])
    if not probe_data.get("success"):
        return False

    stock_data = probe_data.get(probe_id)
    if not stock_data or not stock_data.get("success"):
        return False

    info = stock_data.get("info", {})
    realtime = stock_data.get("realtime", {})
    trade_date = (
        info.get("date")
        or realtime.get("trade_date")
        or realtime.get("latest_trade_date")
    )
    if trade_date:
        today_tw = datetime.now(TW_TZ).strftime("%Y-%m-%d")
        return trade_date.replace("/", "-") == today_tw

    return realtime.get("latest_trade_price", "-") != "-"


def reply_line_message(reply_token, text):
    with ApiClient(line_configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )


def push_line_message(user_id, text):
    with ApiClient(line_configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=text)],
            )
        )


def get_stock_msg(stock_ids, title="現在時間"):
    try:
        all_data = safe_realtime_get(stock_ids)
        if not all_data.get("success"):
            return "股價查詢暫時無法連線"

        now_str = datetime.now(TW_TZ).strftime("%H:%M")
        lines = [f"{title} {now_str}"]

        for sid in stock_ids:
            data = all_data.get(sid)
            if data and data["success"]:
                rt = data["realtime"]
                info = data["info"]
                name = info["name"]

                price_val = rt["latest_trade_price"]
                if price_val == "-":
                    price_val = rt["best_bid_price"][0] if rt["best_bid_price"] else "0"
                price = float(price_val)

                open_price = float(rt["open"]) if rt["open"] != "-" else price
                change_amt = price - open_price
                change_pct = (change_amt / open_price) * 100 if open_price != 0 else 0

                if change_amt > 0:
                    trend = "上漲"
                    prefix = "+"
                elif change_amt < 0:
                    trend = "下跌"
                    prefix = ""
                else:
                    trend = "平盤"
                    prefix = ""

                lines.append(
                    f"{name} {sid} 股價為{price:.2f} {trend} {prefix}{change_pct:.2f}%"
                )
            else:
                lines.append(f"{sid} 查詢失敗")

        return "\n".join(lines)
    except Exception as e:
        print(f"Error: {e}")
        return "股價查詢暫時無法連線"


@app.get("/siri/{stock_query}")
async def siri_query(stock_query: str):
    stock_id = find_stock_id(stock_query)
    if stock_id:
        result = get_stock_msg([stock_id], title="語音報價")
        return {"text": result.replace("\n", " ")}
    return {"text": f"找不到 {stock_query}"}


@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    body_decode = body.decode("utf-8")

    def run_handler():
        try:
            handler.handle(body_decode, signature)
        except InvalidSignatureError:
            print("Invalid LINE signature.")
        except Exception as e:
            print(f"Error: {e}")

    background_tasks.add_task(run_handler)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text.strip()
    if user_text.startswith("/"):
        command_result = handle_stock_command(user_text)
        if command_result:
            reply_line_message(event.reply_token, command_result)
            return

    stock_id = find_stock_id(user_text)
    if stock_id:
        reply_msg = get_stock_msg([stock_id])
    else:
        reply_msg = "請輸入股票代號或名稱"
    reply_line_message(event.reply_token, reply_msg)


scheduler = BackgroundScheduler(timezone="Asia/Taipei")


@scheduler.scheduled_job("cron", day_of_week="mon-fri", hour="9-13", minute="*/15")
def scheduled_push():
    if not (MY_USER_ID and tracked_stocks):
        return
    if not is_market_open_today():
        print("Market closed today, skip scheduled push.")
        return
    push_line_message(MY_USER_ID, get_stock_msg(tracked_stocks, "定時持股推播"))


scheduler.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)