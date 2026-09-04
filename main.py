import os
import sqlite3
import random
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template_string, request, jsonify

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_db_path():
    # На Amvera SQLite должен жить в постоянном томе /data
    if "AMVERA" in os.environ or os.path.isdir("/data"):
        data_dir = "/data"
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "weather.db")
    return os.path.join(BASE_DIR, "weather.db")


DB_PATH = resolve_db_path()

def ensure_column(cursor, name, typedef):
    cursor.execute("PRAGMA table_info(weather)")
    columns = [row[1] for row in cursor.fetchall()]
    if name not in columns:
        cursor.execute("ALTER TABLE weather ADD COLUMN %s %s" % (name, typedef))


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weather (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            temperature REAL,
            humidity REAL,
            temperature2 REAL,
            humidity2 REAL,
            mq4 REAL,
            mq4_alarm INTEGER
        )
    """)
    ensure_column(cursor, "humidity", "REAL")
    ensure_column(cursor, "temperature2", "REAL")
    ensure_column(cursor, "humidity2", "REAL")
    ensure_column(cursor, "mq4", "REAL")
    ensure_column(cursor, "mq4_alarm", "INTEGER")

    cursor.execute("PRAGMA table_info(weather)")
    temp_col = [row for row in cursor.fetchall() if row[1] == "temperature"]
    if temp_col and temp_col[0][3]:
        cursor.execute("ALTER TABLE weather RENAME TO weather_old")
        cursor.execute("""
            CREATE TABLE weather (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                temperature REAL,
                humidity REAL,
                temperature2 REAL,
                humidity2 REAL,
                mq4 REAL,
                mq4_alarm INTEGER
            )
        """)
        cursor.execute("""
            INSERT INTO weather (id, date, temperature, humidity, temperature2, humidity2, mq4, mq4_alarm)
            SELECT id, date, temperature, humidity, temperature2, humidity2, mq4, mq4_alarm
            FROM weather_old
        """)
        cursor.execute("DROP TABLE weather_old")

    conn.commit()
    conn.close()

init_db()

# То же смещение, что на Pico (send_temp.py TZ_OFFSET_HOURS)
TZ_OFFSET_HOURS = 7


def local_now():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=TZ_OFFSET_HOURS)


def default_period():
    now = local_now()
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
    return render_template_string(HTML_TEMPLATE)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Мониторинг температуры и влажности</title>
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
        .chart-wrap + .chart-wrap { margin-top: 16px; }
    </style>
</head>
<body>
    <h1>График температуры, влажности и MQ-4</h1>

    <div class="controls">
        <div class="field">
            <label for="dateFrom">От</label>
            <input type="datetime-local" id="dateFrom">
        </div>
        <div class="field">
            <label for="dateTo">До</label>
            <input type="datetime-local" id="dateTo">
        </div>
        <button type="button" id="applyBtn">Показать</button>
        <button type="button" id="resetBtn">Последние 24 часа</button>
    </div>

    <div class="chart-wrap">
        <canvas id="tempChart"></canvas>
        <p id="emptyMsg" class="empty-msg" style="display: none;">За выбранный период данных нет.</p>
    </div>
    <div class="chart-wrap">
        <canvas id="mqChart"></canvas>
    </div>

    <script>
        const ctx = document.getElementById('tempChart');
        const mqCtx = document.getElementById('mqChart');
        let chart = null;
        let mqChart = null;
        const TZ_OFFSET_HOURS = 7;

        function pad2(n) {
            return String(n).padStart(2, '0');
        }

        function toLocalInputValue(date) {
            const shifted = new Date(date.getTime() + TZ_OFFSET_HOURS * 3600000);
            return (
                shifted.getUTCFullYear() + '-' +
                pad2(shifted.getUTCMonth() + 1) + '-' +
                pad2(shifted.getUTCDate()) + 'T' +
                pad2(shifted.getUTCHours()) + ':' +
                pad2(shifted.getUTCMinutes())
            );
        }

        function last24hRange() {
            const to = new Date();
            const from = new Date(to.getTime() - 24 * 3600000);
            return [toLocalInputValue(from), toLocalInputValue(to)];
        }

        function setPeriodInputs(fromValue, toValue) {
            document.getElementById('dateFrom').value = fromValue;
            document.getElementById('dateTo').value = toValue;
        }

        function buildChart(rows) {
            if (chart) {
                chart.destroy();
            }
            if (mqChart) {
                mqChart.destroy();
            }

            const emptyMsg = document.getElementById('emptyMsg');
            const labels = rows.map(item => item.date);
            if (!labels.length) {
                emptyMsg.style.display = 'block';
                return;
            }

            emptyMsg.style.display = 'none';
            chart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [
                        {
                            label: 'T1, °C',
                            data: rows.map(item => item.temperature),
                            borderColor: '#e74c3c',
                            tension: 0.2,
                            fill: false,
                            pointRadius: 2,
                            yAxisID: 'yTemp'
                        },
                        {
                            label: 'T2, °C',
                            data: rows.map(item => item.temperature2),
                            borderColor: '#c0392b',
                            borderDash: [6, 4],
                            tension: 0.2,
                            fill: false,
                            pointRadius: 2,
                            yAxisID: 'yTemp'
                        },
                        {
                            label: 'RH1, %',
                            data: rows.map(item => item.humidity),
                            borderColor: '#3498db',
                            tension: 0.2,
                            fill: false,
                            pointRadius: 2,
                            yAxisID: 'yHum'
                        },
                        {
                            label: 'RH2, %',
                            data: rows.map(item => item.humidity2),
                            borderColor: '#1abc9c',
                            borderDash: [6, 4],
                            tension: 0.2,
                            fill: false,
                            pointRadius: 2,
                            yAxisID: 'yHum'
                        }
                    ]
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
                        yTemp: {
                            type: 'linear',
                            position: 'left',
                            title: { display: true, text: 'Температура, °C' }
                        },
                        yHum: {
                            type: 'linear',
                            position: 'right',
                            min: 0,
                            max: 100,
                            title: { display: true, text: 'Влажность, %' },
                            grid: { drawOnChartArea: false }
                        }
                    },
                    plugins: {
                        legend: { display: true }
                    }
                }
            });

            mqChart = new Chart(mqCtx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'MQ-4, мВ',
                        data: rows.map(item => item.mq4),
                        borderColor: '#8e44ad',
                        tension: 0.2,
                        fill: false,
                        pointRadius: 2
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
                            title: { display: true, text: 'MQ-4, мВ на GP26' }
                        }
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
            buildChart(data);
        }

        document.getElementById('applyBtn').addEventListener('click', () => {
            loadChart(
                document.getElementById('dateFrom').value,
                document.getElementById('dateTo').value
            );
        });

        document.getElementById('resetBtn').addEventListener('click', () => {
            const [fromValue, toValue] = last24hRange();
            setPeriodInputs(fromValue, toValue);
            loadChart(fromValue, toValue);
        });

        {
            const [fromValue, toValue] = last24hRange();
            setPeriodInputs(fromValue, toValue);
            loadChart(fromValue, toValue);
        }
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
        SELECT date, temperature, humidity, temperature2, humidity2, mq4, mq4_alarm
        FROM weather
        WHERE date >= ? AND date <= ?
        ORDER BY date ASC
        """,
        (format_datetime(date_from), format_datetime(date_to)),
    )
    rows = cursor.fetchall()
    conn.close()

    data = [{
        "date": row[0],
        "temperature": row[1],
        "humidity": row[2],
        "temperature2": row[3],
        "humidity2": row[4],
        "mq4": row[5],
        "mq4_alarm": row[6],
    } for row in rows]

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
        humidity = data.get("humidity")
        temperature2 = data.get("temperature2")
        humidity2 = data.get("humidity2")
        mq4 = data.get("mq4")
        mq4_alarm = data.get("mq4_alarm")
    else:
        date = request.args.get("date")
        temperature = request.args.get("temperature")
        humidity = request.args.get("humidity")
        temperature2 = request.args.get("temperature2")
        humidity2 = request.args.get("humidity2")
        mq4 = request.args.get("mq4")
        mq4_alarm = request.args.get("mq4_alarm")

    if not date:
        return jsonify({"status": "error", "message": "Missing date"}), 400
    if temperature is None and temperature2 is None and mq4 is None:
        return jsonify({"status": "error", "message": "Missing sensor data"}), 400

    def as_float(value):
        if value is None or value == "":
            return None
        return float(value)

    def as_int(value):
        if value is None or value == "":
            return None
        return int(float(value))

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO weather (date, temperature, humidity, temperature2, humidity2, mq4, mq4_alarm)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date,
            as_float(temperature),
            as_float(humidity),
            as_float(temperature2),
            as_float(humidity2),
            as_float(mq4),
            as_int(mq4_alarm),
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({
        "status": "success",
        "date": date,
        "temperature": as_float(temperature),
        "humidity": as_float(humidity),
        "temperature2": as_float(temperature2),
        "humidity2": as_float(humidity2),
        "mq4": as_float(mq4),
        "mq4_alarm": as_int(mq4_alarm),
    })
