import os
import sqlite3
import random
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "weather.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weather (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            temperature REAL NOT NULL
        )
    """)
    conn.commit()

    # Проверяем, пустая ли таблица
    cursor.execute("SELECT COUNT(*) FROM weather")
    count = cursor.fetchone()[0]

    # Если записей нет, генерируем 100 демо-записей
    if count == 0:
        base_time = datetime.now()
        demo_data = []
        for i in range(100):
            # Шаг в 1 минуту назад для каждой последующей записи
            time_record = base_time - timedelta(minutes=i)
            str_time = time_record.strftime("%Y-%m-%d %H:%M:%S")
            # Температура 25 +- 10 градусов (от 15.0 до 35.0)
            temp = round(random.uniform(15.0, 35.0), 1)
            demo_data.append((str_time, temp))

        cursor.executemany("INSERT INTO weather (date, temperature) VALUES (?, ?)", demo_data)
        conn.commit()

    conn.close()

init_db()

# Чистый HTML-шаблон без веб-формы добавления
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Мониторинг температуры</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; background-color: #f9f9f9; color: #333; }
        h1 { color: #2c3e50; }
        table { border-collapse: collapse; width: 50%; margin-top: 20px; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        th, td { border: 1px solid #e0e0e0; padding: 12px; text-align: left; }
        th { background: #34495e; color: white; }
        tr:nth-child(even) { background: #f2f2f2; }
    </style>
</head>
<body>
    <h1>История показаний температуры</h1>
    <table>
        <tr>
            <th>Дата и время</th>
            <th>Температура</th>
        </tr>
        {% for row in rows %}
        <tr>
            <td>{{ row[0] }}</td>
            <td>{{ row[1] }} °C</td>
        </tr>
        {% endfor %}
    </table>
</body>
</html>
"""

# Главная страница с таблицей (сортировка новых записей строго сверху)
@app.route("/")
def read_index():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT date, temperature FROM weather ORDER BY date DESC")
    rows = cursor.fetchall()
    conn.close()
    return render_template_string(HTML_TEMPLATE, rows=rows)

# API метод для добавления данных (принимает как JSON, так и URL-параметры)
@app.route("/api/add", methods=["POST", "GET"])
def add_api():
    if request.is_json:
        data = request.get_json()
        date = data.get("date")
        temperature = data.get("temperature")
    else:
        date = request.args.get("date")
        temperature = request.args.get("temperature")

    if not date or temperature is None:
        return jsonify({"status": "error", "message": "Missing date or temperature"}), 400

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO weather (date, temperature) VALUES (?, ?)", (date, float(temperature)))
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "date": date, "temperature": float(temperature)})
