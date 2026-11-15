import logging
import sqlite3
import re
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ================= 配置区域 =================
BOT_TOKEN = "9026238096:AAEGGKY914xfHn76q2aeXFqTks_iau_x_QA"
DB_NAME = "exchange_accounting.db"
# ===========================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            biz_type TEXT NOT NULL,       -- 'in' (入款) 或 'out' (下发)
            raw_text TEXT NOT NULL,       -- 原始文本或算式
            fiat_amount REAL DEFAULT 0,   -- 法币金额
            usdt_amount REAL DEFAULT 0,   -- USDT 金额
            note TEXT,                    -- 备注/经办人
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY,
            rate REAL DEFAULT 1.0,
            fee_percent REAL DEFAULT 0.0
        )
    """)
    conn.commit()
    conn.close()

def get_settings(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT rate, fee_percent FROM settings WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0], row[1]
    return 125.0, 7.0  # 默认汇率 125，费率 7%

def save_settings(user_id, rate=None, fee=None):
    current_rate, current_fee = get_settings(user_id)
    new_rate = rate if rate is not None else current_rate
    new_fee = fee if fee is not None else current_fee
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO settings (user_id, rate, fee_percent) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET rate = excluded.rate, fee_percent = excluded.fee_percent
    """, (user_id, new_rate, new_fee))
    conn.commit()
    conn.close()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 **U版财务记账/下发小助手**\n\n"
        "⚙️ **基础设置：**\n"
        "• `/rate 125` - 设置当前汇率\n"
        "• `/fee 7` - 设置交易费率(%)\n\n"
        "📝 **记账格式：**\n"
        "• **入款**：直接发算式或金额，例如：`入款 2000000/125*(0.93)` 或 `+ 2000000`\n"
        "• **下发**：`下发 14880U 财务002` 或 `- 14880U 财务002`\n\n"
        "📊 **查看报表：**\n"
        "• `/report` - 生成今日入款、下发及未下发汇总"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def set_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("❌ 请输入汇率，例如：`/rate 125`", parse_mode="Markdown")
        return
    try:
        rate = float(context.args[0])
        save_settings(user_id, rate=rate)
        await update.message.reply_text(f"✅ 汇率已更新为: `{rate}`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ 汇率格式错误，请输入数字。")

async def set_fee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("❌ 请输入费率，例如：`/fee 7` (代表7%)", parse_mode="Markdown")
        return
    try:
        fee = float(context.args[0])
        save_settings(user_id, fee=fee)
        await update.message.reply_text(f"✅ 交易费率已更新为: `{fee}%`", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ 费率格式错误，请输入数字。")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    rate, fee_percent = get_settings(user_id)

    biz_type = None
    content = text

    if text.startswith("入款") or text.startswith("+"):
        biz_type = "in"
        content = text[2:].strip() if text.startswith("入款") else text[1:].strip()
    elif text.startswith("下发") or text.startswith("-"):
        biz_type = "out"
        content = text[2:].strip() if text.startswith("下发") else text[1:].strip()
    else:
        return 

    current_time = datetime.now().strftime("%H:%M:%S")

    if biz_type == "in":
        clean_expr = re.sub(r'[^0-9\.\+\-\*\/\(\)]', '', content)
        try:
            fiat_val = eval(clean_expr)
            calculated_u = fiat_val / rate * (1 - fee_percent / 100)
            
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO transactions (user_id, biz_type, raw_text, fiat_amount, usdt_amount, note) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, 'in', content, fiat_val, calculated_u, "自动计算")
            )
            conn.commit()
            conn.close()

            await update.message.reply_text(
                f"📥 **入款记录成功** [{current_time}]\n"
                f"• 原始算式/金额：`{content}`\n"
                f"• 法币金额：`{fiat_val:,.2f}`\n"
                f"• 折算 U 数(含{fee_percent}%费率)：`{calculated_u:.2f}U`",
                parse_mode="Markdown"
            )
        except Exception:
            await update.message.reply_text("❌ 计算或格式解析失败，请检查算式。例如：`入款 2000000/125*(0.93)`")

    elif biz_type == "out":
        parts = content.split(maxsplit=1)
        u_str = parts[0].upper().replace("U", "")
        note = parts[1] if len(parts) > 1 else "无备注"
        
        try:
            u_val = float(u_str)
            fiat_val = u_val * rate 
            
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO transactions (user_id, biz_type, raw_text, fiat_amount, usdt_amount, note) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, 'out', content, fiat_val, u_val, note)
            )
            conn.commit()
            conn.close()

            await update.message.reply_text(
                f"📤 **下发记录成功** [{current_time}]\n"
                f"• 下发数量：`{u_val:,.2f}U`\n"
                f"• 经办/备注：`{note}`",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("❌ 下发格式错误，例如：`下发 14880U 财务002`")

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rate, fee_percent = get_settings(user_id)
    today_str = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT datetime(created_at, 'localtime'), raw_text, fiat_amount, usdt_amount 
        FROM transactions WHERE user_id = ? AND biz_type = 'in' AND date(created_at, 'localtime') = ?
    """, (user_id, today_str))
    in_rows = cursor.fetchall()

    cursor.execute("""
        SELECT datetime(created_at, 'localtime'), usdt_amount, fiat_amount, note 
        FROM transactions WHERE user_id = ? AND biz_type = 'out' AND date(created_at, 'localtime') = ?
    """, (user_id, today_str))
    out_rows = cursor.fetchall()
    conn.close()

    total_in_fiat = sum([row[2] for row in in_rows])
    total_in_u = sum([row[3] for row in in_rows])
    
    total_out_u = sum([row[1] for row in out_rows])
    total_out_fiat = sum([row[2] for row in out_rows])

    un_out_u = total_in_u - total_out_u

    msg = [f"📊 **今日财务对账报表 ({today_str})**\n"]
    
    msg.append(f"🟢 **今日入款 ({len(in_rows)} 笔)**")
    for dt, raw, fiat, u in in_rows:
        time_part = dt[11:19]
        msg.append(f"`{time_part}` {fiat:,.0f} / {rate} * (0.93) = **{u:.2f}U**")
    
    msg.append("")
    msg.append(f"🔴 **今日下发 ({len(out_rows)} 笔)**")
    for dt, u, fiat, note in out_rows:
        time_part = dt[11:19]
        msg.append(f"`{time_part}` **{u:.0f}U** ({fiat:,.0f}) {note}")

    msg.append("")
    msg.append(f"💰 **总入款**：`{total_in_fiat:,.0f}` (`{total_in_u:.2f}U`)")
    msg.append(f"📈 **汇率**：`{rate}`")
    msg.append(f"🏷️ **交易费率**：`{fee_percent}%`")
    msg.append("-----------------------------------")
    msg.append(f"📥 **应下发**：`{total_in_fiat:,.0f}` | `{total_in_u:.2f}U`")
    msg.append(f"📤 **已下发**：`{total_out_fiat:,.0f}` | `{total_out_u:.2f}U`")
    msg.append(f"⚠️ **未下发**：`{-un_out_u:.2f}U`" if un_out_u < 0 else f"⚠️ **未下发**：`{un_out_u:.2f}U`")

    await update.message.reply_text("\n".join(msg), parse_mode="Markdown")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rate", set_rate))
    app.add_handler(CommandHandler("fee", set_fee))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 财务对账/下发机器人已启动...")
    app.run_polling()

if __name__ == "__main__":
    main()
