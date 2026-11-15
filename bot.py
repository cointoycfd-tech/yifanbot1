import os
import logging
import sqlite3
import threading
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ==================== 配置区域 ====================
BOT_TOKEN = "9026238096:AAEGGKY914xfHn76q2aeXFqTks_iau_x_QA"
DB_NAME = "exchange_accounting.db"
# ==================================================

# 启动一个简单的 HTTP 服务器来响应 Render 的端口检测，防止超时
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def log_message(self, format, *args):
        pass

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
    # 交易流水表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            biz_type TEXT NOT NULL,          -- 'in' (入款) 或 'out' (下发)
            raw_text TEXT NOT NULL,          -- 原始文本
            fiat_amount REAL DEFAULT 0,      -- 法币金额
            usdt_amount REAL DEFAULT 0,      -- USDT 金额（下发或扣点后U）
            note TEXT,                       -- 备注/经办人
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 全局设置表（存储汇率和点位）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value REAL NOT NULL
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('exchange_rate', 125.0)")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fee_rate', 7.0)")
    conn.commit()
    conn.close()

def get_setting(key, default):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key, value):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

# /start 命令
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rate = get_setting('exchange_rate', 125.0)
    fee = get_setting('fee_rate', 7.0)
    await update.message.reply_text(
        "🤖 **发哥财务记账/下发小助手**\n\n"
        f"⚙️ **当前基础设置**：\n"
        f"- 当前汇率：`{rate}`（指令：`/rate 数字`）\n"
        f"- 当前点位：`{fee}%`（指令：`/fee 数字`）\n\n"
        "📝 **记账格式说明**：\n"
        "• **入款**：直接发算式或金额，例如：`入款 2000000/125*(0.93)` 或 `+ 2000000`，自动累计今日入金。\n"
        "• **下发**：按设置汇率及扣除点位计算实际到账 U，例如：`下发 14880U 财务002` 或直接 `14880U 财务002`。\n\n"
        "📊 **查看报表**：\n"
        "• 发送 `/report` 或 `/今日报表` 生成今日累计入款、下发及未下发汇总。",
        parse_mode="Markdown"
    )

# /rate 设置汇率
async def set_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        current = get_setting('exchange_rate', 125.0)
        await update.message.reply_text(f"当前汇率为：{current}。如需修改，请发送：`/rate 125`", parse_mode="Markdown")
        return
    try:
        new_rate = float(context.args[0])
        set_setting('exchange_rate', new_rate)
        await update.message.reply_text(f"✅ 汇率已成功更新为：`{new_rate}`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ 格式错误，请输入正确的数字，例如：`/rate 125`", parse_mode="Markdown")

