import os
import logging
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ==================== 配置区域 ====================
BOT_TOKEN = "9026238096:AAEGGKY914xFlHn76q2aeXFqTks_iau_x_QA"
DB_NAME = "exchange_accounting.db"
# ==================================================

# 启动一个简单的 HTTP 服务器来响应 Render 的端口检测，防止超时
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def log_message(self, format, *args):
        pass # 屏蔽 HTTP 访问日志，保持控制台清爽

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# 配置日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# 初始化数据库
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            biz_type TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            fiat_amount REAL DEFAULT 0,
            usdt_amount REAL DEFAULT 0,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

# /start 命令
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_html(
        f"你好 {user.mention_html()}！👋\n\n"
        "我是你的 USDT/法币财务对账及记账机器人。\n"
        "你可以直接发送交易明细给我进行记录。"
    )

# 消息处理
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    await update.message.reply_text(f"收到你的记账/对账内容：\n{text}")

def main() -> None:
    init_db()
    
    # 在后台独立线程中启动端口监听服务（满足 Render 要求）
    threading.Thread(target=run_server, daemon=True).start()
    logger.info("后台端口监听服务已启动...")

    # 创建 Telegram 机器人应用
    application = Application.builder().token(BOT_TOKEN).build()

    # 注册命令和消息处理器
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 启动机器人轮询
    logger.info("🚀 财务对账/下发机器人已启动...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
