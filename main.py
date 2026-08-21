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

    # Если записей нет, генерируем демо-записи за последние 25 часов (шаг 1 минута)
    if count == 0:
        base_time = datetime.now()
        demo_data = []
        for i in range(1500):
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

def default_period():
    now = datetime.now()
    return now - timedelta(hours=24), now

def parse_datetime(value):
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None

def format_datetime(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def format_datetime_local(dt):
    return dt.strftime("%Y-%m-%dT%H:%M")

# Главная страница с графиком температуры
@app.route("/")
def read_index():
    date_from, date_to = default_period()
    return render_template_string(
        HTML_TEMPLATE,
        default_from=format_datetime_local(date_from),
        default_to=format_datetime_local(date_to),
    )

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Мониторинг температуры</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; background-color: #f9f9f9; color: #333; }
        h1 { color: #2c3e50; }
        .controls {
            display: flex;
            flex-wrap: wrap;
            gap: 16px;
            align-items: end;
            margin: 24px 0;
            padding: 16px;
            background: white;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            border-radius: 6px;
        }
        .field label { display: block; margin-bottom: 6px; font-size: 14px; color: #555; }
        .field input {
            padding: 8px 10px;
            border: 1px solid #ccc;
            border-radius: 4px;
            font-size: 14px;
        }
        button {
            padding: 9px 18px;
            border: none;
            border-radius: 4px;
            background: #34495e;
            color: white;
            cursor: pointer;
            font-size: 14px;
        }
        button:hover { background: #2c3e50; }
        .chart-wrap {
            background: white;
            padding: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            border-radius: 6px;
        }
        .empty-msg { margin-top: 12px; color: #777; }
    </style>
</head>
<body>
    <h1>График температуры</h1>

    <div class="controls">
        <div class="field">
            <label for="dateFrom">От</label>
            <input type="datetime-local" id="dateFrom" value="{{ default_from }}">
        </div>
        <div class="field">
            <label for="dateTo">До</label>
            <input type="datetime-local" id="dateTo" value="{{ default_to }}">
        </div>
        <button type="button" id="applyBtn">Показать</button>
        <button type="button" id="resetBtn">Последние 24 часа</button>
    </div>

    <div class="chart-wrap">
        <canvas id="tempChart"></canvas>
        <p id="emptyMsg" class="empty-msg" style="display: none;">За выбранный период данных нет.</p>
    </div>

    <script>
        const ctx = document.getElementById('tempChart');
        let chart = null;

        function buildChart(labels, values) {
            if (chart) {
                chart.destroy();
            }

            const emptyMsg = document.getElementById('emptyMsg');
            if (!labels.length) {
                emptyMsg.style.display = 'block';
                return;
            }

            emptyMsg.style.display = 'none';
            chart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Температура, °C',
                        data: values,
                        borderColor: '#e74c3c',
                        backgroundColor: 'rgba(231, 76, 60, 0.1)',
                        tension: 0.2,
                        fill: true,
                        pointRadius: 2,
                        pointHoverRadius: 5
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: true,
                    aspectRatio: 2.5,
                    scales: {
                        x: {
                            title: { display: true, text: 'Дата и время' },
                            ticks: { maxRotation: 45, minRotation: 0 }
                        },
                        y: {
                            title: { display: true, text: 'Температура, °C' }
                        }
                    },
                    plugins: {
                        legend: { display: true }
                    }
                }
            });
        }

        async function loadChart(fromValue, toValue) {
            const params = new URLSearchParams();
            if (fromValue) params.set('from', fromValue);
            if (toValue) params.set('to', toValue);

            const response = await fetch('/api/data?' + params.toString());
            const data = await response.json();

            buildChart(
                data.map(item => item.date),
                data.map(item => item.temperature)
            );
        }

        document.getElementById('applyBtn').addEventListener('click', () => {
            loadChart(
                document.getElementById('dateFrom').value,
                document.getElementById('dateTo').value
            );
        });

        document.getElementById('resetBtn').addEventListener('click', async () => {
            const response = await fetch('/api/data');
            const meta = await response.json();
            document.getElementById('dateFrom').value = meta.period_from;
            document.getElementById('dateTo').value = meta.period_to;
            buildChart(
                meta.data.map(item => item.date),
                meta.data.map(item => item.temperature)
            );
        });

        loadChart('{{ default_from }}', '{{ default_to }}');
    </script>
</body>
</html>
"""

@app.route("/api/data")
def get_data():
    date_from_raw = request.args.get("from")
    date_to_raw = request.args.get("to")

    if date_from_raw and date_to_raw:
        date_from = parse_datetime(date_from_raw)
        date_to = parse_datetime(date_to_raw)
        if not date_from or not date_to:
            return jsonify({"status": "error", "message": "Invalid date format"}), 400
        if date_from > date_to:
            return jsonify({"status": "error", "message": "'from' must be before 'to'"}), 400
    else:
        date_from, date_to = default_period()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT date, temperature
        FROM weather
        WHERE date >= ? AND date <= ?
        ORDER BY date ASC
        """,
        (format_datetime(date_from), format_datetime(date_to)),
    )
    rows = cursor.fetchall()
    conn.close()

    data = [{"date": row[0], "temperature": row[1]} for row in rows]

    if not date_from_raw and not date_to_raw:
        return jsonify({
            "period_from": format_datetime_local(date_from),
            "period_to": format_datetime_local(date_to),
            "data": data,
        })

    return jsonify(data)

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
