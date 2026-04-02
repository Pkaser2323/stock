import os
import re
import twstock
import uvicorn
from fastapi import FastAPI, Request, BackgroundTasks
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

# 載入環境變數
load_dotenv()

app = FastAPI()

# --- 配置區 ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
MY_USER_ID = os.getenv("MY_USER_ID")
# 你原本的持股清單
MY_STOCKS = ['2449', '3380', '2355', '8096', '8028']

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print("❌ 錯誤：找不到環境變數！")
    exit(1)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 功能：查詢股價 ---
def find_stock_id(user_input):
    if user_input.isdigit():
        return user_input
    for sid, info in twstock.codes.items():
        if info.name == user_input:
            return sid
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
                
                # 取得該股票的最後成交時間 (來自交易所數據)
                trade_time = info.get('time', '未知時間')
                
                # 取得價格邏輯
                price_val = rt['latest_trade_price']
                if price_val == '-':
                    price_val = rt['best_bid_price'][0] if rt['best_bid_price'] else "0"
                
                price = float(price_val)
                open_price = float(rt['open']) if rt['open'] != '-' else price
                change_amt = price - open_price
                change_pct = (change_amt / open_price) * 100 if open_price != 0 else 0
                
                icon = "📈" if change_amt > 0 else "📉" if change_amt < 0 else "➖"
                
                # 組裝訊息：加入成交時間
                msg += f"\n📦 {name}({sid})\n💰 今天的股價價是{price:.2f}\n{icon} 漲幅為{change_pct:+.2f}%\n"
            else:
                msg += f"\n❌ {sid} 查詢失敗"
        return msg.strip()
    except Exception as e:
        print(f"查詢出錯: {e}")
        return "⚠️ 股價查詢暫時無法連線"

# --- 背景任務：定時推播 ---
def scheduled_push():
    if MY_USER_ID:
        print("⏰ 執行定時推播...")
        message = get_stock_msg(MY_STOCKS, title="🔔 定時持股推播")
        line_bot_api.push_message(MY_USER_ID, TextSendMessage(text=message))

scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(scheduled_push, 'cron', day_of_week='mon-fri', hour='9-13', minute='*/15')
scheduler.start()

# --- Webhook 路由 ---
@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature")
    body = await request.body()
    body_decode = body.decode("utf-8")

    def run_handler():
        try:
            handler.handle(body_decode, signature)
        except InvalidSignatureError:
            print("⚠️ 簽章驗證失敗")
        except Exception as e:
            print(f"❌ 處理訊息時出錯: {e}")

    background_tasks.add_task(run_handler)
    return "OK"

# --- 訊息處理 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text.strip()
    stock_id = find_stock_id(user_text)
    
    if stock_id:
        reply_msg = get_stock_msg([stock_id], title="🔍 查詢結果")
    else:
        reply_msg = "💡 請輸入股票代號或名稱"
        
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 伺服器啟動成功：http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)