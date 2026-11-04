"""Offline regression tests: no live feeds, credentials, or email delivery."""

import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import news_digest as n

NOW = datetime(2026, 10, 30, 6, 15, tzinfo=timezone.utc)
RSS = b"""<rss version="2.0"><channel><item>
<title>AI &amp; chips - Publisher</title><link>https://example.com/story</link>
<source>Publisher</source><pubDate>Fri, 30 Oct 2026 05:15:00 GMT</pubDate>
<description>&lt;p&gt;Research &amp;amp; markets&lt;/p&gt;</description>
</item><item><title>No link</title></item></channel></rss>"""
ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<title>Atom story</title><link rel="self" href="https://example.com/feed"/>
<link rel="alternate" href="https://example.com/story"/>
<updated>2026-10-30T05:15:00Z</updated><content>Example text</content>
</entry></feed>"""


def article(url="https://example.com/story", hours_ago=1, **kwargs):
    return n.Article(
        kwargs.get("source", "Example Publisher"), kwargs.get("title", "Example headline"),
        url, NOW - timedelta(hours=hours_ago), kwargs.get("summary", "Example summary"),
    )


def email_settings(**kwargs):
    return replace(n.Settings(
        resend_api_key="fake-test-key", email_from="Digest <sender@example.com>",
        recipients=("reader@example.com",),
    ), **kwargs)


def digest(**kwargs):
    return replace(n.Digest((article(),), NOW, 1, 5), **kwargs)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_daily_defaults_and_preview_without_secrets(self):
        settings = n.Settings.from_env(require_email=False)
        self.assertEqual(settings.lookback_hours, 24)
        self.assertEqual(settings.max_articles, 30)
        self.assertEqual(settings.watchlist, ())

    def test_email_secrets_required_for_delivery(self):
        with self.assertRaisesRegex(n.DigestError, "RESEND_API_KEY"):
            n.Settings.from_env()

    def test_integer_settings_fail_for_invalid_values(self):
        for value in ["oops", "0", "-1", "169"]:
            with self.subTest(value=value), patch.dict(os.environ, LOOKBACK_HOURS=value):
                with self.assertRaises(n.DigestError):
                    n.Settings.from_env(require_email=False)

    def test_other_integer_bounds(self):
        for key in ["MAX_ARTICLES", "REQUEST_TIMEOUT", "MAX_ATTEMPTS"]:
            with self.subTest(key=key), patch.dict(os.environ, {key: "0"}):
                with self.assertRaises(n.DigestError):
                    n.Settings.from_env(require_email=False)

    def test_watchlist_normalized_deduplicated_and_encoded(self):
        os.environ["WATCHLIST"] = " nbis, NBS.L, NBIS,BRK-B, "
        settings = n.Settings.from_env(require_email=False)
        self.assertEqual(settings.watchlist, ("NBIS", "NBS.L", "BRK-B"))
        feeds = n.feed_list(settings)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(feeds["Stock: NBS.L"]).query)
        self.assertEqual(query["q"], ['"NBS.L" stock'])

    def test_empty_watchlist_keeps_ai_feed(self):
        os.environ["WATCHLIST"] = ""
        self.assertEqual(len(n.feed_list(n.Settings.from_env(require_email=False))), 1)

    def test_invalid_watchlist_rejected(self):
        os.environ["WATCHLIST"] = "NVDA OR spam"
        with self.assertRaisesRegex(n.DigestError, "WATCHLIST"):
            n.Settings.from_env(require_email=False)

    def test_recipients_deduplicated(self):
        os.environ.update(
            RESEND_API_KEY="fake-key", EMAIL_FROM="sender@example.com",
            DIGEST_RECIPIENTS="a@example.com, b@example.com, a@example.com",
        )
        self.assertEqual(n.Settings.from_env().recipients, ("a@example.com", "b@example.com"))

    def test_bad_email_inputs_rejected(self):
        for settings in [
            email_settings(email_from="invalid"),
            email_settings(recipients=("invalid",)),
            email_settings(recipients=tuple(f"{i}@example.com" for i in range(51))),
            email_settings(email_from="ok@example.com\nBcc: other@example.com"),
        ]:
            with self.subTest(settings=settings), self.assertRaises(n.DigestError):
                settings.validate_email()

    def test_credentials_hidden_from_repr(self):
        self.assertNotIn("fake-test-key", repr(email_settings()))
        self.assertNotIn("reader@example.com", repr(email_settings()))

    def test_env_bom_quotes_and_host_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                '# comment\nWATCHLIST="NBIS,NVDA"\nLOOKBACK_HOURS=8\nignored\n',
                encoding="utf-8-sig",
            )
            os.environ["LOOKBACK_HOURS"] = "24"
            n.load_env(path)
        self.assertEqual(os.environ["WATCHLIST"], "NBIS,NVDA")
        self.assertEqual(os.environ["LOOKBACK_HOURS"], "24")

    def test_env_path_independent_of_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / ".env").write_text("WATCHLIST=AMD", encoding="utf-8")
            with patch.object(n, "PROJECT_DIR", path):
                n.load_env()
        self.assertEqual(os.environ["WATCHLIST"], "AMD")


class FeedTests(unittest.TestCase):
    def test_rss_parser_cleans_titles_and_markup(self):
        articles = n.parse_feed(RSS, "fallback")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].title, "AI & chips")
        self.assertEqual(articles[0].source, "Publisher")
        self.assertEqual(articles[0].summary, "Research & markets")

    def test_atom_prefers_alternate_link_and_content(self):
        item = n.parse_feed(ATOM, "Atom source")[0]
        self.assertEqual(item.url, "https://example.com/story")
        self.assertEqual(item.summary, "Example text")
        self.assertEqual(item.source, "Atom source")

    def test_invalid_xml_and_non_feed_responses_fail(self):
        for data in [
            b"<rss>", b"<rss/>", b"<html><body>Blocked</body></html>",
            b'<?xml version="1.0" encoding="made-up"?><rss><channel/></rss>',
        ]:
            with self.subTest(data=data), self.assertRaises(n.DigestError):
                n.parse_feed(data, "test")

    def test_empty_valid_feed_succeeds(self):
        self.assertEqual(n.parse_feed(b"<rss><channel/></rss>", "test"), [])

    def test_unsafe_links_are_dropped(self):
        for link in [b"javascript:alert(1)", b"file:///secret", b"/relative"]:
            self.assertEqual(
                n.parse_feed(RSS.replace(b"https://example.com/story", link), "test"), [],
            )

    def test_dates_normalized_or_excluded(self):
        for value in [
            "Fri, 30 Oct 2026 05:15:00 GMT", "2026-10-30T05:15:00Z",
            "2026-10-30T07:15:00+02:00", "2026-10-30T05:15:00",
        ]:
            with self.subTest(value=value):
                self.assertEqual(n.parse_date(value), NOW - timedelta(hours=1))
        for value in [None, "broken", "9999-12-31T23:59:59-12:00"]:
            self.assertEqual(n.parse_date(value).year, 1)

    def test_filter_cutoff_future_sort_dedup_and_limit(self):
        recent = article(hours_ago=1)
        duplicate_older = article(hours_ago=2)
        newest = article("https://example.com/new", 0)
        boundary = article("https://example.com/boundary", 24)
        old = article("https://example.com/old", 25)
        future = article("https://example.com/future", -1)
        with patch.object(n, "feed_list", return_value={"a": "url", "b": "url"}), \
                patch.object(n, "fetch_feed", side_effect=[
                    [recent, boundary, old, future], [duplicate_older, newest],
                ]):
            result = n.collect(n.Settings(max_articles=2), now=NOW)
        self.assertEqual(result.articles, (newest, recent))
        self.assertEqual(result.total_matches, 3)
        self.assertEqual(result.successful_feeds, 2)

    def test_partial_failure_is_preserved(self):
        with patch.object(n, "feed_list", return_value={"good": "url", "bad": "url"}), \
                patch.object(n, "fetch_feed", side_effect=[[article()], n.DigestError("test")]), \
                self.assertLogs(n.LOGGER, level="WARNING"):
            result = n.collect(n.Settings(), now=NOW)
        self.assertEqual(result.failed_feeds, ("bad",))
        self.assertEqual(result.successful_feeds, 1)

    def test_total_feed_failure_is_fatal(self):
        with patch.object(n, "fetch_feed", side_effect=n.DigestError("test")), \
                self.assertLogs(n.LOGGER, level="WARNING"), \
                self.assertRaisesRegex(n.DigestError, "All news feeds failed"):
            n.collect(n.Settings(), now=NOW)

    def test_zero_matches_is_distinct_from_outage(self):
        with patch.object(n, "fetch_feed", return_value=[]):
            result = n.collect(n.Settings(watchlist=()), now=NOW)
        self.assertEqual(result.articles, ())
        self.assertEqual(result.successful_feeds, 1)
        self.assertEqual(result.failed_feeds, ())


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(n.time, "sleep")
        self.sleeper = self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.request = urllib.request.Request("https://example.com")
        self.settings = n.Settings()

    def request_data(self):
        return n.request_bytes(self.request, self.settings, max_bytes=100)

    def test_rate_limit_retry_after(self):
        failure = urllib.error.HTTPError(
            "https://example.com", 429, "", {"Retry-After": "2"}, io.BytesIO(),
        )
        with patch.object(n.urllib.request, "urlopen", side_effect=[
            failure, io.BytesIO(b"ok"),
        ]) as fetch, self.assertLogs(n.LOGGER, level="WARNING"):
            self.assertEqual(self.request_data(), b"ok")
        self.assertEqual(fetch.call_count, 2)
        self.sleeper.assert_called_once_with(2.0)

    def test_timeout_and_server_errors_retried(self):
        failure = urllib.error.HTTPError("https://example.com", 503, "", {}, io.BytesIO())
        with patch.object(n.urllib.request, "urlopen", side_effect=[
            TimeoutError(), failure, io.BytesIO(b"ok"),
        ]), self.assertLogs(n.LOGGER, level="WARNING"):
            self.assertEqual(self.request_data(), b"ok")
        self.assertEqual([call.args[0] for call in self.sleeper.call_args_list], [1.0, 2.0])

    def test_retries_stop_after_configured_attempts(self):
        with patch.object(n.urllib.request, "urlopen", side_effect=TimeoutError()) as fetch, \
                self.assertLogs(n.LOGGER, level="WARNING"), \
                self.assertRaisesRegex(n.DigestError, "3 attempt"):
            self.request_data()
        self.assertEqual(fetch.call_count, 3)

    def test_permanent_failure_not_retried(self):
        failure = urllib.error.HTTPError("https://example.com", 403, "", {}, io.BytesIO())
        with patch.object(n.urllib.request, "urlopen", side_effect=failure) as fetch, \
                self.assertRaisesRegex(n.DigestError, "HTTP 403"):
            self.request_data()
        self.assertEqual(fetch.call_count, 1)
        self.sleeper.assert_not_called()

    def test_resend_permission_error_has_actionable_hint(self):
        failure = urllib.error.HTTPError("https://api.resend.com/emails", 403, "", {}, io.BytesIO())
        self.request = urllib.request.Request("https://api.resend.com/emails")
        with patch.object(n.urllib.request, "urlopen", side_effect=failure), \
                self.assertRaisesRegex(n.DigestError, "verified sender"):
            self.request_data()

    def test_oversized_response_rejected(self):
        with patch.object(n.urllib.request, "urlopen", return_value=io.BytesIO(b"x" * 101)), \
                self.assertRaisesRegex(n.DigestError, "size"):
            self.request_data()

    def test_incomplete_response_retried_instead_of_parsed(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.side_effect = n.http.client.IncompleteRead(b"<rss>")
        with patch.object(n.urllib.request, "urlopen", side_effect=[
            response, io.BytesIO(b"ok"),
        ]), self.assertLogs(n.LOGGER, level="WARNING"):
            self.assertEqual(self.request_data(), b"ok")

    def test_retry_after_bounded_and_malformed_falls_back(self):
        self.assertEqual(n.retry_delay("9999", 1), 60.0)
        self.assertEqual(n.retry_delay("-2", 1), 0.0)
        self.assertEqual(n.retry_delay("invalid", 2), 2.0)
        self.assertEqual(n.retry_delay("Fri, 30 Oct 2020 00:00:00 GMT", 1), 0.0)


class RenderingAndDeliveryTests(unittest.TestCase):
    def test_html_escapes_external_content_and_links(self):
        item = article(
            'https://example.com/?q=" onmouseover="oops',
            title="<script>alert(1)</script>", source="<Publisher>", summary="<img src=x>",
        )
        body = n.render_html(digest(articles=(item,)), n.Settings())
        self.assertNotIn("<script>", body)
        self.assertNotIn("<img src=x>", body)
        self.assertIn("&lt;Publisher&gt;", body)
        self.assertIn("&quot;", body)
        self.assertIn('lang="en"', body)

    def test_render_does_not_make_unsafe_url_clickable(self):
        body = n.render_html(digest(articles=(article("javascript:alert(1)"),)), n.Settings())
        self.assertNotIn("javascript:", body)

    def test_window_limit_and_partial_coverage_in_both_formats(self):
        result = digest(total_matches=10, failed_feeds=("Stock: NBIS",))
        for body in [
            n.render_html(result, n.Settings(lookback_hours=12)),
            n.render_text(result, n.Settings(lookback_hours=12)),
        ]:
            self.assertIn("12 hours", body)
            self.assertIn("Partial coverage", body)
            self.assertIn("Stock: NBIS", body)
            self.assertIn("10 matching stories", body)

    def test_empty_digest_uses_configured_window(self):
        result = digest(articles=(), total_matches=0)
        self.assertIn("last 8 hours", n.render_html(result, n.Settings(lookback_hours=8)))
        self.assertIn("No new articles", n.render_text(result, n.Settings()))

    def test_email_payload_and_transport_retry_use_same_idempotency_key(self):
        failure = urllib.error.HTTPError("https://api.resend.com/emails", 503, "", {}, io.BytesIO())
        with patch.object(n.urllib.request, "urlopen", side_effect=[
            failure, io.BytesIO(b'{"id":"mock-email-id"}'),
        ]) as send, patch.object(n.time, "sleep"), self.assertLogs(n.LOGGER, level="WARNING"):
            email_id = n.send_email(digest(failed_feeds=("AI news",)), email_settings())
        self.assertEqual(email_id, "mock-email-id")
        first, second = [call.args[0] for call in send.call_args_list]
        self.assertIs(first, second)
        self.assertTrue(first.get_header("Idempotency-key").startswith("digest-"))
        payload = json.loads(first.data)
        self.assertEqual(payload["to"], ["reader@example.com"])
        self.assertIn("partial coverage", payload["subject"])
        self.assertIn("text", payload)
        self.assertIn("html", payload)

    def test_invalid_resend_response_fails(self):
        for data in [b"not-json", b"{}", b"[]", b'{"id":null}']:
            with self.subTest(data=data), \
                    patch.object(n, "request_bytes", return_value=data), \
                    self.assertRaises(n.DigestError):
                n.send_email(digest(), email_settings())

    def test_cli_dry_run_does_not_send(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(n, "load_env"), \
                patch.object(n, "collect", return_value=digest()), \
                patch.object(n, "send_email") as send, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(n.main(["--dry-run"]), 0)
        self.assertIn("<!doctype html>", output.getvalue())
        send.assert_not_called()

    def test_cli_output_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview" / "email.html"
            with patch.dict(os.environ, {}, clear=True), patch.object(n, "load_env"), \
                    patch.object(n, "collect", return_value=digest()):
                self.assertEqual(n.main(["--dry-run", "--output", str(path)]), 0)
            self.assertIn("AI News Digest", path.read_text(encoding="utf-8"))

    def test_cli_output_requires_dry_run(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            n.main(["--output", "preview.html"])
        self.assertEqual(error.exception.code, 2)

    def test_cli_total_outage_never_sends(self):
        with patch.object(n, "load_env"), patch.object(n.Settings, "from_env", return_value=email_settings()), \
                patch.object(n, "fetch_feed", side_effect=n.DigestError("test")), \
                patch.object(n, "send_email") as send, self.assertLogs(n.LOGGER, level="WARNING"):
            self.assertEqual(n.main([]), 1)
        send.assert_not_called()

    def test_cli_validates_before_fetching(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(n, "load_env"), \
                patch.object(n, "collect") as collect, self.assertLogs(n.LOGGER, level="ERROR"):
            self.assertEqual(n.main([]), 1)
        collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
