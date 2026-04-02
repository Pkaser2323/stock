import os
import twstock
import uvicorn
from datetime import datetime
from fastapi import FastAPI, Request, BackgroundTasks
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()

# --- 配置區 ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
MY_USER_ID = os.getenv("MY_USER_ID")
MY_STOCKS = ['2449', '3380', '2355', '8096', '8028']

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

def find_stock_id(user_input):
    if user_input.isdigit(): return user_input
    for sid, info in twstock.codes.items():
        if info.name == user_input: return sid
    return None

def get_stock_msg(stock_ids, title="📊 股價回報"):
    try:
        all_data = twstock.realtime.get(stock_ids)
        msg = f"{title}\n"
        for sid in stock_ids:
            data = all_data.get(sid)
            if data and data['success']:
                rt = data['realtime']
                info = data['info']
                name = info['name']
                trade_time = info.get('time', '未知時間')
                
                # 取得成交價
                price_val = rt['latest_trade_price']
                if price_val == '-':
                    price_val = rt['best_bid_price'][0] if rt['best_bid_price'] else "0"
                price = float(price_val)
                
                # 計算漲跌幅
                open_price = float(rt['open']) if rt['open'] != '-' else price
                change_amt = price - open_price
                change_pct = (change_amt / open_price) * 100 if open_price != 0 else 0
                
                # 設定狀態文字與符號
                if change_amt > 0:
                    status = "今天上漲"
                    icon = "📈"
                elif change_amt < 0:
                    status = "今天下跌"
                    icon = "📉"
                else:
                    status = "今天平盤"
                    icon = "➖"
                
                # 重新組合訊息格式
                msg += f"\n📦 {name}({sid})\n{icon} {status} {abs(change_pct):.2f}%\n💰 現在股價為 {price:.2f}\n🕒 時間: {trade_time}\n"
            else:
                msg += f"\n❌ {sid} 查詢失敗"
        return msg.strip()
    except Exception as e:
        print(f"Error: {e}")
        return "⚠️ 股價查詢暫時無法連線"

# --- 新增：iOS 捷徑專用 URL ---
@app.get("/siri/{stock_query}")
async def siri_query(stock_query: str):
    stock_id = find_stock_id(stock_query)
    if stock_id:
        result = get_stock_msg([stock_id], title="🚀 語音報價")
        # 移除換行讓 Siri 唸起來更順暢
        return {"text": result.replace("\n", " ")}
    return {"text": f"找不到 {stock_query}"}

# --- LINE Webhook ---
@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature")
    body = await request.body()
    body_decode = body.decode("utf-8")
    def run_handler():
        try:
            handler.handle(body_decode, signature)
        except Exception as e:
            print(f"Error: {e}")
    background_tasks.add_task(run_handler)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text.strip()
    stock_id = find_stock_id(user_text)
    if stock_id:
        reply_msg = get_stock_msg([stock_id])
    else:
        reply_msg = "💡 請輸入股票代號或名稱"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))

# --- 定時任務 ---
scheduler = BackgroundScheduler(timezone="Asia/Taipei")
@scheduler.scheduled_job('cron', day_of_week='mon-fri', hour='9-13', minute='*/15')
def scheduled_push():
    if MY_USER_ID:
        line_bot_api.push_message(MY_USER_ID, TextSendMessage(text=get_stock_msg(MY_STOCKS, "🔔 定時持股推播")))

scheduler.start()

if __name__ == "__main__":
    # Zeabur 分配 PORT
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)