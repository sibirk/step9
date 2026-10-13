import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote
from flask import Flask, render_template_string, request, jsonify, send_file


app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Один архив обновления АЗС. Запас на диске оставляем под базу и журнал.
UPDATE_NAME = "azs_update.zip"
MAX_UPDATE_BYTES = 150 * 1024 * 1024
DISK_RESERVE_BYTES = 64 * 1024 * 1024
LOW_DISK_BYTES = 200 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_UPDATE_BYTES


def resolve_db_path():
    # На Amvera SQLite должен жить в постоянном томе /data
    if "AMVERA" in os.environ or os.path.isdir("/data"):
        data_dir = "/data"
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "weather.db")
    return os.path.join(BASE_DIR, "weather.db")


DB_PATH = resolve_db_path()

# Совпадает с API_KEY на Pico (send_temp.py). На Amvera можно задать STEP9_API_KEY.
API_KEY = os.environ.get("STEP9_API_KEY") or os.environ.get("API_KEY") or "2s9-8f3aC1eB7dQ4nP6wK9"


def api_key_ok():
    header = request.headers.get("X-Api-Key") or ""
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        auth = auth[7:].strip()
    body_key = ""
    if request.is_json:
        body = request.get_json(silent=True) or {}
        body_key = body.get("api_key") or ""
    provided = header or auth or body_key or request.args.get("api_key") or ""
    return bool(API_KEY) and provided == API_KEY


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

    ensure_column(cursor, "ds1", "REAL")
    ensure_column(cursor, "ds2", "REAL")
    ensure_column(cursor, "ds3", "REAL")
    ensure_column(cursor, "ds4", "REAL")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS phone_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL,
            local_id INTEGER NOT NULL,
            at_ms INTEGER NOT NULL,
            text TEXT NOT NULL,
            received TEXT NOT NULL,
            UNIQUE(device, local_id)
        )
    """)

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


def storage_dir():
    return os.path.dirname(DB_PATH)


def update_path():
    return os.path.join(storage_dir(), UPDATE_NAME)


def update_meta_path():
    return os.path.join(storage_dir(), "azs_update.json")


def format_bytes(size):
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            if unit == "Б":
                return "%d %s" % (int(value), unit)
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%d Б" % int(size)


def disk_info():
    usage = shutil.disk_usage(storage_dir())
    total = usage.total or 1
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "total_text": format_bytes(usage.total),
        "used_text": format_bytes(usage.used),
        "free_text": format_bytes(usage.free),
        "used_percent": min(100, int(usage.used * 100 / total)),
        "low": usage.free < LOW_DISK_BYTES,
    }


def read_update_meta():
    path = update_meta_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update_info():
    path = update_path()
    info = {"available": False}
    if os.path.isfile(path):
        meta = read_update_meta()
        size = os.path.getsize(path)
        uploaded = meta.get("uploaded_at")
        if not uploaded:
            uploaded = format_datetime(datetime.fromtimestamp(os.path.getmtime(path)))
        info = {
            "available": True,
            "name": meta.get("original_name") or UPDATE_NAME,
            "size": size,
            "size_text": format_bytes(size),
            "sha256": meta.get("sha256") or "",
            "uploaded_at": uploaded,
        }
    info["disk"] = disk_info()
    return info


def remove_quiet(path):
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def cleanup_update_parts():
    remove_quiet(update_path() + ".part")
    remove_quiet(update_meta_path() + ".part")


def clean_upload_name(value):
    name = os.path.basename((value or "").replace("\\", "/")).strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '\"<>:|?*')
    name = name[:180].strip() or UPDATE_NAME
    if not name.lower().endswith(".zip"):
        name += ".zip"
    return name


class UploadError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def prepare_disk_for_upload(need_bytes):
    """Освобождает место под новый архив. Старый файл удаляется только если иначе не влезет."""
    free = shutil.disk_usage(storage_dir()).free
    path = update_path()
    current = os.path.getsize(path) if os.path.isfile(path) else 0
    if free >= need_bytes + DISK_RESERVE_BYTES:
        return False
    if current and free + current >= need_bytes + DISK_RESERVE_BYTES:
        remove_quiet(path)
        remove_quiet(update_meta_path())
        return True
    raise UploadError(
        "Не хватает места: свободно %s, для файла нужно %s и ещё %s запаса под базу."
        % (format_bytes(free), format_bytes(need_bytes), format_bytes(DISK_RESERVE_BYTES)),
        507,
    )


def save_update_stream(length, original_name):
    removed_old = prepare_disk_for_upload(length)
    part = update_path() + ".part"
    digest = hashlib.sha256()
    written = 0
    header = b""
    try:
        with open(part, "wb") as out:
            remaining = length
            while remaining > 0:
                chunk = request.stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                if written == 0:
                    header = chunk[:4]
                written += len(chunk)
                remaining -= len(chunk)
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if written != length:
            raise UploadError("Загрузка оборвалась", 400)
        if written < 4 or not header.startswith(b"PK"):
            raise UploadError("Это не zip-архив", 400)
        os.replace(part, update_path())
    except Exception as exc:
        remove_quiet(part)
        if removed_old:
            text = str(exc) or "Загрузка не завершилась"
            code = exc.code if isinstance(exc, UploadError) else 500
            raise UploadError(
                text + " Предыдущий файл уже удалён — загрузите архив ещё раз.",
                code,
            )
        raise
    meta = {
        "original_name": original_name,
        "size": written,
        "sha256": digest.hexdigest(),
        "uploaded_at": format_datetime(local_now()),
    }
    meta_part = update_meta_path() + ".part"
    with open(meta_part, "w", encoding="utf-8") as handle:
        json.dump(meta, handle, ensure_ascii=False)
    os.replace(meta_part, update_meta_path())
    return update_info()


cleanup_update_parts()


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <rect x="12" y="2" width="8" height="18" rx="4" fill="#dff3fb" stroke="#3498db" stroke-width="2"/>
  <rect x="14" y="12" width="4" height="10" fill="#e74c3c"/>
  <circle cx="16" cy="24" r="7" fill="#e74c3c"/>
</svg>
"""


