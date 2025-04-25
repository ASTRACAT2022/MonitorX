import asyncio
import sqlite3
import re
import random
import os
import gc
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from transformers import pipeline
from flask import Flask, render_template, request, jsonify
import threading
from datetime import datetime, timedelta
from uuid import uuid4

# Инициализация
TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_BOT_TOKEN')
classifier = pipeline('text-classification', model='google/mobilebert-uncased', device=-1)
app = Flask(__name__)

# База данных
DB_PATH = '/app/chat_filter.db' if os.getenv('RENDER') else 'chat_filter.db'
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
                    chat_id INTEGER PRIMARY KEY,
                    sensitivity REAL DEFAULT 0.7,
                    welcome_enabled INTEGER DEFAULT 1,
                    captcha_enabled INTEGER DEFAULT 1,
                    captcha_timeout INTEGER DEFAULT 120,
                    captcha_type TEXT DEFAULT 'button',
                    spam_keywords TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS captcha (
                    chat_id INTEGER,
                    user_id INTEGER,
                    status INTEGER DEFAULT 0,
                    answer TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS bans (
                    chat_id INTEGER,
                    user_id INTEGER,
                    reason TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id))''')
conn.commit()

# Проверка на спам
def is_spam(text, chat_id):
    cursor.execute('SELECT spam_keywords FROM settings WHERE chat_id = ?', (chat_id,))
    keywords = cursor.fetchone()
    keywords = keywords[0].split(',') if keywords and keywords[0] else ['заработай миллионы', 'быстрые деньги']
    for keyword in keywords:
        if keyword.strip() and re.search(keyword.lower(), text.lower()):
            return True
    result = classifier(text)[0]
    score = result['score'] if result['label'] == 'NEGATIVE' else 1 - result['score']
    cursor.execute('SELECT sensitivity FROM settings WHERE chat_id = ?', (chat_id,))
    sensitivity = cursor.fetchone()
    sensitivity = sensitivity[0] if sensitivity else 0.7
    return score > sensitivity

# Генерация вопроса для CAPTCHA
def generate_captcha_question():
    a = random.randint(1, 10)
    b = random.randint(1, 10)
    question = f"{a} + {b} = ?"
    answer = str(a + b)
    return question, answer

# Приветственное сообщение и CAPTCHA (только для групп)
async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    if update.effective_chat.type != 'group' and update.effective_chat.type != 'supergroup':
        return  # CAPTCHA только для групп
    cursor.execute('SELECT welcome_enabled, captcha_enabled, captcha_timeout, captcha_type FROM settings WHERE chat_id = ?', (chat_id,))
    settings = cursor.fetchone()
    welcome_enabled = settings[0] if settings else 1
    captcha_enabled = settings[1] if settings else 1
    captcha_timeout = settings[2] if settings else 120
    captcha_type = settings[3] if settings else 'button'
    welcome = f"Добро пожаловать, {user.first_name}!"

    if welcome_enabled and captcha_enabled:
        if captcha_type == 'button':
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("Я не бот", callback_data=f'captcha_{user.id}_button')]])
            await context.bot.send_message(chat_id, f"{welcome}\nНажмите кнопку в течение {captcha_timeout} секунд:", reply_markup=markup)
            cursor.execute('INSERT OR REPLACE INTO captcha (chat_id, user_id, status, answer) VALUES (?, ?, 0, ?)', (chat_id, user.id, 'button'))
            conn.commit()
        else:
            question, answer = generate_captcha_question()
            await context.bot.send_message(chat_id, f"{welcome}\nОтветьте на вопрос в течение {captcha_timeout} секунд:\n{question}")
            cursor.execute('INSERT OR REPLACE INTO captcha (chat_id, user_id, status, answer) VALUES (?, ?, 0, ?)', (chat_id, user.id, answer))
            conn.commit()
        context.job_queue.run_once(check_captcha_timeout, captcha_timeout, data={'chat_id': chat_id, 'user_id': user.id})

# Проверка тайм-аута CAPTCHA
async def check_captcha_timeout(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data['chat_id']
    user_id = job.data['user_id']
    cursor.execute('SELECT status FROM captcha WHERE chat_id = ? AND user_id = ?', (chat_id, user_id))
    status = cursor.fetchone()
    if status and status[0] == 0:
        try:
            await context.bot.ban_chat_member(chat_id, user_id)
            await context.bot.send_message(chat_id, f"Пользователь (ID: {user_id}) удалён за непрохождение CAPTCHA.")
            cursor.execute('INSERT INTO bans (chat_id, user_id, reason) VALUES (?, ?, ?)', (chat_id, user_id, 'Не пройдена CAPTCHA'))
            cursor.execute('DELETE FROM captcha WHERE chat_id = ? AND user_id = ?', (chat_id, user_id))
            conn.commit()
        except Exception as e:
            print(f"Ошибка при бане: {e}")

# Обработчик новых участников (только для групп)
async def new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ['group', 'supergroup']:
        return
    for member in update.message.new_chat_members:
        await send_welcome(update, context)

# Обработчик сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    text = update.message.text
    chat_type = update.effective_chat.type

    # Проверка CAPTCHA (только для групп)
    if chat_type in ['group', 'supergroup']:
        cursor.execute('SELECT status, answer FROM captcha WHERE chat_id = ? AND user_id = ?', (chat_id, user_id))
        captcha_status = cursor.fetchone()
        if captcha_status and captcha_status[0] == 0:
            if captcha_status[1] != 'button':
                if text.strip() == captcha_status[1]:
                    cursor.execute('UPDATE captcha SET status = 1 WHERE chat_id = ? AND user_id = ?', (chat_id, user_id))
                    conn.commit()
                    await update.message.reply_text("CAPTCHA пройдена!")
                    return
                else:
                    await update.message.delete()
                    await context.bot.send_message(chat_id, f"{username}, неверный ответ!")
                    return
            await update.message.delete()
            await context.bot.send_message(chat_id, f"{username}, сначала пройдите CAPTCHA!")
            return

    # Проверка на спам (для всех типов чатов)
    if is_spam(text, chat_id):
        if chat_type in ['group', 'supergroup', 'channel']:
            await update.message.delete()
            await context.bot.send_message(chat_id, f"Сообщение от {username} удалено как спам.")
        else:  # Личный чат
            await update.message.reply_text("Это сообщение похоже на спам.")
        gc.collect()  # Очистка памяти
    else:
        if chat_type == 'private':
            await update.message.reply_text("Отправьте /start для управления ботом.")

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    is_admin_user = await is_admin(update, context) if chat_type in ['group', 'supergroup', 'channel'] else False
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Настройки", callback_data='settings')] if is_admin_user else [],
        [InlineKeyboardButton("Помощь", callback_data='help')]
    ])
    if chat_type == 'private':
        await update.message.reply_text("Привет! Я бот для модерации. Используй кнопки или команды.", reply_markup=markup)
    else:
        await update.message.reply_text("Бот для модерации чата. Используй кнопки для управления.", reply_markup=markup)

# Команда /rules
async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cursor.execute('SELECT spam_keywords FROM settings WHERE chat_id = ?', (chat_id,))
    rules_text = cursor.fetchone()
    rules_text = rules_text[0] if rules_text and rules_text[0] else "Правила не установлены."
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='back')]])
    await update.message.reply_text(f"Правила чата:\n{rules_text}", reply_markup=markup)

# Проверка админских прав
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    chat_type = update.effective_chat.type
    if chat_type == 'private':
        return False
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        return any(admin.user.id == user_id for admin in admins)
    except:
        return False

# Обработчик кнопок
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat.id
    data = query.data
    user_id = query.from_user.id
    chat_type = query.message.chat.type

    if data == 'back':
        is_admin_user = await is_admin(update, context) if chat_type in ['group', 'supergroup', 'channel'] else False
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Настройки", callback_data='settings')] if is_admin_user else [],
            [InlineKeyboardButton("Помощь", callback_data='help')]
        ])
        await query.edit_message_text("Выберите действие:", reply_markup=markup)

    elif data == 'help':
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='back')]])
        help_text = "Я бот для модерации чатов и каналов. Доступные команды:\n/start - Начать\n/rules - Правила\nАдмины могут настраивать фильтры и CAPTCHA."
        await query.edit_message_text(help_text, reply_markup=markup)

    elif data == 'settings':
        if not await is_admin(update, context):
            await query.answer("Только админы могут управлять настройками!")
            return
        cursor.execute('SELECT sensitivity, welcome_enabled, captcha_enabled FROM settings WHERE chat_id = ?', (chat_id,))
        settings = cursor.fetchone()
        sensitivity, welcome_enabled, captcha_enabled = settings if settings else (0.7, 1, 1 if chat_type in ['group', 'supergroup'] else 0)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Фильтр спама", callback_data='sensitivity')],
            [InlineKeyboardButton(f"Приветствия: {'Вкл' if welcome_enabled else 'Выкл'}", callback_data='toggle_welcome')] if chat_type in ['group', 'supergroup'] else [],
            [InlineKeyboardButton(f"CAPTCHA: {'Вкл' if captcha_enabled else 'Выкл'}", callback_data='toggle_captcha')] if chat_type in ['group', 'supergroup'] else [],
            [InlineKeyboardButton("Настройка CAPTCHA", callback_data='captcha_settings')] if chat_type in ['group', 'supergroup'] else [],
            [InlineKeyboardButton("Назад", callback_data='back')]
        ])
        await query.edit_message_text("Настройки:", reply_markup=markup)

    elif data == 'sensitivity':
        if not await is_admin(update, context):
            await query.answer("Только админы могут менять настройки!")
            return
        cursor.execute('SELECT sensitivity FROM settings WHERE chat_id = ?', (chat_id,))
        sensitivity = cursor.fetchone()
        sensitivity = sensitivity[0] if sensitivity else 0.7
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Увеличить", callback_data='increase_sensitivity'),
             InlineKeyboardButton("Уменьшить", callback_data='decrease_sensitivity')],
            [InlineKeyboardButton("Назад", callback_data='settings')]
        ])
        await query.edit_message_text(f"Чувствительность: {sensitivity:.2f}", reply_markup=markup)

    elif data in ['increase_sensitivity', 'decrease_sensitivity']:
        if not await is_admin(update, context):
            await query.answer("Только админы могут менять настройки!")
            return
        cursor.execute('SELECT sensitivity FROM settings WHERE chat_id = ?', (chat_id,))
        sensitivity = cursor.fetchone()
        sensitivity = sensitivity[0] if sensitivity else 0.7
        if data == 'increase_sensitivity':
            sensitivity = min(sensitivity + 0.1, 1.0)
        else:
            sensitivity = max(sensitivity - 0.1, 0.0)
        cursor.execute('INSERT OR REPLACE INTO settings (chat_id, sensitivity) VALUES (?, ?)', (chat_id, sensitivity))
        conn.commit()
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Увеличить", callback_data='increase_sensitivity'),
             InlineKeyboardButton("Уменьшить", callback_data='decrease_sensitivity')],
            [InlineKeyboardButton("Назад", callback_data='settings')]
        ])
        await query.edit_message_text(f"Чувствительность: {sensitivity:.2f}", reply_markup=markup)

    elif data == 'toggle_welcome':
        if not await is_admin(update, context) or chat_type not in ['group', 'supergroup']:
            await query.answer("Недоступно!")
            return
        cursor.execute('SELECT welcome_enabled FROM settings WHERE chat_id = ?', (chat_id,))
        welcome_enabled = cursor.fetchone()
        welcome_enabled = welcome_enabled[0] if welcome_enabled else 1
        new_value = 0 if welcome_enabled else 1
        cursor.execute('INSERT OR REPLACE INTO settings (chat_id, welcome_enabled) VALUES (?, ?)', (chat_id, new_value))
        conn.commit()
        cursor.execute('SELECT sensitivity, welcome_enabled, captcha_enabled FROM settings WHERE chat_id = ?', (chat_id,))
        settings = cursor.fetchone()
        sensitivity, welcome_enabled, captcha_enabled = settings if settings else (0.7, new_value, 1)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Фильтр спама", callback_data='sensitivity')],
            [InlineKeyboardButton(f"Приветствия: {'Вкл' if welcome_enabled else 'Выкл'}", callback_data='toggle_welcome')],
            [InlineKeyboardButton(f"CAPTCHA: {'Вкл' if captcha_enabled else 'Выкл'}", callback_data='toggle_captcha')],
            [InlineKeyboardButton("Настройка CAPTCHA", callback_data='captcha_settings')],
            [InlineKeyboardButton("Назад", callback_data='back')]
        ])
        await query.edit_message_text("Настройки:", reply_markup=markup)

    elif data == 'toggle_captcha':
        if not await is_admin(update, context) or chat_type not in ['group', 'supergroup']:
            await query.answer("Недоступно!")
            return
        cursor.execute('SELECT captcha_enabled FROM settings WHERE chat_id = ?', (chat_id,))
        captcha_enabled = cursor.fetchone()
        captcha_enabled = captcha_enabled[0] if captcha_enabled else 1
        new_value = 0 if captcha_enabled else 1
        cursor.execute('INSERT OR REPLACE INTO settings (chat_id, captcha_enabled) VALUES (?, ?)', (chat_id, new_value))
        conn.commit()
        cursor.execute('SELECT sensitivity, welcome_enabled, captcha_enabled FROM settings WHERE chat_id = ?', (chat_id,))
        settings = cursor.fetchone()
        sensitivity, welcome_enabled, captcha_enabled = settings if settings else (0.7, 1, new_value)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Фильтр спама", callback_data='sensitivity')],
            [InlineKeyboardButton(f"Приветствия: {'Вкл' if welcome_enabled else 'Выкл'}", callback_data='toggle_welcome')],
            [InlineKeyboardButton(f"CAPTCHA: {'Вкл' if captcha_enabled else 'Выкл'}", callback_data='toggle_captcha')],
            [InlineKeyboardButton("Настройка CAPTCHA", callback_data='captcha_settings')],
            [InlineKeyboardButton("Назад", callback_data='back')]
        ])
        await query.edit_message_text("Настройки:", reply_markup=markup)

    elif data == 'captcha_settings':
        if not await is_admin(update, context) or chat_type not in ['group', 'supergroup']:
            await query.answer("Недоступно!")
            return
        cursor.execute('SELECT captcha_enabled, captcha_timeout, captcha_type FROM settings WHERE chat_id = ?', (chat_id,))
        settings = cursor.fetchone()
        captcha_enabled, captcha_timeout, captcha_type = settings if settings else (1, 120, 'button')
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"CAPTCHA: {'Вкл' if captcha_enabled else 'Выкл'}", callback_data='toggle_captcha')],
            [InlineKeyboardButton("Увеличить тайм-аут", callback_data='increase_captcha_timeout'),
             InlineKeyboardButton("Уменьшить тайм-аут", callback_data='decrease_captcha_timeout')],
            [InlineKeyboardButton(f"Тип: {'Кнопка' if captcha_type == 'button' else 'Вопрос'}", callback_data='toggle_captcha_type')],
            [InlineKeyboardButton("Назад", callback_data='back')]
        ])
        await query.edit_message_text(f"CAPTCHA:\nСтатус: {'Вкл' if captcha_enabled else 'Выкл'}\nТайм-аут: {captcha_timeout} сек\nТип: {'Кнопка' if captcha_type == 'button' else 'Вопрос'}", reply_markup=markup)

    elif data == 'toggle_captcha_type':
        if not await is_admin(update, context) or chat_type not in ['group', 'supergroup']:
            await query.answer("Недоступно!")
            return
        cursor.execute('SELECT captcha_type FROM settings WHERE chat_id = ?', (chat_id,))
        captcha_type = cursor.fetchone()
        captcha_type = captcha_type[0] if captcha_type else 'button'
        new_type = 'question' if captcha_type == 'button' else 'button'
        cursor.execute('INSERT OR REPLACE INTO settings (chat_id, captcha_type) VALUES (?, ?)', (chat_id, new_type))
        conn.commit()
        cursor.execute('SELECT captcha_enabled, captcha_timeout, captcha_type FROM settings WHERE chat_id = ?', (chat_id,))
        settings = cursor.fetchone()
        captcha_enabled, captcha_timeout, captcha_type = settings if settings else (1, 120, new_type)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"CAPTCHA: {'Вкл' if captcha_enabled else 'Выкл'}", callback_data='toggle_captcha')],
            [InlineKeyboardButton("Увеличить тайм-аут", callback_data='increase_captcha_timeout'),
             InlineKeyboardButton("Уменьшить тайм-аут", callback_data='decrease_captcha_timeout')],
            [InlineKeyboardButton(f"Тип: {'Кнопка' if captcha_type == 'button' else 'Вопрос'}", callback_data='toggle_captcha_type')],
            [InlineKeyboardButton("Назад", callback_data='back')]
        ])
        await query.edit_message_text(f"CAPTCHA:\nСтатус: {'Вкл' if captcha_enabled else 'Выкл'}\nТайм-аут: {captcha_timeout} сек\nТип: {'Кнопка' if captcha_type == 'button' else 'Вопрос'}", reply_markup=markup)

    elif data in ['increase_captcha_timeout', 'decrease_captcha_timeout']:
        if not await is_admin(update, context) or chat_type not in ['group', 'supergroup']:
            await query.answer("Недоступно!")
            return
        cursor.execute('SELECT captcha_timeout FROM settings WHERE chat_id = ?', (chat_id,))
        timeout = cursor.fetchone()
        timeout = timeout[0] if timeout else 120
        if data == 'increase_captcha_timeout':
            timeout += 30
        else:
            timeout = max(30, timeout - 30)
        cursor.execute('INSERT OR REPLACE INTO settings (chat_id, captcha_timeout) VALUES (?, ?)', (chat_id, timeout))
        conn.commit()
        cursor.execute('SELECT captcha_enabled, captcha_timeout, captcha_type FROM settings WHERE chat_id = ?', (chat_id,))
        settings = cursor.fetchone()
        captcha_enabled, captcha_timeout, captcha_type = settings if settings else (1, timeout, 'button')
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"CAPTCHA: {'Вкл' if captcha_enabled else 'Выкл'}", callback_data='toggle_captcha')],
            [InlineKeyboardButton("Увеличить тайм-аут", callback_data='increase_captcha_timeout'),
             InlineKeyboardButton("Уменьшить тайм-аут", callback_data='decrease_captcha_timeout')],
            [InlineKeyboardButton(f"Тип: {'Кнопка' if captcha_type == 'button' else 'Вопрос'}", callback_data='toggle_captcha_type')],
            [InlineKeyboardButton("Назад", callback_data='back')]
        ])
        await query.edit_message_text(f"CAPTCHA:\nСтатус: {'Вкл' if captcha_enabled else 'Выкл'}\nТайм-аут: {captcha_timeout} сек\nТип: {'Кнопка' if captcha_type == 'button' else 'Вопрос'}", reply_markup=markup)

    elif data.startswith('captcha_'):
        if chat_type not in ['group', 'supergroup']:
            await query.answer("CAPTCHA доступна только в группах!")
            return
        target_user_id = int(data.split('_')[1])
        if target_user_id != user_id:
            await query.answer("Это не ваша CAPTCHA!")
            return
        cursor.execute('SELECT status, answer FROM captcha WHERE chat_id = ? AND user_id = ?', (chat_id, user_id))
        captcha = cursor.fetchone()
        if captcha and captcha[0] == 0:
            if captcha[1] == 'button':
                cursor.execute('UPDATE captcha SET status = 1 WHERE chat_id = ? AND user_id = ?', (chat_id, user_id))
                conn.commit()
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='back')]])
                await query.edit_message_text("CAPTCHA пройдена!", reply_markup=markup)
            else:
                await query.answer("Ответьте на вопрос в чате!")
        else:
            await query.answer("CAPTCHA уже пройдена!")

# Веб-приложение
@app.route('/')
def index():
    cursor.execute('SELECT chat_id, sensitivity, welcome_enabled, captcha_enabled, captcha_timeout, captcha_type FROM settings')
    settings = cursor.fetchall()
    return render_template('index.html', settings=settings)

@app.route('/update_settings', methods=['POST'])
def update_settings():
    chat_id = int(request.form['chat_id'])
    sensitivity = float(request.form['sensitivity'])
    welcome_enabled = int(request.form.get('welcome_enabled', 0))
    captcha_enabled = int(request.form.get('captcha_enabled', 0))
    captcha_timeout = int(request.form['captcha_timeout'])
    captcha_type = request.form['captcha_type']
    
    cursor.execute('INSERT OR REPLACE INTO settings (chat_id, sensitivity, welcome_enabled, captcha_enabled, captcha_timeout, captcha_type) VALUES (?, ?, ?, ?, ?, ?)',
                   (chat_id, sensitivity, welcome_enabled, captcha_enabled, captcha_timeout, captcha_type))
    conn.commit()
    return jsonify({'status': 'success'})

# Шаблон веб-страницы
with open('templates/index.html', 'w') as f:
    f.write('''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Панель управления ботом</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 font-sans">
    <div class="container mx-auto p-6">
        <h1 class="text-3xl font-bold text-center mb-8">Панель управления ботом</h1>
        <div class="bg-white shadow-md rounded-lg p-6">
            <h2 class="text-2xl font-semibold mb-4">Настройки чатов</h2>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                {% for setting in settings %}
                <div class="p-4 border rounded-lg">
                    <h3 class="text-xl font-medium mb-2">Chat ID: {{ setting[0] }}</h3>
                    <form action="/update_settings" method="POST">
                        <input type="hidden" name="chat_id" value="{{ setting[0] }}">
                        <div class="mb-4">
                            <label class="block text-sm font-medium">Чувствительность фильтра</label>
                            <input type="number" step="0.1" min="0" max="1" name="sensitivity" value="{{ setting[1] }}" class="w-full p-2 border rounded">
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm font-medium">Приветствия включены</label>
                            <input type="checkbox" name="welcome_enabled" {% if setting[2] %}checked{% endif %} value="1">
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm font-medium">CAPTCHA включена</label>
                            <input type="checkbox" name="captcha_enabled" {% if setting[3] %}checked{% endif %} value="1">
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm font-medium">Тайм-аут CAPTCHA (сек)</label>
                            <input type="number" name="captcha_timeout" value="{{ setting[4] }}" class="w-full p-2 border rounded">
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm font-medium">Тип CAPTCHA</label>
                            <select name="captcha_type" class="w-full p-2 border rounded">
                                <option value="button" {% if setting[5] == 'button' %}selected{% endif %}>Кнопка</option>
                                <option value="question" {% if setting[5] == 'question' %}selected{% endif %}>Вопрос</option>
                            </select>
                        </div>
                        <button type="submit" class="bg-blue-500 text-white px-4 py-2 rounded hover:bg-blue-600">Сохранить</button>
                    </form>
                </div>
                {% endfor %}
            </div>
        </div>
    </div>
</body>
</html>
''')

# Запуск Flask
def run_flask():
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), threaded=False)

# Основной запуск
async def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler('start', start, filters=filters.ChatType.ALL))
    app.add_handler(CommandHandler('rules', rules, filters=filters.ChatType.ALL))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.ALL, handle_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP), new_member))
    app.add_handler(CallbackQueryHandler(button_callback))
    await app.run_polling()

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(main())
