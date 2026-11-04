"""Collect AI and stock headlines from RSS and email a daily digest."""

from __future__ import annotations

import argparse
import hashlib
import html
import http.client
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

LOGGER = logging.getLogger(__name__)
PROJECT_DIR = Path(__file__).resolve().parent
USER_AGENT = "AI-News-Digest/1.0"
MAX_FEED_BYTES = 5 * 1024 * 1024
RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}


class DigestError(RuntimeError):
    """An actionable configuration, feed, or delivery error."""


def load_env(path: str | Path | None = None) -> None:
    """Load a simple KEY=value file without overriding host environment values."""
    path = Path(path) if path is not None else PROJECT_DIR / ".env"
    if not path.exists():
        return
    with path.open(encoding="utf-8-sig") as settings:
        for line in settings:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                os.environ.setdefault(key, value)


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as error:
        raise DigestError(f"{name} must be an integer.") from error
    if not minimum <= value <= maximum:
        raise DigestError(f"{name} must be between {minimum} and {maximum}.")
    return value


def valid_email(value: str) -> bool:
    # Basic input validation; Resend performs final address/domain validation.
    if "\r" in value or "\n" in value:
        return False
    address = parseaddr(value)[1]
    return bool(re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", address))


@dataclass(frozen=True)
class Settings:
    watchlist: tuple[str, ...] = ()
    lookback_hours: int = 24
    max_articles: int = 30
    request_timeout: int = 20
    max_attempts: int = 3
    resend_api_key: str = field(default="", repr=False)
    email_from: str = field(default="", repr=False)
    recipients: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_env(cls, *, require_email: bool = True) -> Settings:
        tickers = tuple(dict.fromkeys(
            ticker.strip().upper()
            for ticker in os.environ.get("WATCHLIST", "").split(",")
            if ticker.strip()
        ))
        if len(tickers) > 20 or any(
            not re.fullmatch(r"[A-Z0-9.^=\-]{1,20}", ticker) for ticker in tickers
        ):
            raise DigestError("WATCHLIST must contain at most 20 valid ticker symbols.")
        settings = cls(
            watchlist=tickers,
            lookback_hours=env_int("LOOKBACK_HOURS", 24, 1, 168),
            max_articles=env_int("MAX_ARTICLES", 30, 1, 100),
            request_timeout=env_int("REQUEST_TIMEOUT", 20, 1, 60),
            max_attempts=env_int("MAX_ATTEMPTS", 3, 1, 5),
            resend_api_key=os.environ.get("RESEND_API_KEY", "").strip(),
            email_from=os.environ.get("EMAIL_FROM", "").strip(),
            recipients=tuple(dict.fromkeys(
                value.strip()
                for value in os.environ.get("DIGEST_RECIPIENTS", "").split(",")
                if value.strip()
            )),
        )
        if require_email:
            settings.validate_email()
        return settings

    def validate_email(self) -> None:
        if not self.resend_api_key or not self.email_from or not self.recipients:
            raise DigestError(
                "Set RESEND_API_KEY, EMAIL_FROM, and DIGEST_RECIPIENTS "
                "in the environment or .env."
            )
        if not valid_email(self.email_from):
            raise DigestError("EMAIL_FROM must be a valid email address.")
        if len(self.recipients) > 50 or any(not valid_email(x) for x in self.recipients):
            raise DigestError("DIGEST_RECIPIENTS must contain 1–50 valid email addresses.")


@dataclass(frozen=True)
class Article:
    source: str
    title: str
    url: str
    published: datetime
    summary: str


@dataclass(frozen=True)
class Digest:
    articles: tuple[Article, ...]
    generated_at: datetime
    total_matches: int
    successful_feeds: int
    failed_feeds: tuple[str, ...] = ()


def feed_list(settings: Settings) -> dict[str, str]:
    queries = {"AI news": "artificial intelligence OR generative AI"}
    queries.update({f"Stock: {ticker}": f'"{ticker}" stock' for ticker in settings.watchlist})
    return {
        source: "https://news.google.com/rss/search?" + urllib.parse.urlencode({
            "q": query, "hl": "en-US", "gl": "US", "ceid": "US:en",
        })
        for source, query in queries.items()
    }


def retry_delay(value: str | None, attempt: int) -> float:
    """Respect Retry-After up to 60 seconds; otherwise use bounded backoff."""
    if value:
        try:
            return min(60.0, max(0.0, float(value)))
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                return min(60.0, max(0.0, (
                    date - datetime.now(timezone.utc)
                ).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return float(2 ** (attempt - 1))


def request_bytes(
    request: urllib.request.Request, settings: Settings, *, max_bytes: int,
) -> bytes:
    """Retry transient transport failures; never log credentials or API bodies."""
    for attempt in range(1, settings.max_attempts + 1):
        retry_after = None
        try:
            with urllib.request.urlopen(request, timeout=settings.request_timeout) as response:
                data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise DigestError("Response exceeded the allowed size.")
            return data
        except urllib.error.HTTPError as error:
            status = error.code
            retry_after = error.headers.get("Retry-After") if error.headers else None
            error.close()
            if status not in RETRYABLE_STATUSES or attempt == settings.max_attempts:
                is_resend = urllib.parse.urlsplit(request.full_url).hostname == "api.resend.com"
                hint = (
                    " Check your Resend API key and verified sender."
                    if is_resend and status in {401, 403} else ""
                )
                raise DigestError(f"HTTP {status}.{hint}") from error
            reason = f"HTTP {status}"
        except (OSError, http.client.HTTPException) as error:
            if attempt == settings.max_attempts:
                raise DigestError(
                    f"Network request failed after {attempt} attempt(s) "
                    f"({type(error).__name__})."
                ) from error
            reason = type(error).__name__
        delay = retry_delay(retry_after, attempt)
        LOGGER.warning("Request failed (%s); retrying in %.1fs.", reason, delay)
        time.sleep(delay)
    raise AssertionError("Unreachable retry state")


def parse_date(value: str | None) -> datetime:
    invalid = datetime.min.replace(tzinfo=timezone.utc)
    if not value:
        return invalid
    try:
        try:
            date = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return date.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return invalid


def text_of(element: ET.Element | None) -> str:
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def clean_text(value: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(value)).split())


def valid_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def parse_feed(data: bytes, source: str) -> list[Article]:
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, LookupError, ValueError) as error:
        raise DigestError("Feed returned invalid or incomplete XML.") from error
    atom = {"atom": "http://www.w3.org/2005/Atom"}
    is_atom = root.tag == "{http://www.w3.org/2005/Atom}feed"
    if root.tag != "rss" and not is_atom:
        raise DigestError("Response is not a supported RSS 2.0 or Atom feed.")
    if root.tag == "rss" and root.find("channel") is None:
        raise DigestError("RSS feed is missing its channel.")
    articles = []
    items = root.findall("atom:entry", atom) if is_atom else root.findall("./channel/item")
    for item in items:
        if is_atom:
            title = clean_text(text_of(item.find("atom:title", atom)))
            links = item.findall("atom:link", atom)
            url = next((
                link.get("href", "") for link in links
                if link.get("rel", "alternate") == "alternate"
                and valid_url(link.get("href", ""))
            ), "")
            publisher = source
            published = parse_date(
                text_of(item.find("atom:published", atom))
                or text_of(item.find("atom:updated", atom))
            )
            summary = clean_text(
                text_of(item.find("atom:summary", atom))
                or text_of(item.find("atom:content", atom))
            )
        else:
            title = clean_text(text_of(item.find("title")))
            url = text_of(item.find("link"))
            publisher = clean_text(text_of(item.find("source"))) or source
            published = parse_date(text_of(item.find("pubDate")))
            summary = clean_text(text_of(item.find("description")))
            suffix = f" - {publisher}"
            if title.lower().endswith(suffix.lower()):
                title = title[:-len(suffix)].rstrip()
        if title and valid_url(url):
            articles.append(Article(publisher[:120], title[:300], url, published, summary[:400]))
    return articles


def fetch_feed(source: str, feed_url: str, settings: Settings) -> list[Article]:
    request = urllib.request.Request(feed_url, headers={"User-Agent": USER_AGENT})
    return parse_feed(request_bytes(request, settings, max_bytes=MAX_FEED_BYTES), source)


def collect(settings: Settings, *, now: datetime | None = None) -> Digest:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=settings.lookback_hours)
    unique: dict[str, Article] = {}
    failures = []
    successes = 0
    for source, url in feed_list(settings).items():
        try:
            articles = fetch_feed(source, url, settings)
        except DigestError as error:
            failures.append(source)
            LOGGER.warning("Could not read %s: %s", source, error)
            continue
        successes += 1
        for article in articles:
            if cutoff <= article.published <= now:
                existing = unique.get(article.url)
                if existing is None or article.published > existing.published:
                    unique[article.url] = article
    if not successes:
        raise DigestError("All news feeds failed. No email was sent.")
    articles = sorted(unique.values(), key=lambda article: (article.published, article.url), reverse=True)
    LOGGER.info(
        "Collected %d unique stories; %d feed(s) succeeded, %d failed.",
        len(articles), successes, len(failures),
    )
    return Digest(
        tuple(articles[:settings.max_articles]), now, len(articles), successes, tuple(failures),
    )


