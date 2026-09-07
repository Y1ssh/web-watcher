"""Notification channels, including a real webhook POST to a local server.

The email channel is tested with an injected SMTP class rather than a live mail
server: what matters is that the right message, credentials, and TLS steps are
used, and that a failure becomes a NotifyError instead of ending the run.
"""

import io
import json
import os
import smtplib
import tempfile
import unittest
from pathlib import Path

from tests.support import LocalSite
from watcher import notify
from watcher.errors import ConfigError
from watcher.notify import (
    Channel,
    ConsoleChannel,
    Delivery,
    EmailChannel,
    FileChannel,
    Notification,
    NotifyError,
    WebhookChannel,
    build_channel,
    build_notification,
    deliver,
    render,
)

NOTE = Notification(subject="Something changed", body="from A to B")


class EnvTestCase(unittest.TestCase):
    """Restores the environment so tests cannot leak variables into each other."""

    def setUp(self):
        self._saved = dict(os.environ)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._saved)


class RenderTests(unittest.TestCase):
    def test_fills_placeholders(self):
        self.assertEqual(
            render("{value} at {url}", {"value": "10", "url": "u"}), "10 at u"
        )

    def test_an_unknown_placeholder_is_left_visible_rather_than_raising(self):
        # Raising here would lose the alert entirely over a typo in a template.
        self.assertEqual(render("{nope}", {"value": "10"}), "{nope}")

    def test_default_templates_mention_the_important_fields(self):
        note = build_notification({
            "url": "https://example.com",
            "value": "In Stock",
            "previous_value": "Sold Out",
            "outcome": "changed",
            "condition": "the value changed",
            "condition_reason": "it changed",
            "at": "2000-01-01T00:00:00Z",
        })
        self.assertIn("In Stock", note.body)
        self.assertIn("Sold Out", note.body)
        self.assertIn("https://example.com", note.body)

    def test_a_test_notification_is_labelled(self):
        note = Notification("Subject", "Body", is_test=True)
        self.assertTrue(note.display_subject.startswith("[TEST]"))
        self.assertEqual(Notification("Subject", "Body").display_subject, "Subject")


class ConsoleChannelTests(unittest.TestCase):
    def test_writes_to_the_given_stream(self):
        stream = io.StringIO()
        ConsoleChannel(stream).send(NOTE)
        output = stream.getvalue()
        self.assertIn("Something changed", output)
        self.assertIn("from A to B", output)


class FileChannelTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "logs" / "notifications.log"

    def test_appends_the_message(self):
        FileChannel(self.path).send(NOTE)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("Something changed", text)
        self.assertIn("from A to B", text)

    def test_creates_missing_directories(self):
        FileChannel(self.path).send(NOTE)
        self.assertTrue(self.path.exists())

    def test_keeps_earlier_alerts(self):
        channel = FileChannel(self.path)
        channel.send(Notification("first", "a"))
        channel.send(Notification("second", "b"))
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("first", text)
        self.assertIn("second", text)

    def test_an_unwritable_path_becomes_a_notify_error(self):
        # The path exists as a file, so treating it as a directory must fail.
        blocker = Path(self._temp.name) / "blocker"
        blocker.write_text("x", encoding="utf-8")
        with self.assertRaises(NotifyError):
            FileChannel(blocker / "inner.log").send(NOTE)


class WebhookChannelTests(EnvTestCase):
    def test_posts_json_to_the_url_from_the_environment(self):
        with LocalSite() as site:
            os.environ["TEST_HOOK"] = site.url + "/hook"
            WebhookChannel("TEST_HOOK").send(NOTE)
            self.assertEqual(len(site.received), 1)
            payload = json.loads(site.received[0]["body"])
        self.assertIn("Something changed", payload["text"])
        self.assertIn("from A to B", payload["text"])

    def test_the_payload_key_is_configurable_for_other_services(self):
        with LocalSite() as site:
            os.environ["TEST_HOOK"] = site.url + "/hook"
            WebhookChannel("TEST_HOOK", text_key="content").send(NOTE)
            payload = json.loads(site.received[0]["body"])
        self.assertIn("Something changed", payload["content"])

    def test_a_missing_environment_variable_is_reported_clearly(self):
        os.environ.pop("TEST_HOOK", None)
        with self.assertRaises(ConfigError) as caught:
            WebhookChannel("TEST_HOOK").send(NOTE)
        self.assertIn("TEST_HOOK", str(caught.exception))

    def test_an_error_status_becomes_a_notify_error(self):
        with LocalSite() as site:
            site.write_status = 500
            os.environ["TEST_HOOK"] = site.url + "/hook"
            with self.assertRaises(NotifyError):
                WebhookChannel("TEST_HOOK").send(NOTE)

    def test_an_unreachable_webhook_becomes_a_notify_error(self):
        os.environ["TEST_HOOK"] = "http://127.0.0.1:1/hook"
        with self.assertRaises(NotifyError):
            WebhookChannel("TEST_HOOK", timeout=2).send(NOTE)

    def test_a_non_http_webhook_url_is_refused(self):
        os.environ["TEST_HOOK"] = "file:///etc/passwd"
        with self.assertRaises(NotifyError):
            WebhookChannel("TEST_HOOK").send(NOTE)


