"""Generate the checked-in demo using fictional stories and the real renderer."""

import argparse
import html
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

from news_digest import Article, Digest, Settings, render_html

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "sample-digest.html"
ILLUSTRATION = OUTPUT.with_name("digest-preview.svg")


def sample_digest() -> Digest:
    now = datetime(2026, 10, 30, 6, 15, tzinfo=timezone.utc)
    stories = (
        Article(
            "Example Research Journal", "Open models bring new tools to AI research",
            "https://example.com/ai-research", now - timedelta(hours=1),
            "Illustrative story: a research team releases open tools for evaluating "
            "language models. This demo uses fictional headlines, publishers, and excerpts.",
        ),
        Article(
            "Example Markets", "Your watchlist: the latest in AI infrastructure",
            "https://example.com/ai-infrastructure", now - timedelta(hours=3),
            "Illustrative story: follow the companies building chips, cloud platforms, "
            "and the infrastructure behind AI. Add your own ticker symbols to personalize the digest.",
        ),
        Article(
            "Example Technology Review", "A daily briefing, delivered to your inbox",
            "https://example.com/daily-briefing", now - timedelta(hours=5),
            "Illustrative story: public RSS headlines become a readable email with "
            "publisher links, publication times, and a plain-text alternative.",
        ),
    )
    return Digest(stories, now, len(stories), 5)


def sample_svg(digest: Digest) -> str:
    """A code-generated illustration, not an email-client screenshot."""
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="760" height="1000" viewBox="0 0 760 1000">',
        '<title>AI News Digest: illustrative email layout with fictional stories</title>',
        '<rect width="760" height="1000" fill="#f2f5fa"/>',
        '<g font-family="Arial, Helvetica, sans-serif">',
        '<rect x="52" y="40" width="656" height="166" rx="16" fill="#243c83"/>',
        '<text x="80" y="82" fill="#c3d3ff" font-size="12" letter-spacing="2">YOUR DAILY SIGNAL</text>',
        '<text x="80" y="128" fill="#fff" font-size="34" font-weight="bold">AI News Digest</text>',
        '<text x="80" y="167" fill="#dae3ff" font-size="15">AI &amp; markets · 3 stories · last 24 hours</text>',
    ]
    for index, article in enumerate(digest.articles):
        top = 232 + index * 224
        parts.extend([
            f'<rect x="52" y="{top}" width="656" height="206" rx="12" fill="#fff" stroke="#e0e6ef"/>',
            f'<text x="80" y="{top+31}" fill="#5568d9" font-size="12" font-weight="bold">{html.escape(article.source)}</text>',
            f'<text x="680" y="{top+31}" text-anchor="end" fill="#718096" font-size="12">{article.published:%b %d, %Y · %H:%M} UTC</text>',
        ])
        for line_index, line in enumerate(textwrap.wrap(article.title, width=48)):
            parts.append(
                f'<text x="80" y="{top+66+line_index*27}" fill="#18253b" font-size="22" '
                f'font-weight="bold">{html.escape(line)}</text>'
            )
        for line_index, line in enumerate(textwrap.wrap(article.summary, width=76)):
            parts.append(
                f'<text x="80" y="{top+123+line_index*23}" fill="#536176" font-size="15">'
                f'{html.escape(line)}</text>'
            )
    parts.extend([
        '<text x="380" y="932" text-anchor="middle" fill="#718096" font-size="13">Illustrative layout · fictional stories · generated from the demo fixture</text>',
        '<text x="380" y="957" text-anchor="middle" fill="#718096" font-size="12">Public RSS headlines. A daily briefing in your inbox.</text>',
        '</g></svg>',
    ])
    return "\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the sample is out of date.")
    args = parser.parse_args()
    digest = sample_digest()
    outputs = {OUTPUT: render_html(digest, Settings()), ILLUSTRATION: sample_svg(digest)}
    if args.check:
        if any(
            not path.exists() or path.read_text(encoding="utf-8") != content
            for path, content in outputs.items()
        ):
            print("Sample is out of date. Run: python -m examples.generate_preview")
            return 1
        print("Sample matches the current renderer.")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    for path, content in outputs.items():
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"Sample written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
