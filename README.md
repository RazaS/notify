# Notify

A private reminder queue with a login screen, light/dark mode, and Telegram delivery.

## Default login

- Username: `raza`
- Password: `password`

## Telegram setup

Set these environment variables before running the app:

```bash
export TELEGRAM_BOT_TOKEN="your-bot-token"
export TELEGRAM_CHAT_ID="your-chat-id"
```

Optional settings:

```bash
export NOTIFY_TIMEZONE="America/Toronto"
export NOTIFY_USERNAME="raza"
export NOTIFY_PASSWORD="password"
export NOTIFY_SCHEDULER_INTERVAL="10"
```

## Run

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000).

## Behavior

- Add a reminder message and a date/time from the dashboard.
- Queued reminders appear in the scrollable queue and can be deleted before send.
- The background scheduler checks due reminders every 10 seconds.
- When Telegram accepts a due reminder, it is moved into the archive list automatically.

## Test

```bash
python3 -m unittest discover -s tests
```

## Deploy

The repo includes a dedicated deployment identity for `notify.bloodapps.com`:

- App path: `/opt/notify-app`
- Gunicorn bind: `127.0.0.1:8327`
- systemd service: `notify-app.service`
- update service: `notify-app-update.service`
- update timer: `notify-app-update.timer`
- nginx config: `deploy/nginx-notify.bloodapps.com.conf`
- SQLite path: `/opt/notify-app/data/notify.db`

Expected server env file: `/etc/notify-app.env`

```bash
SECRET_KEY=replace-this
NOTIFY_DB_PATH=/opt/notify-app/data/notify.db
NOTIFY_TIMEZONE=America/Toronto
NOTIFY_USERNAME=raza
NOTIFY_PASSWORD=password
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=your-chat-id
NOTIFY_DEPLOY_BRANCH=main
```
