# Quick start

See the [README](README.md) for configuration and cloud delivery.

```bash
# Offline tests and fictional demo; no credentials:
python -m unittest discover -s tests -v
python -m examples.generate_preview

# Live preview; no email sent:
python news_digest.py --dry-run --output preview.html

# Send after configuring .env or environment variables:
python news_digest.py
```

Use `python3` on macOS/Linux if needed.
[Publish to a new GitHub repository](docs/PUBLISHING.md).
