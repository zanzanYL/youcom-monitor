#!/usr/bin/env python3
"""Track recent web results for a list of topics."""

import argparse
import difflib
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
TOPICS_FILE = BASE_DIR / "topics.json"
SNAPSHOTS_DIR = BASE_DIR / "snapshots"
SEARCH_URL = "https://ydc-index.io/v1/search"
CONTENTS_URL = "https://ydc-index.io/v1/contents"
REQUEST_TIMEOUT = 60


class MonitorError(Exception):
    """An expected configuration, API, or persisted-data error."""


def topic_id(topic):
    return hashlib.sha256(topic.casefold().encode("utf-8")).hexdigest()


def read_json(path, default):
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise MonitorError(f"Could not read {path.name}: {error}") from error


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary_path, path)
    except OSError as error:
        raise MonitorError(f"Could not write {path}: {error}") from error
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def api_post(url, api_key, body):
    try:
        response = requests.post(
            url,
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.JSONDecodeError as error:
        raise MonitorError(f"API response from {url} was not valid JSON") from error
    except requests.RequestException as error:
        raise MonitorError(f"API request to {url} failed: {error}") from error


def normalized_url(url):
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, "")
    )


def fetch_topic(topic, api_key):
    search_response = api_post(
        SEARCH_URL,
        api_key,
        {
            "query": topic,
            "count": 5,
            "freshness": "week",
            "extraction": {"extraction_mode": "highlights"},
        },
    )
    if not isinstance(search_response, dict):
        raise MonitorError("Search API returned an invalid response (expected an object)")
    result_groups = search_response.get("results") or {}
    if not isinstance(result_groups, dict):
        raise MonitorError(f"Search API returned invalid results for {topic!r}")
    results = result_groups.get("web", [])
    if not isinstance(results, list):
        raise MonitorError(f"Search API returned an invalid results list for {topic!r}")

    sources = []
    for result in results:
        if not isinstance(result, dict) or not isinstance(result.get("url"), str):
            continue
        contents = result.get("contents") or {}
        highlights = contents.get("highlights", []) if isinstance(contents, dict) else []
        sources.append(
            {
                "url": result["url"],
                "title": result.get("title") or result["url"],
                "description": result.get("description"),
                "page_age": result.get("page_age"),
                "highlights": (
                    [item for item in highlights if isinstance(item, str)]
                    if isinstance(highlights, list)
                    else []
                ),
                "content": None,
            }
        )

    if sources:
        pages = api_post(
            CONTENTS_URL,
            api_key,
            {
                "urls": [source["url"] for source in sources],
                "formats": ["markdown"],
            },
        )
        if not isinstance(pages, list):
            raise MonitorError("Contents API returned an invalid response (expected a list)")
        pages_by_url = {
            normalized_url(page["url"]): page
            for page in pages
            if isinstance(page, dict) and page.get("url")
        }
        for source in sources:
            page = pages_by_url.get(normalized_url(source["url"]))
            if page:
                source["title"] = page.get("title") or source["title"]
                markdown = page.get("markdown")
                source["content"] = markdown if isinstance(markdown, str) else None

    return {
        "topic": topic,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
    }


def source_text(source):
    if source.get("content"):
        return source["content"]
    return "\n".join(source.get("highlights") or []) or source.get("description") or ""


def find_changes(previous, current):
    previous_sources = {normalized_url(item["url"]): item for item in previous["sources"]}
    current_sources = {normalized_url(item["url"]): item for item in current["sources"]}
    changes = []

    for key, source in current_sources.items():
        old_source = previous_sources.get(key)
        if old_source is None:
            changes.append({"kind": "new", "source": source, "previous": None})
            continue

        old_text = source_text(old_source)
        new_text = source_text(source)
        metadata_changed = (
            old_source.get("title") != source.get("title")
            or old_source.get("description") != source.get("description")
        )
        if old_text == new_text and not metadata_changed:
            continue

        changes.append({"kind": "changed", "source": source, "previous": old_source})

    return changes


