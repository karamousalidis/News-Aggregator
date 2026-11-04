"""Build a clean source ZIP for a new repo; omit secrets and existing Git history."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = (
    ".env.example", ".gitattributes", ".gitignore", ".python-version",
    "LICENSE", "README.md",
    "SCHEDULING_OPTIONS.md", "news_digest.py", "run.md", "schedule.ps1", "schedule_macos.sh",
)
SOURCE_TYPES = {".py", ".md", ".yml", ".yaml", ".html", ".png", ".svg"}


def source_files() -> list[Path]:
    paths = [ROOT / name for name in ROOT_FILES]
    for folder in (".github", "docs", "examples", "tests", "tools"):
        paths.extend(
            path for path in (ROOT / folder).rglob("*")
            if path.is_file() and path.suffix in SOURCE_TYPES
            and "__pycache__" not in path.parts and not path.is_symlink()
            and not path.name.startswith(".env")
        )
    missing = [str(path.relative_to(ROOT)) for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing source files: " + ", ".join(missing))
    return sorted(paths)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "exports" / "ai-news-digest-source.zip")
    args = parser.parse_args()
    paths = source_files()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, "w", ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, path.relative_to(ROOT).as_posix())
    print(f"Clean source ZIP: {args.output}")


if __name__ == "__main__":
    main()
