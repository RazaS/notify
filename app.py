from __future__ import annotations

import atexit
import json
import os
import sqlite3
import threading
from datetime import UTC, date, datetime, time, timedelta
from functools import wraps
from pathlib import Path
from secrets import compare_digest
from urllib import error as url_error
from urllib import request as url_request
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message TEXT NOT NULL,
    scheduled_for_utc TEXT NOT NULL,
    timezone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at_utc TEXT NOT NULL,
    sent_at_utc TEXT,
    archived_at_utc TEXT,
    telegram_response TEXT,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_reminders_status_scheduled
ON reminders(status, scheduled_for_utc);
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_storage(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def from_storage(value: str) -> datetime:
    return datetime.fromisoformat(value)


def format_local(utc_value: str | None, tz_name: str) -> str:
    if not utc_value:
        return ""
    local_dt = from_storage(utc_value).astimezone(ZoneInfo(tz_name))
    return local_dt.strftime("%b %d, %Y at %I:%M %p")


def build_telegram_message(reminder: sqlite3.Row) -> str:
    scheduled = format_local(reminder["scheduled_for_utc"], reminder["timezone"])
    return f"Reminder: {reminder['message']}\nWhen: {scheduled}"


def build_schedule_defaults(now_local: datetime) -> dict:
    hour_24 = now_local.hour
    minute_value = now_local.minute
    period = "AM" if hour_24 < 12 else "PM"
    hour_12 = hour_24 % 12 or 12
    return {
        "default_date": now_local.strftime("%Y-%m-%d"),
        "default_hour": f"{hour_12:02d}",
        "default_minute": f"{minute_value:02d}",
        "default_period": period,
        "hour_options": [f"{value:02d}" for value in range(1, 13)],
        "minute_options": [f"{value:02d}" for value in range(60)],
        "period_options": ["AM", "PM"],
    }


def parse_scheduled_fields(
    scheduled_date_raw: str,
    scheduled_hour_raw: str,
    scheduled_minute_raw: str,
    scheduled_period_raw: str,
    timezone_name: str,
) -> datetime:
    scheduled_date = date.fromisoformat(scheduled_date_raw)
    hour_12 = int(scheduled_hour_raw)
    minute_value = int(scheduled_minute_raw)
    period = scheduled_period_raw.upper()

    if hour_12 < 1 or hour_12 > 12:
        raise ValueError("Invalid hour.")
    if minute_value < 0 or minute_value > 59:
        raise ValueError("Invalid minute.")
    if period not in {"AM", "PM"}:
        raise ValueError("Invalid period.")

    hour_24 = hour_12 % 12
    if period == "PM":
        hour_24 += 12

    local_time = time(hour=hour_24, minute=minute_value)
    return datetime.combine(
        scheduled_date,
        local_time,
        tzinfo=ZoneInfo(timezone_name),
    )


class TelegramSender:
    def __init__(self, token: str | None, chat_id: str | None) -> None:
        self.token = token
        self.chat_id = chat_id

    def send_message(self, text: str) -> dict:
        if not self.token or not self.chat_id:
            raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before sending reminders.")

        endpoint = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": text,
            }
        ).encode("utf-8")
        http_request = url_request.Request(
            endpoint,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        try:
            with url_request.urlopen(http_request, timeout=15) as response:
                raw_body = response.read().decode("utf-8")
        except url_error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(body or f"Telegram request failed with HTTP {exc.code}.") from exc
        except url_error.URLError as exc:
            raise RuntimeError(f"Telegram request failed: {exc.reason}") from exc

        data = json.loads(raw_body)
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Telegram rejected the reminder."))
        return data


def init_db(database_path: str) -> None:
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(SCHEMA)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(_: object = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def load_reminders(status: str, limit: int | None = None) -> list[dict]:
    db = get_db()
    query = """
        SELECT id, message, scheduled_for_utc, timezone, status, created_at_utc,
               sent_at_utc, archived_at_utc, last_error
        FROM reminders
        WHERE status = ?
    """
    params: list[object] = [status]
    if status == "queued":
        query += " ORDER BY scheduled_for_utc ASC"
    else:
        query += " ORDER BY archived_at_utc DESC, sent_at_utc DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    rows = db.execute(query, params).fetchall()
    reminders = []
    for row in rows:
        reminders.append(
            {
                "id": row["id"],
                "message": row["message"],
                "status": row["status"],
                "scheduled_label": format_local(row["scheduled_for_utc"], row["timezone"]),
                "created_label": format_local(row["created_at_utc"], row["timezone"]),
                "sent_label": format_local(row["sent_at_utc"], row["timezone"]),
                "archived_label": format_local(row["archived_at_utc"], row["timezone"]),
                "last_error": row["last_error"],
            }
        )
    return reminders


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


def process_due_reminders(app: Flask) -> int:
    lock = app.extensions["notify_process_lock"]
    if not lock.acquire(blocking=False):
        return 0

    processed_count = 0
    try:
        with sqlite3.connect(app.config["DATABASE"]) as connection:
            connection.row_factory = sqlite3.Row
            due_rows = connection.execute(
                """
                SELECT id, message, scheduled_for_utc, timezone, created_at_utc
                FROM reminders
                WHERE status = 'queued' AND scheduled_for_utc <= ?
                ORDER BY scheduled_for_utc ASC
                LIMIT 25
                """,
                (to_storage(utc_now()),),
            ).fetchall()

            sender = app.extensions["notify_sender"]
            for row in due_rows:
                try:
                    response = sender.send_message(build_telegram_message(row))
                except Exception as exc:  # noqa: BLE001
                    connection.execute(
                        "UPDATE reminders SET last_error = ? WHERE id = ?",
                        (str(exc), row["id"]),
                    )
                    continue

                sent_at = to_storage(utc_now())
                connection.execute(
                    """
                    UPDATE reminders
                    SET status = 'archived',
                        sent_at_utc = ?,
                        archived_at_utc = ?,
                        telegram_response = ?,
                        last_error = NULL
                    WHERE id = ?
                    """,
                    (sent_at, sent_at, json.dumps(response), row["id"]),
                )
                processed_count += 1

            connection.commit()
    finally:
        lock.release()

    return processed_count


def start_scheduler(app: Flask) -> None:
    if app.extensions.get("notify_scheduler_started"):
        return

    stop_event = threading.Event()
    interval = max(5, int(app.config["SCHEDULER_INTERVAL_SECONDS"]))

    def run_loop() -> None:
        while not stop_event.is_set():
            process_due_reminders(app)
            stop_event.wait(interval)

    thread = threading.Thread(target=run_loop, name="notify-scheduler", daemon=True)
    thread.start()
    app.extensions["notify_scheduler_started"] = True
    app.extensions["notify_scheduler_stop"] = stop_event
    atexit.register(stop_event.set)


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    default_database = Path(app.instance_path) / "notify.db"
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "notify-dev-secret"),
        DATABASE=os.environ.get("NOTIFY_DB_PATH", str(default_database)),
        APP_TIMEZONE=os.environ.get("NOTIFY_TIMEZONE", "America/Toronto"),
        LOGIN_USERNAME=os.environ.get("NOTIFY_USERNAME", "raza"),
        LOGIN_PASSWORD=os.environ.get("NOTIFY_PASSWORD", "password"),
        TELEGRAM_BOT_TOKEN=os.environ.get("TELEGRAM_BOT_TOKEN"),
        TELEGRAM_CHAT_ID=os.environ.get("TELEGRAM_CHAT_ID"),
        SCHEDULER_INTERVAL_SECONDS=int(os.environ.get("NOTIFY_SCHEDULER_INTERVAL", "10")),
        START_SCHEDULER=True,
    )

    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    init_db(app.config["DATABASE"])

    app.extensions["notify_sender"] = TelegramSender(
        app.config["TELEGRAM_BOT_TOKEN"],
        app.config["TELEGRAM_CHAT_ID"],
    )
    app.extensions["notify_process_lock"] = threading.Lock()
    app.teardown_appcontext(close_db)

    @app.context_processor
    def inject_template_context() -> dict:
        sender = app.extensions["notify_sender"]
        sender_token = getattr(sender, "token", app.config["TELEGRAM_BOT_TOKEN"])
        sender_chat_id = getattr(sender, "chat_id", app.config["TELEGRAM_CHAT_ID"])
        return {
            "app_timezone": app.config["APP_TIMEZONE"],
            "telegram_ready": bool(sender_token and sender_chat_id),
        }

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("authenticated"):
            return redirect(url_for("index"))

        error_message = None
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            valid_username = compare_digest(username, app.config["LOGIN_USERNAME"])
            valid_password = compare_digest(password, app.config["LOGIN_PASSWORD"])
            if valid_username and valid_password:
                session.clear()
                session["authenticated"] = True
                session["username"] = username
                return redirect(url_for("index"))
            error_message = "Incorrect username or password."

        return render_template("login.html", error_message=error_message)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def index():
        queued = load_reminders("queued")
        archived = load_reminders("archived", limit=50)
        now_local = datetime.now(ZoneInfo(app.config["APP_TIMEZONE"]))
        return render_template(
            "index.html",
            queued=queued,
            archived=archived,
            **build_schedule_defaults(now_local),
        )

    @app.post("/reminders")
    @login_required
    def create_reminder():
        message = request.form.get("message", "").strip()
        scheduled_date_raw = request.form.get("scheduled_date", "").strip()
        scheduled_hour_raw = request.form.get("scheduled_hour", "").strip()
        scheduled_minute_raw = request.form.get("scheduled_minute", "").strip()
        scheduled_period_raw = request.form.get("scheduled_period", "").strip()
        timezone_name = app.config["APP_TIMEZONE"]

        if (
            not message
            or not scheduled_date_raw
            or not scheduled_hour_raw
            or not scheduled_minute_raw
            or not scheduled_period_raw
        ):
            flash("Message and time are required.", "error")
            return redirect(url_for("index"))

        try:
            local_dt = parse_scheduled_fields(
                scheduled_date_raw,
                scheduled_hour_raw,
                scheduled_minute_raw,
                scheduled_period_raw,
                timezone_name,
            )
        except ValueError:
            flash("Use a valid date and time.", "error")
            return redirect(url_for("index"))

        if len(message) > 500:
            flash("Keep reminders under 500 characters.", "error")
            return redirect(url_for("index"))

        now_local = datetime.now(ZoneInfo(timezone_name)).replace(second=0, microsecond=0)
        if local_dt < now_local:
            flash("Pick a time in the future.", "error")
            return redirect(url_for("index"))

        db = get_db()
        db.execute(
            """
            INSERT INTO reminders (message, scheduled_for_utc, timezone, status, created_at_utc)
            VALUES (?, ?, ?, 'queued', ?)
            """,
            (
                message,
                to_storage(local_dt),
                timezone_name,
                to_storage(utc_now()),
            ),
        )
        db.commit()
        flash("Reminder queued.", "success")
        return redirect(url_for("index"))

    @app.post("/reminders/<int:reminder_id>/delete")
    @login_required
    def delete_reminder(reminder_id: int):
        db = get_db()
        result = db.execute(
            "DELETE FROM reminders WHERE id = ? AND status = 'queued'",
            (reminder_id,),
        )
        db.commit()
        if result.rowcount:
            flash("Reminder deleted.", "success")
        else:
            flash("That reminder is no longer in the queue.", "error")
        return redirect(url_for("index"))

    if app.config["START_SCHEDULER"]:
        start_scheduler(app)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