def coverage_notice(digest: Digest) -> str:
    if not digest.failed_feeds:
        return ""
    return "Partial coverage: these feeds could not be read: " + ", ".join(digest.failed_feeds) + "."


def render_html(digest: Digest, settings: Settings) -> str:
    """Render a table-based email with inline styles and escaped feed content."""
    escape = html.escape
    cards = []
    for article in digest.articles:
        published = article.published.strftime("%b %d, %Y · %H:%M UTC")
        link = (
            f'<a href="{escape(article.url, quote=True)}" '
            'style="color:#18253b;text-decoration:none">'
            f'{escape(article.title)}</a>'
        ) if valid_url(article.url) else escape(article.title)
        cards.append(f"""
<tr><td style="padding:0 0 14px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"
style="background:#ffffff;border:1px solid #e0e6ef;border-radius:12px">
<tr><td style="padding:22px">
<p style="margin:0 0 9px;font-size:12px;color:#5568d9;font-weight:bold">{escape(article.source)}
<span style="color:#718096;font-weight:normal"> · {published}</span></p>
<h2 style="margin:0 0 10px;font-size:19px;line-height:1.4">{link}</h2>
<p style="margin:0;color:#536176;font-size:14px;line-height:1.65">{escape(article.summary)}</p>
</td></tr></table></td></tr>""")
    if not cards:
        cards.append(
            '<tr><td style="padding:24px;background:#fff;border-radius:12px">'
            f"No new articles found in the last {settings.lookback_hours} hours."
            "</td></tr>"
        )
    notice = coverage_notice(digest)
    warning = (
        '<tr><td style="padding:0 0 18px;color:#8a4b08;font-size:13px">'
        f"{escape(notice)}</td></tr>"
    ) if notice else ""
    limit_note = (
        f"Showing the newest {len(digest.articles)} of {digest.total_matches} matching stories. "
        if digest.total_matches > len(digest.articles) else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI News Digest</title></head>
<body style="margin:0;background:#f2f5fa;font-family:Arial,Helvetica,sans-serif;color:#18253b">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>
<td align="center" style="padding:28px 12px">
<table role="presentation" width="640" cellspacing="0" cellpadding="0" style="width:100%;max-width:640px">
<tr><td style="padding:30px 26px;background:#243c83;border-radius:16px;color:#fff">
<p style="margin:0 0 12px;font-size:12px;letter-spacing:2px;color:#c3d3ff">YOUR DAILY SIGNAL</p>
<h1 style="margin:0 0 10px;font-size:32px">AI News Digest</h1>
<p style="margin:0;font-size:14px;color:#dae3ff">AI &amp; markets · {len(digest.articles)} stories · last {settings.lookback_hours} hours</p>
</td></tr><tr><td style="height:22px"></td></tr>
{warning}{"".join(cards)}
<tr><td style="padding:10px 12px;color:#718096;font-size:12px;line-height:1.7;text-align:center">
{limit_note}Generated {digest.generated_at:%Y-%m-%d %H:%M} UTC.<br>
Headlines and excerpts from public RSS feeds. Follow links to read the original stories.
</td></tr></table></td></tr></table></body></html>"""


def render_text(digest: Digest, settings: Settings) -> str:
    lines = [
        "AI News Digest",
        f"{len(digest.articles)} stories from the last {settings.lookback_hours} hours.",
        f"Generated {digest.generated_at:%Y-%m-%d %H:%M} UTC.",
    ]
    if notice := coverage_notice(digest):
        lines.extend(["", notice])
    if not digest.articles:
        lines.extend(["", f"No new articles found in the last {settings.lookback_hours} hours."])
    for article in digest.articles:
        lines.extend([
            "", f"{article.title} — {article.source}",
            f"{article.published:%Y-%m-%d %H:%M} UTC", article.url, article.summary,
        ])
    if digest.total_matches > len(digest.articles):
        lines.extend(["", f"Showing {len(digest.articles)} of {digest.total_matches} matching stories."])
    return "\n".join(lines)


def send_email(digest: Digest, settings: Settings) -> str:
    settings.validate_email()
    subject = f"AI News Digest — {len(digest.articles)} stories"
    if digest.failed_feeds:
        subject += " (partial coverage)"
    payload = json.dumps({
        "from": settings.email_from,
        "to": list(settings.recipients),
        "subject": subject,
        "html": render_html(digest, settings),
        "text": render_text(digest, settings),
    }, sort_keys=True).encode("utf-8")
    request = urllib.request.Request(
        "https://api.resend.com/emails", data=payload,
        headers={
            "Authorization": f"Bearer {settings.resend_api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Idempotency-Key": "digest-" + hashlib.sha256(payload).hexdigest(),
        },
        method="POST",
    )
    response = request_bytes(request, settings, max_bytes=64 * 1024)
    try:
        result = json.loads(response)
    except (ValueError, UnicodeDecodeError) as error:
        raise DigestError("Resend returned an invalid JSON response.") from error
    if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
        raise DigestError("Resend response did not contain an email ID.")
    return result["id"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview without sending email.")
    parser.add_argument("--output", type=Path, help="Save dry-run HTML to this file.")
    args = parser.parse_args(argv)
    if args.output and not args.dry_run:
        parser.error("--output requires --dry-run")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        load_env()
        settings = Settings.from_env(require_email=not args.dry_run)
        digest = collect(settings)
        if args.dry_run:
            body = render_html(digest, settings)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(body, encoding="utf-8")
                LOGGER.info("Preview saved to %s.", args.output)
            else:
                print(body)
        else:
            email_id = send_email(digest, settings)
            LOGGER.info("Email accepted by Resend; id=%s.", email_id)
    except (DigestError, OSError) as error:
        LOGGER.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
