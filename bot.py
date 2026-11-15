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

# 初始化数据库（支持动态设置、流水记录）
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
            usdt_amount REAL DEFAULT 0,      -- USDT 金额
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
    # 默认写入汇率 125 和点位 7 (%)
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('exchange_rate', 125.0)")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('fee_rate', 7.0)")
    conn.commit()
    conn.close()

# 获取设置
def get_setting(key, default):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

# 保存设置
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
        "🤖 发哥财务记账/下发小助手\n\n"
        f"⚙️ 当前系统设置：\n"
        f"- 当前汇率：`{rate}` (指令: `/汇率 数字`)\n"
        f"- 交易费率：`{fee}%` (指令: `/点位 数字`)\n\n"
        "📝 记账格式说明：\n"
        "• **入款**：直接发算式、金额或带 `入款` 关键字，例如：`入款 2000000/125*(0.93)` 或 `+ 2000000`\n"
        "• **下发**：发送下发格式，例如：`下发 14880U 财务002`\n\n"
        "📊 报表查看：\n"
        "• 发送 `/今日报表` 生成今日入款、下发及未下发汇总。",
        parse_mode="Markdown"
    )

# 设置汇率
async def set_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        current = get_setting('exchange_rate', 125.0)
        await update.message.reply_text(f"当前汇率为：{current}。如需修改，请发送：/汇率 125")
        return
    try:
        new_rate = float(context.args[0])
        set_setting('exchange_rate', new_rate)
        await update.message.reply_text(f"✅ 汇率已成功更新为：{new_rate}")
    except ValueError:
        await update.message.reply_text("❌ 格式错误，请输入正确的数字，例如：`/汇率 125`", parse_mode="Markdown")

# 设置点位
async def set_fee(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        current = get_setting('fee_rate', 7.0)
        await update.message.reply_text(f"当前费率为：{current}%。如需修改，请发送：/点位 7")
        return
    try:
        new_fee = float(context.args[0])
        set_setting('fee_rate', new_fee)
        await update.message.reply_text(f"✅ 交易费率已成功更新为：{new_fee}%")
    except ValueError:
        await update.message.reply_text("❌ 格式错误，请输入正确的数字，例如：`/点位 7`", parse_mode="Markdown")

# 核心消息与记账处理
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    user_id = update.effective_user.id
    
    current_rate = get_setting('exchange_rate', 125.0)
    current_fee = get_setting('fee_rate', 7.0) # 例如 7% 扣点，实际到账或计算系数可按需调整
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    # 1. 匹配下发格式：如 "下发 14880U 财务002" 或 "14880U 财务002"
    out_match = re.search(r'^(?:下发)?\s*([\d,]+(?:\.\d+)?)\s*U\s*(.*)$', text, re.IGNORECASE)
    if out_match:
        usdt_val = float(out_match.group(1).replace(',', ''))
        note = out_match.group(2).strip() or "无备注"
        
        cursor.execute(
            "INSERT INTO transactions (user_id, biz_type, raw_text, fiat_amount, usdt_amount, note) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, 'out', text, 0.0, usdt_val, note)
        )
        conn.commit()
        conn.close()
        
        await update.message.reply_text(
            f"📤 **下发记账成功**\n"
            f"• 金额：`{usdt_val:,.2f} U`\n"
            f"• 经办/备注：`{note}`",
            parse_mode="Markdown"
        )
        return

    # 2. 匹配入款格式：支持 "+ 2000000" 或 "入款 2000000..." 或纯算式
    in_clean = text
    if text.startswith('+'):
        in_clean = text[1:].strip()
    elif text.startswith('入款'):
        in_clean = text[2:].strip()

    # 尝试直接解析入款法币金额或算式（如 2000000 或带表达式）
    # 如果用户输入了类似 2000000/125*(0.93) 的算式，安全计算出结果
    try:
        # 仅允许数字和基础数学字符，防止恶意代码
        if re.match(r'^[\d\.\/\*\+\-\(\)\s]+$', in_clean):
            fiat_val = float(eval(in_clean))
            # 自动根据当前汇率计算折算 USDT
            usdt_val = fiat_val / current_rate
            
            cursor.execute(
                "INSERT INTO transactions (user_id, biz_type, raw_text, fiat_amount, usdt_amount, note) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, 'in', text, fiat_val, usdt_val, "自动计算入款")
            )
            conn.commit()
            conn.close()
            
            await update.message.reply_text(
                f"📥 **入款记账成功**\n"
                f"• 法币金额：`{fiat_val:,.2f}`\n"
                f"• 当前汇率：`{current_rate}`\n"
                f"• 折算 USDT：`{usdt_val:,.2f} U`",
                parse_mode="Markdown"
            )
            return
    except Exception:
        pass

    conn.close()
    # 如果无法识别，给出提示
    await update.message.reply_text("❓ 未能识别该指令或记账格式。入款可直接发金额/算式（如 `2000000`），下发格式如 `下发 14880U 财务002`。")

# /今日报表 命令
async def daily_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # 统计今日入款总额（法币及折算USDT）
    cursor.execute("""
        SELECT SUM(fiat_amount), SUM(usdt_amount) 
        FROM transactions 
        WHERE biz_type = 'in' AND date(created_at, 'localtime') = date('now', 'localtime')
    """)
    in_res = cursor.fetchone()
    total_in_fiat = in_res[0] if in_res[0] else 0.0
    total_in_usdt = in_res[1] if in_res[1] else 0.0

    # 统计今日下发总额（USDT）
    cursor.execute("""
        SELECT SUM(usdt_amount) 
        FROM transactions 
        WHERE biz_type = 'out' AND date(created_at, 'localtime') = date('now', 'localtime')
    """)
    out_res = cursor.fetchone()
    total_out_usdt = out_res[0] if out_res[0] else 0.0

    # 计算未下发金额 = 总入款折算USDT - 总下发USDT
    pending_usdt = total_in_usdt - total_out_usdt

    conn.close()

    rate = get_setting('exchange_rate', 125.0)
    fee = get_setting('fee_rate', 7.0)

    report_text = (
        f"📊 **【今日财务汇总报表】**\n"
        f"📅 日期：`今日`\n"
        f"⚙️ 参考汇率：`{rate}` | 费率：`{fee}%`\n"
        "------------------------------------\n"
        f"📥 **总入款法币**：`{total_in_fiat:,.2f}`\n"
        f"🔄 **折算总入款USDT**：`{total_in_usdt:,.2f} U`\n"
        "------------------------------------\n"
        f"📤 **总已下发USDT**：`{total_out_usdt:,.2f} U`\n"
        f"⏳ **今日未下发(结余)**：`{pending_usdt:,.2f} U`\n"
        "------------------------------------\n"
        "💡 发送 `/今日报表` 可随时查看最新实时数据。"
    )
    await update.message.reply_text(report_text, parse_mode="Markdown")

def main() -> None:
    init_db()
    
    # 后台独立线程启动 HTTP 端口监听（满足 Render 要求）
    threading.Thread(target=run_server, daemon=True).start()
    logger.info("后台端口监听服务已启动...")

    # 创建 Telegram 机器人应用
    application = Application.builder().token(BOT_TOKEN).build()

    # 注册指令与消息处理器
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("汇率", set_rate))
    application.add_handler(CommandHandler("点位", set_fee))
    application.add_handler(CommandHandler("今日报表", daily_report))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 启动轮询
    logger.info("🚀 财务对账/下发机器人已启动...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