class FakeSMTP:
    """Stands in for smtplib.SMTP, recording what the channel asked it to do."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args = None
        self.messages = []
        self.closed = False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.messages.append(message)


class EmailChannelTests(EnvTestCase):
    def setUp(self):
        super().setUp()
        FakeSMTP.instances = []

    def _channel(self, **overrides):
        settings = {
            "to": "me@example.com",
            "sender": "watcher@example.com",
            "host": "smtp.example.com",
            "port": 587,
            "smtp_factory": FakeSMTP,
        }
        settings.update(overrides)
        return EmailChannel(**settings)

    def test_sends_a_message_with_the_right_headers(self):
        self._channel().send(NOTE)
        message = FakeSMTP.instances[0].messages[0]
        self.assertEqual(message["Subject"], "Something changed")
        self.assertEqual(message["To"], "me@example.com")
        self.assertEqual(message["From"], "watcher@example.com")
        self.assertIn("from A to B", message.get_content())

    def test_starts_tls_by_default(self):
        self._channel().send(NOTE)
        self.assertTrue(FakeSMTP.instances[0].started_tls)

    def test_tls_can_be_turned_off(self):
        self._channel(use_tls=False).send(NOTE)
        self.assertFalse(FakeSMTP.instances[0].started_tls)

    def test_credentials_come_from_the_environment(self):
        os.environ["TEST_SMTP_USER"] = "user@example.com"
        os.environ["TEST_SMTP_PASSWORD"] = "placeholder-not-a-secret"
        self._channel(
            username_env="TEST_SMTP_USER", password_env="TEST_SMTP_PASSWORD"
        ).send(NOTE)
        self.assertEqual(
            FakeSMTP.instances[0].login_args, ("user@example.com", "placeholder-not-a-secret")
        )

    def test_no_login_when_no_credentials_are_configured(self):
        self._channel().send(NOTE)
        self.assertIsNone(FakeSMTP.instances[0].login_args)

    def test_a_missing_password_variable_is_reported_clearly(self):
        os.environ["TEST_SMTP_USER"] = "user@example.com"
        os.environ.pop("TEST_SMTP_PASSWORD", None)
        with self.assertRaises(ConfigError) as caught:
            self._channel(
                username_env="TEST_SMTP_USER", password_env="TEST_SMTP_PASSWORD"
            ).send(NOTE)
        self.assertIn("TEST_SMTP_PASSWORD", str(caught.exception))

    def test_an_smtp_failure_becomes_a_notify_error(self):
        class BrokenSMTP(FakeSMTP):
            def send_message(self, message):
                raise smtplib.SMTPException("mailbox full")

        with self.assertRaises(NotifyError):
            self._channel(smtp_factory=BrokenSMTP).send(NOTE)

    def test_an_unreachable_server_becomes_a_notify_error(self):
        class UnreachableSMTP(FakeSMTP):
            def __init__(self, host, port, timeout=None):
                raise OSError("connection refused")

        with self.assertRaises(NotifyError):
            self._channel(smtp_factory=UnreachableSMTP).send(NOTE)


class DeliverTests(unittest.TestCase):
    class Working(Channel):
        kind = "working"

        def __init__(self):
            self.sent = []

        def send(self, note):
            self.sent.append(note)

    class Broken(Channel):
        kind = "broken"

        def send(self, note):
            raise NotifyError("nope")

    class Exploding(Channel):
        kind = "exploding"

        def send(self, note):
            raise ValueError("something unexpected")

    def test_reports_each_channel(self):
        results = deliver([self.Working(), self.Broken()], NOTE)
        self.assertEqual([r.sent for r in results], [True, False])

    def test_one_broken_channel_does_not_stop_the_others(self):
        working = self.Working()
        deliver([self.Broken(), working], NOTE)
        self.assertEqual(len(working.sent), 1)

    def test_an_unexpected_error_is_contained_too(self):
        # A channel raising something other than NotifyError must still not end
        # an unattended watch.
        working = self.Working()
        results = deliver([self.Exploding(), working], NOTE)
        self.assertFalse(results[0].sent)
        self.assertIn("ValueError", results[0].detail)
        self.assertEqual(len(working.sent), 1)

    def test_no_channels_means_no_deliveries(self):
        self.assertEqual(deliver([], NOTE), [])

    def test_summary_reads_clearly(self):
        self.assertIn("sent", Delivery("console", sent=True).summary)
        self.assertIn("FAILED", Delivery("console", sent=False, detail="x").summary)


class BuildChannelTests(unittest.TestCase):
    BASE = Path("/base")

    def test_builds_each_kind(self):
        self.assertIsInstance(
            build_channel({"kind": "console"}, base_dir=self.BASE), ConsoleChannel
        )
        self.assertIsInstance(
            build_channel({"kind": "file"}, base_dir=self.BASE), FileChannel
        )
        self.assertIsInstance(
            build_channel({"kind": "webhook", "url_env": "X"}, base_dir=self.BASE),
            WebhookChannel,
        )

    def test_file_path_resolves_against_the_config_directory(self):
        channel = build_channel(
            {"kind": "file", "path": "logs/alerts.log"}, base_dir=self.BASE
        )
        self.assertEqual(channel.path, self.BASE / "logs" / "alerts.log")

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_channel({"kind": "carrier-pigeon"}, base_dir=self.BASE)

    def test_misspelled_key_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_channel(
                {"kind": "webhook", "url_env": "X", "text_ky": "content"},
                base_dir=self.BASE,
            )

    def test_a_webhook_url_written_into_the_config_is_refused(self):
        # A Slack webhook URL is a credential. Accepting it inline would put it
        # straight into a file people commit.
        with self.assertRaises(ConfigError) as caught:
            build_channel(
                {"kind": "webhook", "url": "https://hooks.slack.com/x"},
                base_dir=self.BASE,
            )
        self.assertIn("environment variable", str(caught.exception))

    def test_an_inline_password_is_refused(self):
        with self.assertRaises(ConfigError):
            build_channel(
                {
                    "kind": "email",
                    "to": "a@b.c",
                    "from": "d@e.f",
                    "host": "smtp.example.com",
                    "password": "placeholder-not-a-secret",
                },
                base_dir=self.BASE,
            )

    def test_webhook_requires_url_env(self):
        with self.assertRaises(ConfigError):
            build_channel({"kind": "webhook"}, base_dir=self.BASE)

    def test_email_requires_both_credential_variables_or_neither(self):
        base = {
            "kind": "email",
            "to": "a@b.c",
            "from": "d@e.f",
            "host": "smtp.example.com",
        }
        with self.assertRaises(ConfigError):
            build_channel({**base, "username_env": "U"}, base_dir=self.BASE)
        # Neither is fine: an open relay or a local mail server needs no login.
        build_channel(base, base_dir=self.BASE)

    def test_email_rejects_an_impossible_port(self):
        with self.assertRaises(ConfigError):
            build_channel(
                {
                    "kind": "email",
                    "to": "a@b.c",
                    "from": "d@e.f",
                    "host": "smtp.example.com",
                    "port": 99999,
                },
                base_dir=self.BASE,
            )

    def test_channel_descriptions_do_not_leak_the_secret(self):
        channel = build_channel(
            {"kind": "webhook", "url_env": "TEST_HOOK"}, base_dir=self.BASE
        )
        self.assertIn("TEST_HOOK", channel.describe())
        self.assertNotIn("http", channel.describe())


class ModuleSurfaceTests(unittest.TestCase):
    def test_channel_kinds_are_all_buildable(self):
        self.assertEqual(set(notify.CHANNEL_KINDS), set(notify._ALLOWED_KEYS))


if __name__ == "__main__":
    unittest.main()