def print_changes(changes):
    for change in changes:
        source = change["source"]
        label = "NEW" if change["kind"] == "new" else "CHANGED"
        print(f"\n{label}: {source['title']}\n  {source['url']}")
        previous = change["previous"]
        if previous is None:
            excerpt = source_text(source).strip().replace("\n", " ")
            if len(excerpt) > 400:
                excerpt = excerpt[:397].rstrip() + "..."
            if excerpt:
                print(f"  {excerpt}")
            continue

        old_text = source_text(previous)
        new_text = source_text(source)
        if old_text != new_text:
            diff = list(
                difflib.unified_diff(
                    old_text.splitlines(),
                    new_text.splitlines(),
                    fromfile="previous",
                    tofile="current",
                    lineterm="",
                    n=1,
                )
            )
            for line in diff[2:32]:
                print(f"  {line}")
            if len(diff) > 32:
                print("  ... (diff truncated)")
        else:
            print("  Page title or description changed.")


def check_topic(topic, api_key, persist=True):
    snapshot_path = SNAPSHOTS_DIR / f"{topic_id(topic)}.json"
    previous = read_json(snapshot_path, None)
    current = fetch_topic(topic, api_key)
    changes = (
        find_changes(previous, current)
        if previous is not None
        else [{"kind": "new", "source": source, "previous": None} for source in current["sources"]]
    )
    if persist:
        save_snapshot(topic, current)
    return {"current": current, "previous": previous, "changes": changes}


def save_snapshot(topic, snapshot):
    write_json(SNAPSHOTS_DIR / f"{topic_id(topic)}.json", snapshot)


def print_check_result(result):
    if result["previous"] is None:
        print("Initial snapshot:")
        for source in result["current"]["sources"]:
            print(f"- {source['title']}\n  {source['url']}")
    elif result["changes"]:
        print_changes(result["changes"])
    else:
        print("No new or changed results.")


def add_topic(topic):
    topics = read_json(TOPICS_FILE, [])
    if not isinstance(topics, list):
        raise MonitorError(f"{TOPICS_FILE.name} must contain a JSON list")
    if any(
        isinstance(entry, dict)
        and str(entry.get("topic", "")).casefold() == topic.casefold()
        for entry in topics
    ):
        return False
    topics.append({"id": topic_id(topic), "topic": topic})
    write_json(TOPICS_FILE, topics)
    return True


def check_topics():
    api_key = os.getenv("YDC_API_KEY")
    if not api_key:
        raise MonitorError(
            "YDC_API_KEY is missing. Add it to the .env file in this folder."
        )

    topics = read_json(TOPICS_FILE, [])
    if not isinstance(topics, list):
        raise MonitorError(f"{TOPICS_FILE.name} must contain a JSON list")
    if not topics:
        print('No topics configured. Add one with: python monitor.py add "your topic"')
        return

    for entry in topics:
        if not isinstance(entry, dict) or not entry.get("topic"):
            raise MonitorError(f"{TOPICS_FILE.name} contains an invalid topic entry")
        topic = entry["topic"]
        identifier = topic_id(topic)
        if entry.get("id") not in (None, identifier):
            raise MonitorError(f"{TOPICS_FILE.name} contains an invalid id for {topic!r}")
        print(f"\nChecking: {topic}")
        result = check_topic(topic, api_key)
        print_check_result(result)


def main():
    load_dotenv(BASE_DIR / ".env")
    parser = argparse.ArgumentParser(description="Monitor recent web results by topic.")
    commands = parser.add_subparsers(dest="command", required=True)
    add_parser = commands.add_parser("add", help="Add a topic to monitor")
    add_parser.add_argument("topic", help="Topic to monitor")
    commands.add_parser("check", help="Fetch results and compare with saved snapshots")
    args = parser.parse_args()

    try:
        if args.command == "add":
            topic = args.topic.strip()
            if not topic:
                raise MonitorError("Topic cannot be empty")
            if add_topic(topic):
                print(f"Added topic: {topic}")
            else:
                print(f"Already monitoring: {topic}")
        else:
            check_topics()
    except MonitorError as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
