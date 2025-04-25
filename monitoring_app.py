from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from ping3 import ping
import requests
import schedule
import time
import threading
import telegram
import asyncio
import logging
from datetime import datetime, timedelta
import os

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Инициализация Flask
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////tmp/monitoring.db'  # Изменен путь на /tmp
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Конфигурация Telegram
TELEGRAM_BOT_TOKEN = "7705234760:AAGD1bFJaOeoedKPWxLOVZJYsA5jLQMhtw4"
TELEGRAM_CHAT_ID = "650154766"

# Инициализация Telegram-бота
try:
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
except telegram.error.InvalidToken as e:
    logging.error(f"Invalid Telegram token: {e}")
    bot = None

# Модели базы данных
class Service(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    url = db.Column(db.String(200), nullable=False)
    ping_host = db.Column(db.String(200), nullable=False)

class MonitoringLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(db.Integer, db.ForeignKey('service.id'), nullable=False)
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    ping_success = db.Column(db.Boolean)
    ping_time = db.Column(db.Float)
    http_status = db.Column(db.Integer)
    response_time = db.Column(db.Float)
    incident = db.Column(db.String(200))

class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    check_interval = db.Column(db.Integer, default=300)  # Интервал в секундах
    latency_threshold = db.Column(db.Float, default=1.0)  # Порог задержки в секундах
    error_threshold = db.Column(db.Integer, default=1)   # Порог ошибок

# Инициализация базы данных
with app.app_context():
    try:
        db.create_all()
        logging.info("Database initialized successfully")
    except Exception as e:
        logging.error(f"Failed to initialize database: {e}")

# Отправка уведомлений в Telegram
async def send_telegram_message(message):
    if bot is None:
        logging.warning(f"Cannot send Telegram message: Bot not initialized. Message: {message}")
        return
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logging.info(f"Telegram notification sent: {message}")
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")

# Проверка пинга
def check_ping(host):
    try:
        response_time = ping(host, timeout=2)
        if response_time is None:
            return False, None
        return True, response_time
    except Exception as e:
        logging.warning(f"Ping disabled or error for {host}: {e}. Assuming host is up.")
        return True, None  # Заглушка для платформ, где ICMP ограничен

# Проверка HTTP
def check_http(url):
    try:
        response = requests.get(url, timeout=5)
        return response.status_code, response.elapsed.total_seconds()
    except requests.RequestException as e:
        logging.error(f"HTTP request error for {url}: {e}")
        return None, None

# Функция мониторинга
def monitor_services():
    with app.app_context():
        settings = Settings.query.first()
        if not settings:
            settings = Settings()
            db.session.add(settings)
            db.session.commit()

        services = Service.query.all()
        for service in services:
            incident = None
            # Проверка пинга
            ping_success, ping_time = check_ping(service.ping_host)
            if not ping_success:
                incident = f"Пинг не удался для {service.name}"
                asyncio.run(send_telegram_message(incident))

            # Проверка HTTP
            status_code, response_time = check_http(service.url)
            if status_code:
                if response_time > settings.latency_threshold:
                    incident = f"Высокая задержка для {service.name}: {response_time}с"
                    asyncio.run(send_telegram_message(incident))
                if status_code >= 500:
                    incident = f"Ошибка сервера для {service.name}: Статус {status_code}"
                    asyncio.run(send_telegram_message(incident))
                elif status_code >= 400:
                    incident = f"Ошибка клиента для {service.name}: Статус {status_code}"
                    asyncio.run(send_telegram_message(incident))
            else:
                incident = f"HTTP-запрос не удался для {service.name}"
                asyncio.run(send_telegram_message(incident))

            # Сохранение логов
            log = MonitoringLog(
                service_id=service.id,
                ping_success=ping_success,
                ping_time=ping_time,
                http_status=status_code,
                response_time=response_time,
                incident=incident
            )
            db.session.add(log)
            db.session.commit()

# Планировщик задач
def run_scheduler():
    settings = Settings.query.first()
    interval = settings.check_interval if settings else 300
    schedule.every(interval).seconds.do(monitor_services)
    while True:
        schedule.run_pending()
        time.sleep(1)

# Запуск планировщика в отдельном потоке
scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
scheduler_thread.start()

# Маршруты Flask
@app.route('/')
def index():
    services = Service.query.all()
    settings = Settings.query.first()
    return render_template('index.html', services=services, settings=settings)

@app.route('/add_service', methods=['POST'])
def add_service():
    name = request.form['name']
    url = request.form['url']
    ping_host = request.form['ping_host']
    service = Service(name=name, url=url, ping_host=ping_host)
    db.session.add(service)
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/delete_service/<int:id>')
def delete_service(id):
    service = Service.query.get_or_404(id)
    db.session.delete(service)
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/update_settings', methods=['POST'])
def update_settings():
    settings = Settings.query.first()
    if not settings:
        settings = Settings()
        db.session.add(settings)
    settings.check_interval = int(request.form['check_interval'])
    settings.latency_threshold = float(request.form['latency_threshold'])
    settings.error_threshold = int(request.form['error_threshold'])
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/stats')
def stats():
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(days=1)
    logs = MonitoringLog.query.filter(MonitoringLog.timestamp >= start_time).all()
    
    stats = {
        'total_checks': len(logs),
        'incidents': len([log for log in logs if log.incident]),
        'avg_response_time': sum([log.response_time for log in logs if log.response_time]) / len(logs) if logs else 0,
        'errors_4xx': len([log for log in logs if log.http_status and 400 <= log.http_status < 500]),
        'errors_5xx': len([log for log in logs if log.http_status and log.http_status >= 500])
    }
    return jsonify(stats)

@app.route('/graph_data')
def graph_data():
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(days=1)
    services = Service.query.all()
    data = {}
    for service in services:
        logs = MonitoringLog.query.filter(
            MonitoringLog.service_id == service.id,
            MonitoringLog.timestamp >= start_time
        ).all()
        data[service.name] = {
            'timestamps': [log.timestamp.isoformat() for log in logs],
            'response_times': [log.response_time if log.response_time else 0 for log in logs]
        }
    return jsonify(data)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
