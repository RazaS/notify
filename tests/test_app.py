from __future__ import annotations

import os
import tempfile
import unittest

from app import create_app, get_db, process_due_reminders


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(self, text: str) -> dict:
        self.messages.append(text)
        return {"ok": True, "result": {"message_id": 1}}


class FailingSender:
    def send_message(self, text: str) -> dict:
        raise RuntimeError("boom")


class NotifyAppTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = os.path.join(self.temp_dir.name, "notify-test.db")
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": database_path,
                "START_SCHEDULER": False,
                "SECRET_KEY": "test-secret",
                "LOGIN_USERNAME": "raza",
                "LOGIN_PASSWORD": "password",
            }
        )
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def login(self) -> None:
        self.client.post(
            "/login",
            data={"username": "raza", "password": "password"},
            follow_redirects=True,
        )

    def test_login_required_redirects_root(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_invalid_login_stays_on_login_page(self) -> None:
        response = self.client.post(
            "/login",
            data={"username": "raza", "password": "wrong"},
            follow_redirects=True,
        )
        self.assertIn(b"Incorrect username or password.", response.data)

    def test_create_and_delete_reminder(self) -> None:
        self.login()
        response = self.client.post(
            "/reminders",
            data={
                "message": "Pay the phone bill",
                "scheduled_date": "2030-05-02",
                "scheduled_time": "18:45",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Reminder queued.", response.data)
        self.assertIn(b"Pay the phone bill", response.data)

        with self.app.app_context():
            conn = get_db()
            row = conn.execute("SELECT id FROM reminders").fetchone()
            reminder_id = row["id"]

        delete_response = self.client.post(
            f"/reminders/{reminder_id}/delete",
            follow_redirects=True,
        )
        self.assertIn(b"Reminder deleted.", delete_response.data)
        self.assertNotIn(b"Pay the phone bill", delete_response.data)

    def test_due_reminder_gets_archived_after_send(self) -> None:
        fake_sender = FakeSender()
        self.app.extensions["notify_sender"] = fake_sender
        self.login()

        with self.app.app_context():
            conn = get_db()
            conn.execute(
                """
                INSERT INTO reminders (message, scheduled_for_utc, timezone, status, created_at_utc)
                VALUES (?, ?, ?, 'queued', ?)
                """,
                (
                    "Book the trip",
                    "2000-01-01T15:00:00+00:00",
                    "America/Toronto",
                    "2000-01-01T14:00:00+00:00",
                ),
            )
            conn.commit()

        processed = process_due_reminders(self.app)
        self.assertEqual(processed, 1)
        self.assertEqual(len(fake_sender.messages), 1)
        self.assertIn("Book the trip", fake_sender.messages[0])

        with self.app.app_context():
            conn = get_db()
            row = conn.execute("SELECT status, sent_at_utc FROM reminders").fetchone()
            self.assertEqual(row["status"], "archived")
            self.assertIsNotNone(row["sent_at_utc"])

    def test_failed_send_stays_queued(self) -> None:
        self.app.extensions["notify_sender"] = FailingSender()
        self.login()

        with self.app.app_context():
            conn = get_db()
            conn.execute(
                """
                INSERT INTO reminders (message, scheduled_for_utc, timezone, status, created_at_utc)
                VALUES (?, ?, ?, 'queued', ?)
                """,
                (
                    "Renew passport",
                    "2000-01-01T15:00:00+00:00",
                    "America/Toronto",
                    "2000-01-01T14:00:00+00:00",
                ),
            )
            conn.commit()

        processed = process_due_reminders(self.app)
        self.assertEqual(processed, 0)

        with self.app.app_context():
            conn = get_db()
            row = conn.execute("SELECT status, last_error FROM reminders").fetchone()
            self.assertEqual(row["status"], "queued")
            self.assertEqual(row["last_error"], "boom")

    def test_dashboard_shows_date_and_single_time_field(self) -> None:
        self.login()
        response = self.client.get("/")
        self.assertIn(b'name="scheduled_date"', response.data)
        self.assertIn(b'name="scheduled_time"', response.data)
        self.assertIn(b'pattern="([01][0-9]|2[0-3]):[0-5][0-9]"', response.data)

    def test_archive_defaults_to_three_and_expands_by_ten(self) -> None:
        self.login()

        with self.app.app_context():
            conn = get_db()
            for index in range(15):
                conn.execute(
                    """
                    INSERT INTO reminders (
                        message,
                        scheduled_for_utc,
                        timezone,
                        status,
                        created_at_utc,
                        sent_at_utc,
                        archived_at_utc
                    )
                    VALUES (?, ?, ?, 'archived', ?, ?, ?)
                    """,
                    (
                        f"Archive {index:02d}",
                        f"2000-01-{index + 1:02d}T15:00:00+00:00",
                        "America/Toronto",
                        f"2000-01-{index + 1:02d}T14:00:00+00:00",
                        f"2000-01-{index + 1:02d}T16:00:00+00:00",
                        f"2000-01-{index + 1:02d}T16:00:00+00:00",
                    ),
                )
            conn.commit()

        response = self.client.get("/")
        self.assertIn(b"Archive 14", response.data)
        self.assertIn(b"Archive 13", response.data)
        self.assertIn(b"Archive 12", response.data)
        self.assertNotIn(b"Archive 11", response.data)
        self.assertIn(b"Show 10 more", response.data)

        expanded_response = self.client.get("/?archive_limit=13")
        self.assertIn(b"Archive 11", expanded_response.data)
        self.assertIn(b"Archive 02", expanded_response.data)
        self.assertNotIn(b"Archive 01", expanded_response.data)
        self.assertIn(b"Show 2 more", expanded_response.data)


if __name__ == "__main__":
    unittest.main()
