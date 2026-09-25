"""Capture small, real responses for adapter tests.

Run: uv run python scripts/capture_fixtures.py
"""

import json
import urllib.request
from pathlib import Path

UA = "augury-fixture-capture/0.1"
OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def save_json(name: str, data: object) -> None:
    path = OUT / "hf" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print("wrote", path)


def main() -> None:
    papers = json.loads(fetch("https://huggingface.co/api/daily_papers?sort=trending&limit=5"))
    save_json("daily_papers.json", papers[:5])
    blog = json.loads(fetch("https://huggingface.co/api/blog"))
    blog["allBlogs"] = blog["allBlogs"][:5]
    blog.pop("communityBlogPosts", None)
    save_json("blog.json", blog)
    community = json.loads(fetch("https://huggingface.co/api/blog/community?sort=trending"))
    community["posts"] = community["posts"][:5]
    save_json("community.json", community)


if __name__ == "__main__":
    main()