@app.route("/favicon.svg")
def favicon():
    return app.response_class(FAVICON_SVG, mimetype="image/svg+xml")


# Главная страница с графиком температуры
@app.route("/")
def read_index():
    return render_template_string(HTML_TEMPLATE, disk=disk_info())

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Мониторинг температуры и влажности</title>
    <link rel="icon" href="/favicon.svg" type="image/svg+xml">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; background-color: #f9f9f9; color: #333; }
        h1 { color: #2c3e50; }
        a.nav-btn {
            display: inline-block;
            margin: 0 8px 8px 0;
            padding: 9px 18px;
            border-radius: 4px;
            background: #0d47a1;
            color: white;
            text-decoration: none;
            font-size: 14px;
        }
        a.nav-btn:hover { background: #1565c0; }
        .disk-line { margin: 4px 0 16px; color: #555; font-size: 14px; }
        .disk-line strong { color: #1e8449; }
        .disk-line.disk-low strong { color: #c0392b; }
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
        button:disabled, .field input:disabled { opacity: 0.6; cursor: wait; }
        .loading-msg {
            display: flex;
            align-items: center;
            gap: 10px;
            margin: 0 0 12px;
            color: #2c3e50;
            font-size: 15px;
        }
        .loading-msg[hidden] { display: none; }
        .spinner {
            width: 16px;
            height: 16px;
            border: 2px solid #d5d8dc;
            border-top-color: #34495e;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        #charts.charts-loading { opacity: 0.45; }
        .chart-wrap {
            background: white;
            padding: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            border-radius: 6px;
        }
        .empty-msg { margin-top: 12px; color: #777; }
        .chart-wrap + .chart-wrap { margin-top: 16px; }
        h2 { color: #2c3e50; font-size: 18px; margin: 0 0 12px; }
    </style>
</head>
<body>
    <h1>Графики датчиков</h1>
    <a class="nav-btn" href="/journal">Журнал МП</a>
    <a class="nav-btn" href="/update">Обновление АЗС</a>
    <p class="disk-line{% if disk.low %} disk-low{% endif %}">
        Свободно на диске: <strong>{{ disk.free_text }}</strong> из {{ disk.total_text }}
        (занято {{ disk.used_text }})
    </p>

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
        <button type="button" id="resetBtn">24 часа</button>
        <button type="button" data-days="3">3 дня</button>
        <button type="button" data-days="7">7 дней</button>
        <button type="button" data-days="30">30 дней</button>
    </div>

    <p id="loadingMsg" class="loading-msg" hidden>
        <span class="spinner" aria-hidden="true"></span>
        Загрузка данных…
    </p>
    <p id="emptyMsg" class="empty-msg" style="display: none;">За выбранный период данных нет.</p>
    <div id="charts">
        <div class="chart-wrap">
            <h2>Температура</h2>
            <canvas id="tempChart"></canvas>
        </div>
        <div class="chart-wrap">
            <h2>Влажность</h2>
            <canvas id="humChart"></canvas>
        </div>
        <div class="chart-wrap">
            <h2>MQ-4</h2>
            <canvas id="mqChart"></canvas>
        </div>
        <div class="chart-wrap">
            <h2>DS18B20</h2>
            <canvas id="dsChart"></canvas>
        </div>
    </div>

    <script>
        const ctx = document.getElementById('tempChart');
        const humCtx = document.getElementById('humChart');
        const mqCtx = document.getElementById('mqChart');
        const dsCtx = document.getElementById('dsChart');
        let chart = null;
        let humChart = null;
        let mqChart = null;
        let dsChart = null;
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

        function recentRange(days) {
            const to = new Date();
            const from = new Date(to.getTime() - days * 24 * 3600000);
            return [toLocalInputValue(from), toLocalInputValue(to)];
        }

        function applyRecent(days) {
            const [fromValue, toValue] = recentRange(days);
            setPeriodInputs(fromValue, toValue);
            loadChart(fromValue, toValue);
        }

        function setPeriodInputs(fromValue, toValue) {
            document.getElementById('dateFrom').value = fromValue;
            document.getElementById('dateTo').value = toValue;
        }

        function destroyCharts() {
            if (chart) {
                chart.destroy();
                chart = null;
            }
            if (humChart) {
                humChart.destroy();
                humChart = null;
            }
            if (mqChart) {
                mqChart.destroy();
                mqChart = null;
            }
            if (dsChart) {
                dsChart.destroy();
                dsChart = null;
            }
        }

        function timeScale() {
            return {
                title: { display: true, text: 'Дата и время' },
                ticks: { maxRotation: 45, minRotation: 0 }
            };
        }

        function buildChart(rows) {
            destroyCharts();

            const emptyMsg = document.getElementById('emptyMsg');
            const charts = document.getElementById('charts');
            const labels = rows.map(item => item.date);
            if (!labels.length) {
                emptyMsg.style.display = 'block';
                charts.style.display = 'none';
                return;
            }

            emptyMsg.style.display = 'none';
            charts.style.display = 'block';
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
                            pointRadius: 2
                        },
                        {
                            label: 'T2, °C',
                            data: rows.map(item => item.temperature2),
                            borderColor: '#27ae60',
                            tension: 0.2,
                            fill: false,
                            pointRadius: 2
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: true,
                    aspectRatio: 2.5,
                    scales: {
                        x: timeScale(),
                        y: {
                            type: 'linear',
                            position: 'left',
                            title: { display: true, text: 'Температура, °C' }
                        }
                    },
                    plugins: {
                        legend: { display: true }
                    }
                }
            });
            humChart = new Chart(humCtx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [
                        {
                            label: 'RH1, %',
                            data: rows.map(item => item.humidity),
                            borderColor: '#3498db',
                            tension: 0.2,
                            fill: false,
                            pointRadius: 2
                        },
                        {
                            label: 'RH2, %',
                            data: rows.map(item => item.humidity2),
                            borderColor: '#e74c3c',
                            tension: 0.2,
                            fill: false,
                            pointRadius: 2
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: true,
                    aspectRatio: 2.5,
                    scales: {
                        x: timeScale(),
                        y: {
                            type: 'linear',
                            position: 'left',
                            min: 0,
                            max: 100,
                            title: { display: true, text: 'Влажность, %' }
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
                    datasets: [
                        {
                            label: 'MQ-4, мВ',
                            data: rows.map(item => item.mq4),
                            borderColor: '#8e44ad',
                            tension: 0.2,
                            fill: false,
                            pointRadius: 2
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: true,
                    aspectRatio: 2.5,
                    scales: {
                        x: timeScale(),
                        y: {
                            type: 'linear',
                            position: 'left',
                            title: { display: true, text: 'MQ-4, мВ' }
                        }
                    },
                    plugins: {
                        legend: { display: true }
                    }
                }
            });
            dsChart = new Chart(dsCtx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [
                        {
                            label: 'DS1, °C',
                            data: rows.map(item => item.ds1),
                            borderColor: '#e67e22',
                            tension: 0.2,
                            fill: false,
                            pointRadius: 2,
                            spanGaps: true
                        },
                        {
                            label: 'DS2, °C',
                            data: rows.map(item => item.ds2),
                            borderColor: '#f1c40f',
                            tension: 0.2,
                            fill: false,
                            pointRadius: 2,
                            spanGaps: true
                        },
                        {
                            label: 'DS3, °C',
                            data: rows.map(item => item.ds3),
                            borderColor: '#16a085',
                            tension: 0.2,
                            fill: false,
                            pointRadius: 2,
                            spanGaps: true
                        },
                        {
                            label: 'DS4, °C',
                            data: rows.map(item => item.ds4),
                            borderColor: '#2c3e50',
                            tension: 0.2,
                            fill: false,
                            pointRadius: 2,
                            spanGaps: true
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: true,
                    aspectRatio: 2.5,
                    scales: {
                        x: timeScale(),
                        y: {
                            type: 'linear',
                            position: 'left',
                            title: { display: true, text: 'Температура, °C' }
                        }
                    },
                    plugins: {
                        legend: { display: true }
                    }
                }
            });
        }

        let loadSeq = 0;

        function setLoading(isLoading) {
            document.getElementById('loadingMsg').hidden = !isLoading;
            document.getElementById('charts').classList.toggle('charts-loading', isLoading);
            document.getElementById('charts').setAttribute('aria-busy', isLoading ? 'true' : 'false');
            document.querySelectorAll('.controls button, .controls input').forEach(el => {
                el.disabled = isLoading;
            });
        }

        async function loadChart(fromValue, toValue) {
            const seq = ++loadSeq;
            setLoading(true);
            try {
                const params = new URLSearchParams();
                if (fromValue) params.set('from', fromValue);
                if (toValue) params.set('to', toValue);

                const response = await fetch('/api/data?' + params.toString());
                const data = await response.json();
                if (seq !== loadSeq) return;
                buildChart(data);
            } finally {
                if (seq === loadSeq) setLoading(false);
            }
        }

        document.getElementById('applyBtn').addEventListener('click', () => {
            loadChart(
                document.getElementById('dateFrom').value,
                document.getElementById('dateTo').value
            );
        });

        document.getElementById('resetBtn').addEventListener('click', () => {
            applyRecent(1);
        });

        document.querySelectorAll('button[data-days]').forEach(button => {
            button.addEventListener('click', () => {
                applyRecent(Number(button.dataset.days));
            });
        });

        applyRecent(1);
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
        SELECT date, temperature, humidity, temperature2, humidity2, mq4, mq4_alarm,
               ds1, ds2, ds3, ds4
        FROM weather
        WHERE date >= ? AND date <= ?
        ORDER BY date ASC
        """,
        (format_datetime(date_from), format_datetime(date_to)),
    )
    rows = cursor.fetchall()
    conn.close()

    # Длинный период рисуется долго: оставляем каждую третью точку и последнюю.
    if date_to - date_from > timedelta(days=10) and len(rows) > 1:
        last = rows[-1]
        rows = rows[::3]
        if rows[-1] is not last:
            rows.append(last)

    data = [{
        "date": row[0],
        "temperature": row[1],
        "humidity": row[2],
        "temperature2": row[3],
        "humidity2": row[4],
        "mq4": row[5],
        "mq4_alarm": row[6],
        "ds1": row[7],
        "ds2": row[8],
        "ds3": row[9],
        "ds4": row[10],
    } for row in rows]

    if not date_from_raw and not date_to_raw:
        return jsonify({
            "period_from": format_datetime_local(date_from),
            "period_to": format_datetime_local(date_to),
            "data": data,
        })

    return jsonify(data)

JOURNAL_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Журнал МП</title>
    <link rel="icon" href="/favicon.svg" type="image/svg+xml">
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; background-color: #f9f9f9; color: #333; }
        h1 { color: #2c3e50; }
        a.nav-btn {
            display: inline-block;
            margin: 0 8px 16px 0;
            padding: 9px 18px;
            border-radius: 4px;
            background: #34495e;
            color: white;
            text-decoration: none;
            font-size: 14px;
        }
        a.nav-btn:hover { background: #2c3e50; }
        .disk-line { margin: 4px 0 16px; color: #555; font-size: 14px; }
        .disk-line strong { color: #1e8449; }
        .disk-line.disk-low strong { color: #c0392b; }
        table { border-collapse: collapse; width: 100%; background: white; }
        th, td { border-bottom: 1px solid #e6e6e6; padding: 8px 10px; text-align: left; vertical-align: top; }
        th { background: #f4f6f7; color: #555; font-size: 13px; }
        td { font-size: 14px; }
        .when { white-space: nowrap; }
        .device { color: #777; font-size: 12px; white-space: nowrap; }
        .empty { color: #777; }
    </style>
</head>
<body>
    <h1>Журнал МП</h1>
    <a class="nav-btn" href="/">К графикам</a>
    <a class="nav-btn" href="/update">Обновление АЗС</a>
    <a class="nav-btn" href="/journal">Обновить</a>
    <p class="disk-line{% if disk.low %} disk-low{% endif %}">
        Свободно на диске: <strong>{{ disk.free_text }}</strong> из {{ disk.total_text }}
        (занято {{ disk.used_text }})
    </p>
    {% if rows %}
    <table>
        <tr>
            <th>Время</th>
            <th>Телефон</th>
            <th>Событие</th>
        </tr>
        {% for row in rows %}
        <tr>
            <td class="when">{{ row.when }}</td>
            <td class="device">{{ row.device }}</td>
            <td>{{ row.text }}</td>
        </tr>
        {% endfor %}
    </table>
    {% else %}
    <p class="empty">Записей пока нет.</p>
    {% endif %}
</body>
</html>
"""


def epoch_local(ms):
    moment = datetime.fromtimestamp(ms / 1000.0, timezone.utc).replace(tzinfo=None)
    return format_datetime(moment + timedelta(hours=TZ_OFFSET_HOURS))


@app.route("/journal")
def journal_page():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT at_ms, device, text
        FROM phone_journal
        ORDER BY at_ms DESC, id DESC
        LIMIT 400
        """
    )
    rows = cursor.fetchall()
    conn.close()
    items = [{
        "when": epoch_local(row[0]) if row[0] else "",
        "device": row[1],
        "text": row[2],
    } for row in rows]
    return render_template_string(JOURNAL_HTML, rows=items, disk=disk_info())


@app.route("/api/journal", methods=["POST"])
def add_journal():
    if not api_key_ok():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    device = str(data.get("device") or "").strip()[:64]
    events = data.get("events")
    if not device or not isinstance(events, list):
        return jsonify({"status": "error", "message": "Missing device or events"}), 400
    if len(events) > 50:
        return jsonify({"status": "error", "message": "Too many events"}), 400
    received = format_datetime(local_now())
    rows = []
    for item in events:
        if not isinstance(item, dict):
            continue
        try:
            local_id = int(item.get("id"))
            at_ms = int(item.get("at"))
        except (TypeError, ValueError):
            continue
        text = str(item.get("text") or "").replace("\x00", "").strip()
        if not text or local_id <= 0:
            continue
        rows.append((device, local_id, at_ms, text[:500], received))
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.executemany(
        """
        INSERT OR IGNORE INTO phone_journal (device, local_id, at_ms, text, received)
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "success", "accepted": len(rows)})


# API метод для добавления данных (принимает как JSON, так и URL-параметры)
@app.route("/api/add", methods=["POST", "GET"])
def add_api():
    if not api_key_ok():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    if request.is_json:
        data = request.get_json()
        date = data.get("date")
        temperature = data.get("temperature")
        humidity = data.get("humidity")
        temperature2 = data.get("temperature2")
        humidity2 = data.get("humidity2")
        mq4 = data.get("mq4")
        mq4_alarm = data.get("mq4_alarm")
        ds1 = data.get("ds1")
        ds2 = data.get("ds2")
        ds3 = data.get("ds3")
        ds4 = data.get("ds4")
    else:
        date = request.args.get("date")
        temperature = request.args.get("temperature")
        humidity = request.args.get("humidity")
        temperature2 = request.args.get("temperature2")
        humidity2 = request.args.get("humidity2")
        mq4 = request.args.get("mq4")
        mq4_alarm = request.args.get("mq4_alarm")
        ds1 = request.args.get("ds1")
        ds2 = request.args.get("ds2")
        ds3 = request.args.get("ds3")
        ds4 = request.args.get("ds4")

    if not date:
        return jsonify({"status": "error", "message": "Missing date"}), 400
    if (
        temperature is None
        and temperature2 is None
        and mq4 is None
        and ds1 is None
        and ds2 is None
        and ds3 is None
        and ds4 is None
    ):
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
        INSERT INTO weather (
            date, temperature, humidity, temperature2, humidity2, mq4, mq4_alarm,
            ds1, ds2, ds3, ds4
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date,
            as_float(temperature),
            as_float(humidity),
            as_float(temperature2),
            as_float(humidity2),
            as_float(mq4),
            as_int(mq4_alarm),
            as_float(ds1),
            as_float(ds2),
            as_float(ds3),
            as_float(ds4),
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
        "ds1": as_float(ds1),
        "ds2": as_float(ds2),
        "ds3": as_float(ds3),
        "ds4": as_float(ds4),
    })


UPDATE_HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Обновление АЗС</title>
    <link rel="icon" href="/favicon.svg" type="image/svg+xml">
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; background-color: #f9f9f9; color: #333; }
        h1, h2 { color: #2c3e50; }
        h2 { font-size: 18px; margin: 28px 0 12px; }
        a.nav-btn {
            display: inline-block;
            margin: 0 8px 8px 0;
            padding: 9px 18px;
            border-radius: 4px;
            background: #34495e;
            color: white;
            text-decoration: none;
            font-size: 14px;
        }
        a.nav-btn:hover { background: #2c3e50; }
        .card {
            background: white;
            padding: 16px 20px;
            border-radius: 6px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            margin: 0 0 16px;
            max-width: 720px;
        }
        .disk-line { margin: 0; color: #555; font-size: 15px; }
        .disk-line strong { color: #1e8449; }
        .card.disk-low .disk-line strong { color: #c0392b; }
        .warn { color: #c0392b; margin: 10px 0 0; }
        .disk-bar { height: 14px; background: #e8e8e8; border-radius: 7px; overflow: hidden; margin-top: 12px; }
        .disk-bar > div { height: 100%; background: #1e8449; }
        .card.disk-low .disk-bar > div { background: #c0392b; }
        label { display: block; margin: 12px 0 6px; font-size: 14px; color: #555; }
        input[type="password"], input[type="file"] { font-size: 14px; }
        input[type="password"] {
            padding: 8px 10px;
            border: 1px solid #ccc;
            border-radius: 4px;
            width: 280px;
        }
        button {
            margin: 16px 8px 0 0;
            padding: 9px 18px;
            border: none;
            border-radius: 4px;
            background: #34495e;
            color: white;
            cursor: pointer;
            font-size: 14px;
        }
        button:hover { background: #2c3e50; }
        button:disabled { opacity: 0.6; cursor: wait; }
        button.danger { background: #922b21; }
        button.danger:hover { background: #7b241c; }
        .progress { height: 10px; background: #e8e8e8; border-radius: 5px; overflow: hidden; margin-top: 14px; }
        .progress > div { height: 100%; width: 0; background: #0d47a1; }
        .progress[hidden] { display: none; }
        .msg { margin-top: 12px; }
        .msg.error { color: #c0392b; }
        .msg.ok { color: #1e8449; }
        .meta { color: #555; font-size: 14px; line-height: 1.5; }
        code, pre { font-family: Consolas, monospace; font-size: 13px; }
        pre {
            background: #f4f6f7;
            padding: 12px;
            border-radius: 4px;
            overflow-x: auto;
        }
        .hash { word-break: break-all; }
    </style>
</head>
<body>
    <h1>Обновление АЗС</h1>
    <a class="nav-btn" href="/">К графикам</a>
    <a class="nav-btn" href="/journal">Журнал МП</a>

    <h2>Диск</h2>
    <div class="card{% if info.disk.low %} disk-low{% endif %}">
        <p class="disk-line">
            Свободно: <strong>{{ info.disk.free_text }}</strong>
            из {{ info.disk.total_text }}
            (занято {{ info.disk.used_text }})
        </p>
        <div class="disk-bar" title="Занято {{ info.disk.used_percent }}%">
            <div style="width: {{ info.disk.used_percent }}%"></div>
        </div>
        {% if info.disk.low %}
        <p class="warn">Мало места. Архив около 80 МБ, а при замене на время записи нужен запас.</p>
        {% endif %}
        <p class="meta">Под базу остаётся запас {{ reserve_text }}. Максимальный размер архива — {{ max_text }}.</p>
    </div>

    <h2>Текущий файл</h2>
    <div class="card">
        {% if info.available %}
        <p class="meta">
            {{ info.name }}<br>
            {{ info.size_text }}, загружен {{ info.uploaded_at }}<br>
            <span class="hash">SHA-256: {{ info.sha256 }}</span>
        </p>
        {% else %}
        <p class="meta">Файл обновления ещё не загружен.</p>
        {% endif %}
    </div>

    <h2>Загрузить</h2>
    <div class="card">
        <label for="apiKey">Ключ API</label>
        <input type="password" id="apiKey" autocomplete="current-password">
        <label for="updateFile">Zip-архив</label>
        <input type="file" id="updateFile" accept=".zip,application/zip">
        <div>
            <button type="button" id="uploadBtn">Загрузить</button>
            <button type="button" id="downloadBtn" {% if not info.available %}disabled{% endif %}>Скачать</button>
            <button type="button" id="deleteBtn" class="danger" {% if not info.available %}disabled{% endif %}>Удалить</button>
        </div>
        <div id="progress" class="progress" hidden><div id="progressBar"></div></div>
        <p id="status" class="msg"></p>
    </div>

    <h2>Как качает станция</h2>
    <div class="card">
        <p class="meta">Один и тот же адрес. Ключ — тот же, что для остальных методов, заголовок <code>X-Api-Key</code> или параметр <code>api_key</code>.</p>
        <pre>GET /api/update
GET /api/update/info</pre>
        <p class="meta">Загрузка с компьютера — тело запроса целиком, без формы:</p>
        <pre>POST /api/update
Content-Type: application/zip
X-Api-Key: ...
X-Filename: update.zip</pre>
    </div>

    <script>
        const keyInput = document.getElementById('apiKey');
        const fileInput = document.getElementById('updateFile');
        const statusEl = document.getElementById('status');
        const progress = document.getElementById('progress');
        const progressBar = document.getElementById('progressBar');
        const savedKey = sessionStorage.getItem('azsApiKey') || '';
        if (savedKey) keyInput.value = savedKey;

        function setStatus(text, kind) {
            statusEl.textContent = text || '';
            statusEl.className = 'msg' + (kind ? ' ' + kind : '');
        }

        function apiKey() {
            const key = keyInput.value.trim();
            if (key) sessionStorage.setItem('azsApiKey', key);
            return key;
        }

        function setBusy(busy) {
            document.querySelectorAll('button').forEach(function (button) {
                if (busy) {
                    button.dataset.wasDisabled = button.disabled ? '1' : '';
                    button.disabled = true;
                } else {
                    button.disabled = button.dataset.wasDisabled === '1';
                }
            });
        }

        document.getElementById('uploadBtn').addEventListener('click', function () {
            const key = apiKey();
            const file = fileInput.files && fileInput.files[0];
            if (!key) {
                setStatus('Введите ключ API.', 'error');
                return;
            }
            if (!file) {
                setStatus('Выберите zip-файл.', 'error');
                return;
            }
            const xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/update');
            xhr.setRequestHeader('X-Api-Key', key);
            xhr.setRequestHeader('Content-Type', 'application/zip');
            xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
            xhr.upload.onprogress = function (event) {
                if (!event.lengthComputable) return;
                progress.hidden = false;
                progressBar.style.width = Math.round(100 * event.loaded / event.total) + '%';
            };
            xhr.onload = function () {
                progress.hidden = true;
                let payload = {};
                try { payload = JSON.parse(xhr.responseText || '{}'); } catch (e) { payload = {}; }
                if (xhr.status >= 200 && xhr.status < 300) {
                    window.location.reload();
                    return;
                }
                setBusy(false);
                setStatus(payload.message || ('Ошибка ' + xhr.status), 'error');
            };
            xhr.onerror = function () {
                progress.hidden = true;
                setBusy(false);
                setStatus('Сеть оборвалась во время загрузки.', 'error');
            };
            setBusy(true);
            setStatus('Загрузка…');
            progress.hidden = false;
            progressBar.style.width = '0%';
            xhr.send(file);
        });

        document.getElementById('downloadBtn').addEventListener('click', function () {
            const key = apiKey();
            if (!key) {
                setStatus('Введите ключ API.', 'error');
                return;
            }
            const xhr = new XMLHttpRequest();
            xhr.open('GET', '/api/update');
            xhr.setRequestHeader('X-Api-Key', key);
            xhr.responseType = 'blob';
            xhr.onload = function () {
                setBusy(false);
                if (xhr.status < 200 || xhr.status >= 300) {
                    setStatus('Не удалось скачать (' + xhr.status + ').', 'error');
                    return;
                }
                const link = document.createElement('a');
                const url = URL.createObjectURL(xhr.response);
                link.href = url;
                link.download = 'azs_update.zip';
                document.body.appendChild(link);
                link.click();
                link.remove();
                setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
                setStatus('Файл скачан.', 'ok');
            };
            xhr.onerror = function () {
                setBusy(false);
                setStatus('Сеть оборвалась во время скачивания.', 'error');
            };
            setBusy(true);
            setStatus('Скачивание…');
            xhr.send();
        });

        document.getElementById('deleteBtn').addEventListener('click', function () {
            const key = apiKey();
            if (!key) {
                setStatus('Введите ключ API.', 'error');
                return;
            }
            if (!window.confirm('Удалить файл обновления с сайта?')) return;
            setBusy(true);
            fetch('/api/update', { method: 'DELETE', headers: { 'X-Api-Key': key } })
                .then(function (response) { return response.json().then(function (data) { return { response: response, data: data }; }); })
                .then(function (result) {
                    if (!result.response.ok) {
                        setBusy(false);
                        setStatus(result.data.message || 'Не удалось удалить.', 'error');
                        return;
                    }
                    window.location.reload();
                })
                .catch(function () {
                    setBusy(false);
                    setStatus('Сеть оборвалась.', 'error');
                });
        });
    </script>
</body>
</html>
"""


@app.route("/update")
def update_page():
    return render_template_string(
        UPDATE_HTML,
        info=update_info(),
        max_text=format_bytes(MAX_UPDATE_BYTES),
        reserve_text=format_bytes(DISK_RESERVE_BYTES),
    )


@app.errorhandler(413)
def request_too_large(_err):
    return jsonify({
        "status": "error",
        "message": "Файл больше %s" % format_bytes(MAX_UPDATE_BYTES),
    }), 413


@app.route("/api/update/info", methods=["GET"])
def update_info_api():
    if not api_key_ok():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    return jsonify({"status": "success", **update_info()})


@app.route("/api/update", methods=["GET", "HEAD", "POST", "DELETE"])
def update_api():
    if not api_key_ok():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    if request.method in ("GET", "HEAD"):
        path = update_path()
        if not os.path.isfile(path):
            return jsonify({"status": "error", "message": "Обновление ещё не загружено"}), 404
        return send_file(
            path,
            as_attachment=True,
            download_name=UPDATE_NAME,
            mimetype="application/zip",
            conditional=True,
            max_age=0,
        )

    if request.method == "DELETE":
        try:
            os.remove(update_path())
        except FileNotFoundError:
            pass
        except OSError:
            return jsonify({"status": "error", "message": "Не удалось удалить файл"}), 500
        remove_quiet(update_meta_path())
        return jsonify({"status": "success", **update_info()})

    length = request.content_length
    if not length or length <= 0:
        return jsonify({"status": "error", "message": "Неизвестен размер файла"}), 411
    if length > MAX_UPDATE_BYTES:
        return jsonify({
            "status": "error",
            "message": "Файл больше %s" % format_bytes(MAX_UPDATE_BYTES),
        }), 413
    try:
        info = save_update_stream(
            length,
            clean_upload_name(unquote(request.headers.get("X-Filename") or "")),
        )
    except UploadError as exc:
        return jsonify({"status": "error", "message": str(exc)}), exc.code
    return jsonify({"status": "success", **info})