# /fee 设置点位
async def set_fee(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        current = get_setting('fee_rate', 7.0)
        await update.message.reply_text(f"当前点位费率为：`{current}%`。如需修改，请发送：`/fee 7`", parse_mode="Markdown")
        return
    try:
        new_fee = float(context.args[0])
        set_setting('fee_rate', new_fee)
        await update.message.reply_text(f"✅ 交易点位费率已成功更新为：`{new_fee}%`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ 格式错误，请输入正确的数字，例如：`/fee 7`", parse_mode="Markdown")

# 核心消息与记账处理（入款、下发智能识别）
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    user_id = update.effective_user.id
    
    current_rate = get_setting('exchange_rate', 125.0)
    current_fee = get_setting('fee_rate', 7.0)
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. 匹配下发格式：如 "下发 14880U 财务002" 或 "14880U 财务002"
    out_match = re.search(r'^(?:下发)?\s*([\d,]+(?:\.\d+)?)\s*U\s*(.*)$', text, re.IGNORECASE)
    if out_match:
        input_usdt = float(out_match.group(1).replace(',', ''))
        note = out_match.group(2).strip() or "无备注"
        
        # 按照设置的费率点位扣除（例如输入下发 14880U，扣除对应点位后或者直接记录下发U数）
        # 此处直接记录输入的下发U金额，或按点位计算实际到账。可根据实际业务需要微调。
        actual_out_usdt = input_usdt * (1 - current_fee / 100.0)
        
        cursor.execute(
            "INSERT INTO transactions (user_id, biz_type, raw_text, fiat_amount, usdt_amount, note) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, 'out', text, 0.0, actual_out_usdt, f"{note} (原额:{input_usdt}U 扣费{current_fee}%)")
        )
        conn.commit()
        conn.close()
        
        await update.message.reply_text(
            f"📤 **下发记账成功**\n"
            f"• 申请下发：`{input_usdt:,.2f} U`\n"
            f"• 扣除点位：`{current_fee}%`\n"
            f"• 实际到账/下发：`{actual_out_usdt:,.2f} U`\n"
            f"• 备注：`{note}`",
            parse_mode="Markdown"
        )
        return

    # 2. 匹配入款格式：支持 "+ 2000000" 或 "入款 2000000..." 或纯算式
    in_clean = text
    if text.startswith('+'):
        in_clean = text[1:].strip()
    elif text.startswith('入款'):
        in_clean = text[2:].strip()

    try:
        if re.match(r'^[\d\.\/\*\+\-\(\)\s]+$', in_clean):
            fiat_val = float(eval(in_clean))
            # 自动根据当前汇率折算 USDT
            usdt_val = fiat_val / current_rate
            
            cursor.execute(
                "INSERT INTO transactions (user_id, biz_type, raw_text, fiat_amount, usdt_amount, note) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, 'in', text, fiat_val, usdt_val, "自动计算入款")
            )
            conn.commit()
            
            # 查询今日累计入金总额
            cursor.execute("""
                SELECT SUM(fiat_amount), SUM(usdt_amount) 
                FROM transactions 
                WHERE biz_type = 'in' AND date(created_at, 'localtime') = date('now', 'localtime')
            """)
            tot = cursor.fetchone()
            total_fiat_today = tot[0] if tot[0] else 0.0
            total_usdt_today = tot[1] if tot[1] else 0.0
            conn.close()
            
            await update.message.reply_text(
                f"📥 **入款记账成功**\n"
                f"• 本笔法币：`{fiat_val:,.2f}`\n"
                f"• 折算 USDT：`{usdt_val:,.2f} U`（汇率：`{current_rate}`）\n"
                "------------------------------------\n"
                f"📈 **今日累计入金**：`{total_fiat_today:,.2f}` 法币\n"
                f"🔄 **今日折算总U**：`{total_usdt_today:,.2f} U`",
                parse_mode="Markdown"
            )
            return
    except Exception:
        pass

    conn.close()
    await update.message.reply_text("❓ 未能识别该格式。入款可直接发金额或算式（如 `2000000` 或 `+2000000`），下发格式如 `下发 14880U 财务002`。")

# /report 与 /今日报表 汇总处理
async def daily_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # 统计今日入款总额
    cursor.execute("""
        SELECT SUM(fiat_amount), SUM(usdt_amount) 
        FROM transactions 
        WHERE biz_type = 'in' AND date(created_at, 'localtime') = date('now', 'localtime')
    """)
    in_res = cursor.fetchone()
    total_in_fiat = in_res[0] if in_res[0] else 0.0
    total_in_usdt = in_res[1] if in_res[1] else 0.0

    # 统计今日下发总额（U）
    cursor.execute("""
        SELECT SUM(usdt_amount) 
        FROM transactions 
        WHERE biz_type = 'out' AND date(created_at, 'localtime') = date('now', 'localtime')
    """)
    out_res = cursor.fetchone()
    total_out_usdt = out_res[0] if out_res[0] else 0.0

    # 计算今日未下发（结余）U = 总入款折算USDT - 总已下发USDT
    pending_usdt = total_in_usdt - total_out_usdt
    conn.close()

    rate = get_setting('exchange_rate', 125.0)
    fee = get_setting('fee_rate', 7.0)

    report_text = (
        f"📊 **【发哥财务今日汇总报表】**\n"
        f"📅 日期：`今日实时`\n"
        f"⚙️ 汇率：`{rate}` | 点位费率：`{fee}%`\n"
        "------------------------------------\n"
        f"📥 **今日累计入款法币**：`{total_in_fiat:,.2f}`\n"
        f"🔄 **折算入款总U**：`{total_in_usdt:,.2f} U`\n"
        "------------------------------------\n"
        f"📤 **今日已下发总U**：`{total_out_usdt:,.2f} U`\n"
        f"⏳ **今日未下发(结余)U**：`{pending_usdt:,.2f} U`\n"
        "------------------------------------\n"
        "💡 随时发送 `/report` 或 `/今日报表` 查看最新数据。"
    )
    await update.message.reply_text(report_text, parse_mode="Markdown")

def main() -> None:
    init_db()
    
    threading.Thread(target=run_server, daemon=True).start()
    logger.info("后台端口监听服务已启动...")

    application = Application.builder().token(BOT_TOKEN).build()

    # 注册指令与消息处理器
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("rate", set_rate))
    application.add_handler(CommandHandler("fee", set_fee))
    application.add_handler(CommandHandler("report", daily_report))
    application.add_handler(CommandHandler("今日报表", daily_report))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🚀 财务记账/下发小助手已成功启动...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
