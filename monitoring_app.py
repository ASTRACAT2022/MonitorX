import asyncio
import sqlite3
import re
import random
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from transformers import pipeline
from flask import Flask, render_template, request, jsonify
import threading
from datetime import datetime, timedelta
from uuid import uuid4

# Инициализация
TOKEN = '7705234760:AAGD1bFJaOeoedKPWxLOVZJYsA5jLQMhtw4'  # Замените на ваш токен (например, 7705234760:AAGD1bFJaOeoedKPWxLOVZJYsA5jLQMhtw4)
classifier = pipeline('text-classification', model='distilbert-base-uncased-finetuned-sst-2-english')

# База данных
conn = sqlite3.connect('chat_filter.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    user_id INTEGER,
                    username TEXT,
                    text TEXT,
                    is_spam INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS settings (
                    chat_id INTEGER PRIMARY KEY,
                    sensitivity REAL DEFAULT 0.7,
                    welcome_message TEXT,
                    rules TEXT,
                    flood_limit INTEGER DEFAULT 5,
                    flood_interval INTEGER DEFAULT 10,
                    welcome_enabled INTEGER DEFAULT 1,
                    spam_keywords TEXT,
                    captcha_enabled INTEGER DEFAULT 1,
                    captcha_timeout INTEGER DEFAULT 120,
                    captcha_type TEXT DEFAULT 'button')''')
cursor.execute('''CREATE TABLE IF NOT EXISTS warnings (
                    chat_id INTEGER,
                    user_id INTEGER,
                    count INTEGER DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS bans (
                    chat_id INTEGER,
                    user_id INTEGER,
                    reason TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS captcha (
                    chat_id INTEGER,
                    user_id INTEGER,
                    status INTEGER DEFAULT 0,
                    answer TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id))''')
cursor.execute('''CREATE TABLE IF NOT EXISTS captcha_stats (
                    chat_id INTEGER,
                    user_id INTEGER,
                    passed INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id))''')
conn.commit()

# Проверка на спам
def is_spam(text, chat_id):
    cursor.execute('SELECT spam_keywords FROM settings WHERE chat_id = ?', (chat_id,))
    keywords = cursor.fetchone()
    keywords = keywords[0].split(',') if keywords and keywords[0] else ['заработай миллионы', 'быстрые деньги', 'инвестируй сейчас', 'секрет успеха']
    for keyword in keywords:
        if keyword.strip() and re.search(keyword.lower(), text.lower()):
            return True
    result = classifier(text)[0]
    score = result['score'] if result['label'] == 'NEGATIVE' else 1 - result['score']
    cursor.execute('SELECT sensitivity FROM settings WHERE chat_id = ?', (chat_id,))
    sensitivity = cursor.fetchone()
    sensitivity = sensitivity[0] if sensitivity else 0.7
    return score > sensitivity

# Проверка на флуд
async def check_flood(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    cursor.execute('SELECT flood_limit, flood_interval FROM settings WHERE chat_id = ?', (chat_id,))
    settings = cursor.fetchone()
    flood_limit, flood_interval = settings if settings else (5, 10)

    if 'messages' not in context.user_data:
        context.user_data['messages'] = []
    
    now = datetime.now()
    context.user_data['messages'] = [t for t in context.user_data['messages'] if now - t < timedelta(seconds=flood_interval)]
    context.user_data['messages'].append(now)

    if len(context.user_data['messages']) > flood_limit:
        await update.message.delete()
        await context.bot.send_message(chat_id, f"{update.effective_user.first_name}, не флуди! Подожди {flood_interval} секунд.")
        return True
    return False

# Генерация вопроса для CAPTCHA
def generate_captcha_question():
    a = random.randint(1, 10)
    b = random.randint(1, 10)
    question = f"{a} + {b} = ?"
    answer = str(a + b)
    return question, answer

# Приветственное сообщение и CAPTCHA
async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    cursor.execute('SELECT welcome_message, welcome_enabled, captcha_enabled, captcha_timeout, captcha_type FROM settings WHERE chat_id = ?', (chat_id,))
    settings = cursor.fetchone()
    welcome_enabled = settings[1] if settings else 1
    captcha_enabled = settings[2] if settings else 1
    captcha_timeout = settings[3] if settings else 120
    captcha_type = settings[4] if settings else 'button'
    welcome = settings[0] if settings and settings[0] else f"Добро пожаловать, {user.first_name}! Пожалуйста, соблюдайте правила чата (/rules)."

    if welcome_enabled:
        if captcha_enabled:
            if captcha_type == 'button':
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("Я не бот", callback_data=f'captcha_{user.id}_button')]])
                await context.bot.send_message(chat_id, f"{welcome}\n\nНажмите кнопку ниже в течение {captcha_timeout} секунд, чтобы подтвердить, что вы не бот:", reply_markup=markup)
                cursor.execute('INSERT OR REPLACE INTO captcha (chat_id, user_id, status, answer) VALUES (?, ?, 0, ?)', (chat_id, user.id, 'button'))
                conn.commit()
            else:  # question
                question, answer = generate_captcha_question()
                await context.bot.send_message(chat_id, f"{welcome}\n\nОтветьте на вопрос в течение {captcha_timeout} секунд, чтобы подтвердить, что вы не бот:\n{question}")
                cursor.execute('INSERT OR REPLACE INTO captcha (chat_id, user_id, status, answer) VALUES (?, ?, 0, ?)', (chat_id, user.id, answer))
                conn.commit()
            context.job_queue.run_once(check_captcha_timeout, captcha_timeout, data={'chat_id': chat_id, 'user_id': user.id})
        else:
            await context.bot.send_message(chat_id, welcome)

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
            await context.bot.send_message(chat_id, f"Пользователь (ID: {user_id}) удалён из чата за непрохождение CAPTCHA.")
            cursor.execute('INSERT INTO bans (chat_id, user_id, reason) VALUES (?, ?, ?)', (chat_id, user_id, 'Не пройдена CAPTCHA'))
            cursor.execute('INSERT OR REPLACE INTO captcha_stats (chat_id, user_id, passed) VALUES (?, ?, 0)', (chat_id, user_id))
            cursor.execute('DELETE FROM captcha WHERE chat_id = ? AND user_id = ?', (chat_id, user_id))
            conn.commit()
        except Exception as e:
            print(f"Ошибка при бане: {e}")

# Обработчик новых участников
async def new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for member in update.message.new_chat_members:
        await send_welcome(update, context)

# Обработчик сообщений
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    text = update.message.text

    # Проверка CAPTCHA
    cursor.execute('SELECT status, answer FROM captcha WHERE chat_id = ? AND user_id = ?', (chat_id, user_id))
    captcha_status = cursor.fetchone()
    if captcha_status and captcha_status[0] == 0:
        if captcha_status[1] != 'button':  # Проверка ответа на вопрос
            if text.strip() == captcha_status[1]:
                cursor.execute('UPDATE captcha SET status = 1 WHERE chat_id = ? AND user_id = ?', (chat_id, user_id))
                cursor.execute('INSERT OR REPLACE INTO captcha_stats (chat_id, user_id, passed) VALUES (?, ?, 1)', (chat_id, user_id))
                conn.commit()
                await update.message.reply_text("CAPTCHA пройдена! Добро пожаловать в чат!")
                return
            else:
                await update.message.delete()
                await context.bot.send_message(chat_id, f"{username}, неверный ответ! Попробуйте снова.")
                return
        await update.message.delete()
        await context.bot.send_message(chat_id, f"{username}, сначала пройдите CAPTCHA!")
        return

    # Проверка на флуд
    if await check_flood(update, context):
        return

    # Проверка на спам
    if is_spam(text, chat_id):
        cursor.execute('INSERT INTO messages (chat_id, user_id, username, text, is_spam) VALUES (?, ?, ?, ?, ?)',
                       (chat_id, user_id, username, text, 1))
        conn.commit()
        await update.message.delete()
        await context.bot.send_message(chat_id, f"Сообщение от {username} удалено как спам.")
    else:
        cursor.execute('INSERT INTO messages (chat_id, user_id, username, text, is_spam) VALUES (?, ?, ?, ?, ?)',
                       (chat_id, user_id, username, text, 0))
        conn.commit()

    # Панель админа при ответе на сообщение
    if update.message.reply_to_message and await is_admin(update, context):
        target_user = update.message.reply_to_message.from_user
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Выдать варн", callback_data=f'warn_{target_user.id}'),
             InlineKeyboardButton("Бан", callback_data=f'ban_{target_user.id}')],
            [InlineKeyboardButton("Мут", callback_data=f'mute_{target_user.id}'),
             InlineKeyboardButton("Снять мут", callback_data=f'unmute_{target_user.id}')]
        ])
        await update.message.reply_text(f"Действия для {target_user.first_name}:", reply_markup=markup)

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin_user = await is_admin(update, context)
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("Настройки", callback_data='settings'), InlineKeyboardButton("Статистика", callback_data='stats')],
        [InlineKeyboardButton("Правила", callback_data='rules'), InlineKeyboardButton("Мои варны", callback_data='my_warnings')],
        [InlineKeyboardButton("Управление CAPTCHA", callback_data='captcha_settings')] if is_admin_user else []
    ])
    await update.message.reply_text("Я бот для модерации чата! Используй кнопки для управления.", reply_markup=markup)

# Команда /rules
async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cursor.execute('SELECT rules FROM settings WHERE chat_id = ?', (chat_id,))
    rules_text = cursor.fetchone()
    rules_text = rules_text[0] if rules_text else "Правила не установлены. Админы могут задать правила через настройки."
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='back')]])
    await update.message.reply_text(rules_text, reply_markup=markup)

# Проверка админских прав
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
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

    # Главное меню
    if data == 'back':
        is_admin_user = await is_admin(update, context)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Настройки", callback_data='settings'), InlineKeyboardButton("Статистика", callback_data='stats')],
            [InlineKeyboardButton("Правила", callback_data='rules'), InlineKeyboardButton("Мои варны", callback_data='my_warnings')],
            [InlineKeyboardButton("Управление CAPTCHA", callback_data='captcha_settings')] if is_admin_user else []
        ])
        await query.edit_message_text("Выберите действие:", reply_markup=markup)
        return

    # Настройки
    if data == 'settings':
        if not await is_admin(update, context):
            await query.answer("Только админы могут управлять настройками!")
            return
        cursor.execute('SELECT sensitivity, welcome_enabled, flood_limit, flood_interval, captcha_enabled FROM settings WHERE chat_id = ?', (chat_id,))
        settings = cursor.fetchone()
        sensitivity, welcome_enabled, flood_limit, flood_interval, captcha_enabled = settings if settings else (0.7, 1, 5, 10, 1)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Фильтр спама", callback_data='sensitivity')],
            [InlineKeyboardButton(f"Приветствия: {'Вкл' if welcome_enabled else 'Выкл'}", callback_data='toggle_welcome')],
            [InlineKeyboardButton("Антифлуд", callback_data='flood_settings')],
            [InlineKeyboardButton("Ключевые слова", callback_data='keywords')],
            [InlineKeyboardButton("Правила чата", callback_data='set_rules')],
            [InlineKeyboardButton(f"CAPTCHA: {'Вкл' if captcha_enabled else 'Выкл'}", callback_data='toggle_captcha')],
            [InlineKeyboardButton("Настройка CAPTCHA", callback_data='captcha_settings')],
            [InlineKeyboardButton("Назад", callback_data='back')]
        ])
        await query.edit_message_text("Настройки бота:", reply_markup=markup)

    # Чувствительность фильтра
    elif data == 'sensitivity':
        cursor.execute('SELECT sensitivity FROM settings WHERE chat_id = ?', (chat_id,))
        sensitivity = cursor.fetchone()
        sensitivity = sensitivity[0] if sensitivity else 0.7
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Увеличить", callback_data='increase_sensitivity'),
             InlineKeyboardButton("Уменьшить", callback_data='decrease_sensitivity')],
            [InlineKeyboardButton("Назад", callback_data='settings')]
        ])
        await query.edit_message_text(f"Чувствительность фильтра: {sensitivity:.2f}", reply_markup=markup)

    elif data in ['increase_sensitivity', 'decrease_sensitivity']:
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
        await query.edit_message_text(f"Чувствительность обновлена: {sensitivity:.2f}", reply_markup=markup)

    # Включение/выключение приветствий
    elif data == 'toggle_welcome':
        cursor.execute('SELECT welcome_enabled FROM settings WHERE chat_id = ?', (chat_id,))
        welcome_enabled = cursor.fetchone()
        welcome_enabled = welcome_enabled[0] if welcome_enabled else 1
        new_value = 0 if welcome_enabled else 1
        cursor.execute('INSERT OR REPLACE INTO settings (chat_id, welcome_enabled) VALUES (?, ?)', (chat_id, new_value))
        conn.commit()
        cursor.execute('SELECT sensitivity, welcome_enabled, flood_limit, flood_interval, captcha_enabled FROM settings WHERE chat_id = ?', (chat_id,))
        settings = cursor.fetchone()
        sensitivity, welcome_enabled, flood_limit, flood_interval, captcha_enabled = settings if settings else (0.7, new_value, 5, 10, 1)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Фильтр спама", callback_data='sensitivity')],
            [InlineKeyboardButton(f"Приветствия: {'Вкл' if welcome_enabled else 'Выкл'}", callback_data='toggle_welcome')],
            [InlineKeyboardButton("Антифлуд", callback_data='flood_settings')],
            [InlineKeyboardButton("Ключевые слова", callback_data='keywords')],
            [InlineKeyboardButton("Правила чата", callback_data='set_rules')],
            [InlineKeyboardButton(f"CAPTCHA: {'Вкл' if captcha_enabled else 'Выкл'}", callback_data='toggle_captcha')],
            [InlineKeyboardButton("Настройка CAPTCHA", callback_data='captcha_settings')],
            [InlineKeyboardButton("Назад", callback_data='back')]
        ])
        await query.edit_message_text("Настройки бота:", reply_markup=markup)

    # Настройка антифлуда
    elif data == 'flood_settings':
        cursor.execute('SELECT flood_limit, flood_interval FROM settings WHERE chat_id = ?', (chat_id,))
        settings = cursor.fetchone()
        flood_limit, flood_interval = settings if settings else (5, 10)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Увеличить лимит", callback_data='increase_flood_limit'),
             InlineKeyboardButton("Уменьшить лимит", callback_data='decrease_flood_limit')],
            [InlineKeyboardButton("Увеличить интервал", callback_data='increase_flood_interval'),
             InlineKeyboardButton("Уменьшить интервал", callback_data='decrease_flood_interval')],
            [InlineKeyboardButton("Назад", callback_data='settings')]
        ])
        await query.edit_message_text(f"Антифлуд: {flood_limit} сообщений за {flood_interval} секунд", reply_markup=markup)

    elif data in ['increase_flood_limit', 'decrease_flood_limit', 'increase_flood_interval', 'decrease_flood_interval']:
        cursor.execute('SELECT flood_limit, flood_interval FROM settings WHERE chat_id = ?', (chat_id,))
        settings = cursor.fetchone()
        flood_limit, flood_interval = settings if settings else (5, 10)
        if data == 'increase_flood_limit':
            flood_limit += 1
        elif data == 'decrease_flood_limit':
            flood_limit = max(1, flood_limit - 1)
        elif data == 'increase_flood_interval':
            flood_interval += 5
        elif data == 'decrease_flood_interval':
            flood_interval = max(5, flood_interval - 5)
        cursor.execute('INSERT OR REPLACE INTO settings (chat_id, flood_limit, flood_interval) VALUES (?, ?, ?)', (chat_id, flood_limit, flood_interval))
        conn.commit()
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Увеличить лимит", callback_data='increase_flood_limit'),
             InlineKeyboardButton("Уменьшить лимит", callback_data='decrease_flood_limit')],
            [InlineKeyboardButton("Увеличить интервал", callback_data='increase_flood_interval'),
             InlineKeyboardButton("Уменьшить интервал", callback_data='decrease_flood_interval')],
            [InlineKeyboardButton("Назад", callback_data='settings')]
        ])
        await query.edit_message_text(f"Антифлуд: {flood_limit} сообщений за {flood_interval} секунд", reply_markup=markup)

    # Ключевые слова
    elif data == 'keywords':
        cursor.execute('SELECT spam_keywords FROM settings WHERE chat_id = ?', (chat_id,))
        keywords = cursor.fetchone()
        keywords = keywords[0] if keywords and keywords[0] else "Нет ключевых слов"
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Добавить слово", callback_data='add_keyword')],
            [InlineKeyboardButton("Назад", callback_data='settings')]
        ])
        await query.edit_message_text(f"Ключевые слова для фильтра:\n{keywords}\n\nДобавляйте через кнопку или /addkeyword", reply_markup=markup)

    elif data == 'add_keyword':
        await query.edit_message_text("Отправьте слово для добавления в фильтр:")
        context.user_data['awaiting_keyword'] = True

    # Правила чата
    elif data == 'set_rules':
        await query.edit_message_text("Отправьте текст правил чата:")
        context.user_data['awaiting_rules'] = True

    # Включение/выключение CAPTCHA
    elif data == 'toggle_captcha':
        cursor.execute('SELECT captcha_enabled FROM settings WHERE chat_id = ?', (chat_id,))
        captcha_enabled = cursor.fetchone()
        captcha_enabled = captcha_enabled[0] if captcha_enabled else 1
        new_value = 0 if captcha_enabled else 1
        cursor.execute('INSERT OR REPLACE INTO settings (chat_id, captcha_enabled) VALUES (?, ?)', (chat_id, new_value))
        conn.commit()
        cursor.execute('SELECT sensitivity, welcome_enabled, flood_limit, flood_interval, captcha_enabled FROM settings WHERE chat_id = ?', (chat_id,))
        settings = cursor.fetchone()
        sensitivity, welcome_enabled, flood_limit, flood_interval, captcha_enabled = settings if settings else (0.7, 1, 5, 10, new_value)
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Фильтр спама", callback_data='sensitivity')],
            [InlineKeyboardButton(f"Приветствия: {'Вкл' if welcome_enabled else 'Выкл'}", callback_data='toggle_welcome')],
            [InlineKeyboardButton("Антифлуд", callback_data='flood_settings')],
            [InlineKeyboardButton("Ключевые слова", callback_data='keywords')],
            [InlineKeyboardButton("Правила чата", callback_data='set_rules')],
            [InlineKeyboardButton(f"CAPTCHA: {'Вкл' if captcha_enabled else 'Выкл'}", callback_data='toggle_captcha')],
            [InlineKeyboardButton("Настройка CAPTCHA", callback_data='captcha_settings')],
            [InlineKeyboardButton("Назад", callback_data='back')]
        ])
        await query.edit_message_text("Настройки бота:", reply_markup=markup)

    # Настройка CAPTCHA
    elif data == 'captcha_settings':
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
        await query.edit_message_text(f"Настройка CAPTCHA:\nСтатус: {'Вкл' if captcha_enabled else 'Выкл'}\nТайм-аут: {captcha_timeout} секунд\nТип: {'Кнопка' if captcha_type == 'button' else 'Вопрос'}", reply_markup=markup)

    # Переключение типа CAPTCHA
    elif data == 'toggle_captcha_type':
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
        await query.edit_message_text(f"Настройка CAPTCHA:\nСтатус: {'Вкл' if captcha_enabled else 'Выкл'}\nТайм-аут: {captcha_timeout} секунд\nТип: {'Кнопка' if captcha_type == 'button' else 'Вопрос'}", reply_markup=markup)

    # Настройка тайм-аута CAPTCHA
    elif data in ['increase_captcha_timeout', 'decrease_captcha_timeout']:
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
        await query.edit_message_text(f"Настройка CAPTCHA:\nСтатус: {'Вкл' if captcha_enabled else 'Выкл'}\nТайм-аут: {captcha_timeout} секунд\nТип: {'Кнопка' if captcha_type == 'button' else 'Вопрос'}", reply_markup=markup)

    # Статистика
    elif data == 'stats':
        cursor.execute('SELECT COUNT(*) FROM messages WHERE chat_id = ? AND is_spam = 1', (chat_id,))
        spam_count = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM messages WHERE chat_id = ?', (chat_id,))
        total_count = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM bans WHERE chat_id = ?', (chat_id,))
        ban_count = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM captcha_stats WHERE chat_id = ? AND passed = 1', (chat_id,))
        captcha_passed = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM captcha_stats WHERE chat_id = ? AND passed = 0', (chat_id,))
        captcha_failed = cursor.fetchone()[0]
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='back')]])
        await query.edit_message_text(f"Статистика:\nВсего сообщений: {total_count}\nСпам: {spam_count}\nБаны: {ban_count}\nCAPTCHA пройдено: {captcha_passed}\nCAPTCHA не пройдено: {captcha_failed}", reply_markup=markup)

    # Мои варны
    elif data == 'my_warnings':
        cursor.execute('SELECT count FROM warnings WHERE chat_id = ? AND user_id = ?', (chat_id, user_id))
        warn_count = cursor.fetchone()
        warn_count = warn_count[0] if warn_count else 0
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='back')]])
        await query.edit_message_text(f"Ваши предупреждения: {warn_count}/3", reply_markup=markup)

    # Проверка CAPTCHA
    elif data.startswith('captcha_'):
        target_user_id = int(data.split('_')[1])
        if target_user_id != user_id:
            await query.answer("Это не ваша CAPTCHA!")
            return
        cursor.execute('SELECT status, answer FROM captcha WHERE chat_id = ? AND user_id = ?', (chat_id, user_id))
        captcha = cursor.fetchone()
        if captcha and captcha[0] == 0:
            if captcha[1] == 'button':
                cursor.execute('UPDATE captcha SET status = 1 WHERE chat_id = ? AND user_id = ?', (chat_id, user_id))
                cursor.execute('INSERT OR REPLACE INTO captcha_stats (chat_id, user_id, passed) VALUES (?, ?, 1)', (chat_id, user_id))
                conn.commit()
                markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='back')]])
                await query.edit_message_text("CAPTCHA пройдена! Добро пожаловать в чат!", reply_markup=markup)
            else:
                await query.answer("Ответьте на вопрос в чате!")
        else:
            await query.answer("CAPTCHA уже пройдена или недействительна!")

    # Действия модерации
    elif data.startswith('warn_'):
        if not await is_admin(update, context):
            await query.answer("Только админы могут выдавать предупреждения!")
            return
        target_user_id = int(data.split('_')[1])
        cursor.execute('INSERT OR REPLACE INTO warnings (chat_id, user_id, count) VALUES (?, ?, COALESCE((SELECT count FROM warnings WHERE chat_id = ? AND user_id = ?), 0) + 1)',
                       (chat_id, target_user_id, chat_id, target_user_id))
        conn.commit()
        cursor.execute('SELECT count FROM warnings WHERE chat_id = ? AND user_id = ?', (chat_id, target_user_id))
        warn_count = cursor.fetchone()[0]
        if warn_count >= 3:
            await context.bot.ban_chat_member(chat_id, target_user_id)
            cursor.execute('INSERT INTO bans (chat_id, user_id, reason) VALUES (?, ?, ?)', (chat_id, target_user_id, 'Достигнут лимит предупреждений'))
            conn.commit()
            await query.edit_message_text(f"Пользователь забанен за 3 предупреждения.")
        else:
            await query.edit_message_text(f"Пользователь получил предупреждение ({warn_count}/3).",
                                         reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='back')]]))

    elif data.startswith('ban_'):
        if not await is_admin(update, context):
            await query.answer("Только админы могут банить!")
            return
        target_user_id = int(data.split('_')[1])
        await context.bot.ban_chat_member(chat_id, target_user_id)
        cursor.execute('INSERT INTO bans (chat_id, user_id, reason) VALUES (?, ?, ?)', (chat_id, target_user_id, 'Ручной бан'))
        conn.commit()
        await query.edit_message_text(f"Пользователь забанен.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='back')]]))

    elif data.startswith('mute_'):
        if not await is_admin(update, context):
            await query.answer("Только админы могут заглушать!")
            return
        target_user_id = int(data.split('_')[1])
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("10 мин", callback_data=f'mute_{target_user_id}_10'),
             InlineKeyboardButton("1 час", callback_data=f'mute_{target_user_id}_60')],
            [InlineKeyboardButton("1 день", callback_data=f'mute_{target_user_id}_1440')],
            [InlineKeyboardButton("Назад", callback_data='back')]
        ])
        await query.edit_message_text("Выберите длительность мута:", reply_markup=markup)

    elif data.startswith('mute_') and len(data.split('_')) == 3:
        if not await is_admin(update, context):
            await query.answer("Только админы могут заглушать!")
            return
        target_user_id = int(data.split('_')[1])
        minutes = int(data.split('_')[2])
        until_date = datetime.now() + timedelta(minutes=minutes)
        await context.bot.restrict_chat_member(chat_id, target_user_id, until_date=until_date,
                                              permissions={'can_send_messages': False})
        await query.edit_message_text(f"Пользователь заглушен на {minutes} минут.",
                                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='back')]]))

    elif data.startswith('unmute_'):
        if not await is_admin(update, context):
            await query.answer("Только админы могут снимать мут!")
            return
        target_user_id = int(data.split('_')[1])
        await context.bot.restrict_chat_member(chat_id, target_user_id,
                                              permissions={'can_send_messages': True})
        await query.edit_message_text(f"Мут снят с пользователя.",
                                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='back')]]))

# Обработчик текстовых сообщений для ввода правил и ключевых слов
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text

    if context.user_data.get('awaiting_rules') and await is_admin(update, context):
        cursor.execute('INSERT OR REPLACE INTO settings (chat_id, rules) VALUES (?, ?)', (chat_id, text))
        conn.commit()
        context.user_data['awaiting_rules'] = False
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='settings')]])
        await update.message.reply_text("Правила обновлены!", reply_markup=markup)

    elif context.user_data.get('awaiting_keyword') and await is_admin(update, context):
        keyword = text.lower()
        cursor.execute('SELECT spam_keywords FROM settings WHERE chat_id = ?', (chat_id,))
        keywords = cursor.fetchone()
        keywords = keywords[0].split(',') if keywords and keywords[0] else []
        if keyword not in keywords:
            keywords.append(keyword)
            cursor.execute('INSERT OR REPLACE INTO settings (chat_id, spam_keywords) VALUES (?, ?)', (chat_id, ','.join(keywords)))
            conn.commit()
            await update.message.reply_text(f"Ключевое слово '{keyword}' добавлено.")
        else:
            await update.message.reply_text("Это слово уже в списке!")
        context.user_data['awaiting_keyword'] = False
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data='settings')]])
        await update.message.reply_text("Вернуться к настройкам:", reply_markup=markup)

# Веб-приложение
app = Flask(__name__)

@app.route('/')
def index():
    cursor.execute('SELECT chat_id, COUNT(*) as total, SUM(is_spam) as spam FROM messages GROUP BY chat_id')
    stats = cursor.fetchall()
    cursor.execute('SELECT chat_id, sensitivity, welcome_message, rules, flood_limit, flood_interval, welcome_enabled, spam_keywords, captcha_enabled, captcha_timeout, captcha_type FROM settings')
    settings = cursor.fetchall()
    cursor.execute('SELECT chat_id, COUNT(*) as bans FROM bans GROUP BY chat_id')
    bans = cursor.fetchall()
    cursor.execute('SELECT chat_id, COUNT(*) as passed FROM captcha_stats WHERE passed = 1 GROUP BY chat_id')
    captcha_passed = cursor.fetchall()
    cursor.execute('SELECT chat_id, COUNT(*) as failed FROM captcha_stats WHERE passed = 0 GROUP BY chat_id')
    captcha_failed = cursor.fetchall()
    return render_template('index.html', stats=stats, settings=settings, bans=bans, captcha_passed=captcha_passed, captcha_failed=captcha_failed)

@app.route('/update_settings', methods=['POST'])
def update_settings():
    chat_id = int(request.form['chat_id'])
    sensitivity = float(request.form['sensitivity'])
    welcome_message = request.form['welcome_message']
    rules = request.form['rules']
    flood_limit = int(request.form['flood_limit'])
    flood_interval = int(request.form['flood_interval'])
    welcome_enabled = int(request.form['welcome_enabled'])
    spam_keywords = request.form['spam_keywords']
    captcha_enabled = int(request.form['captcha_enabled'])
    captcha_timeout = int(request.form['captcha_timeout'])
    captcha_type = request.form['captcha_type']
    
    cursor.execute('INSERT OR REPLACE INTO settings (chat_id, sensitivity, welcome_message, rules, flood_limit, flood_interval, welcome_enabled, spam_keywords, captcha_enabled, captcha_timeout, captcha_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   (chat_id, sensitivity, welcome_message, rules, flood_limit, flood_interval, welcome_enabled, spam_keywords, captcha_enabled, captcha_timeout, captcha_type))
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
    <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
</head>
<body class="bg-gray-100 font-sans">
    <div class="container mx-auto p-6">
        <h1 class="text-3xl font-bold text-center mb-8">Панель управления ботом</h1>
        
        <!-- Статистика -->
        <div class="bg-white shadow-md rounded-lg p-6 mb-8">
            <h2 class="text-2xl font-semibold mb-4">Статистика чатов</h2>
            <div class="overflow-x-auto">
                <table class="w-full text-left">
                    <thead>
                        <tr class="bg-gray-200">
                            <th class="p-3">Chat ID</th>
                            <th class="p-3">Всего сообщений</th>
                            <th class="p-3">Спам</th>
                            <th class="p-3">Баны</th>
                            <th class="p-3">CAPTCHA пройдено</th>
                            <th class="p-3">CAPTCHA не пройдено</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for stat in stats %}
                        <tr class="border-b">
                            <td class="p-3">{{ stat[0] }}</td>
                            <td class="p-3">{{ stat[1] }}</td>
                            <td class="p-3">{{ stat[2] }}</td>
                            <td class="p-3">
                                {% for ban in bans %}
                                    {% if ban[0] == stat[0] %}
                                        {{ ban[1] }}
                                    {% endif %}
                                {% endfor %}
                            </td>
                            <td class="p-3">
                                {% for passed in captcha_passed %}
                                    {% if passed[0] == stat[0] %}
                                        {{ passed[1] }}
                                    {% endif %}
                                {% endfor %}
                            </td>
                            <td class="p-3">
                                {% for failed in captcha_failed %}
                                    {% if failed[0] == stat[0] %}
                                        {{ failed[1] }}
                                    {% endif %}
                                {% endfor %}
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
        
        <!-- Настройки -->
        <div class="bg-white shadow-md rounded-lg p-6">
            <h2 class="text-2xl font-semibold mb-4">Настройки чатов</h2>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
                {% for setting in settings %}
                <div class="p-4 border rounded-lg">
                    <h3 class="text-xl font-medium mb-2">Chat ID: {{ setting[0] }}</h3>
                    <form class="settings-form" data-chat-id="{{ setting[0] }}">
                        <div class="mb-4">
                            <label class="block text-sm font-medium">Чувствительность фильтра</label>
                            <input type="number" step="0.1" min="0" max="1" name="sensitivity" value="{{ setting[1] }}" class="w-full p-2 border rounded">
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm font-medium">Приветственное сообщение</label>
                            <textarea name="welcome_message" class="w-full p-2 border rounded">{{ setting[2] }}</textarea>
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm font-medium">Правила</label>
                            <textarea name="rules" class="w-full p-2 border rounded">{{ setting[3] }}</textarea>
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm font-medium">Лимит антифлуда</label>
                            <input type="number" name="flood_limit" value="{{ setting[4] }}" class="w-full p-2 border rounded">
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm font-medium">Интервал антифлуда (сек)</label>
                            <input type="number" name="flood_interval" value="{{ setting[5] }}" class="w-full p-2 border rounded">
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm font-medium">Приветствия включены</label>
                            <input type="checkbox" name="welcome_enabled" {% if setting[6] %}checked{% endif %} value="1">
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm font-medium">Ключевые слова (через запятую)</label>
                            <input type="text" name="spam_keywords" value="{{ setting[7] }}" class="w-full p-2 border rounded">
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm font-medium">CAPTCHA включена</label>
                            <input type="checkbox" name="captcha_enabled" {% if setting[8] %}checked{% endif %} value="1">
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm font-medium">Тайм-аут CAPTCHA (сек)</label>
                            <input type="number" name="captcha_timeout" value="{{ setting[9] }}" class="w-full p-2 border rounded">
                        </div>
                        <div class="mb-4">
                            <label class="block text-sm font-medium">Тип CAPTCHA</label>
                            <select name="captcha_type" class="w-full p-2 border rounded">
                                <option value="button" {% if setting[10] == 'button' %}selected{% endif %}>Кнопка</option>
                                <option value="question" {% if setting[10] == 'question' %}selected{% endif %}>Вопрос</option>
                            </select>
                        </div>
                        <button type="submit" class="bg-blue-500 text-white px-4 py-2 rounded hover:bg-blue-600">Сохранить</button>
                    </form>
                </div>
                {% endfor %}
            </div>
        </div>
    </div>

    <script>
        $(document).ready(function() {
            $('.settings-form').submit(function(e) {
                e.preventDefault();
                const chatId = $(this).data('chat-id');
                const formData = $(this).serializeArray();
                const data = { chat_id: chatId };
                formData.forEach(item => {
                    data[item.name] = item.value;
                });
                data.welcome_enabled = $(this).find('input[name="welcome_enabled"]').is(':checked') ? 1 : 0;
                data.captcha_enabled = $(this).find('input[name="captcha_enabled"]').is(':checked') ? 1 : 0;

                $.post('/update_settings', data, function(response) {
                    if (response.status === 'success') {
                        alert('Настройки сохранены!');
                    } else {
                        alert('Ошибка при сохранении настроек.');
                    }
                });
            });
        });
    </script>
</body>
</html>
''')

# Запуск Flask в отдельном потоке
def run_flask():
    app.run(host='0.0.0.0', port=5000)

# Основной запуск
async def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('rules', rules))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member))
    app.add_handler(CallbackQueryHandler(button_callback))
    await app.run_polling()

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(main())
