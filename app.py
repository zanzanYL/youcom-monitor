#!/usr/bin/env python3
"""Local Flask dashboard for company news monitoring."""

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, url_for

import monitor


BASE_DIR = Path(__file__).resolve().parent
ALERTS_FILE = BASE_DIR / "alerts.json"
RESEARCH_URL = "https://api.you.com/v1/research"
MAX_ALERTS = 50
MAX_RESEARCH_INPUT = 32000

load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)


def api_key():
    key = os.getenv("YDC_API_KEY")
    if not key:
        raise monitor.MonitorError(
            "YDC_API_KEY is missing. Add it to the .env file in this folder."
        )
    return key


def get_topics():
    topics = monitor.read_json(monitor.TOPICS_FILE, [])
    if not isinstance(topics, list):
        raise monitor.MonitorError(f"{monitor.TOPICS_FILE.name} must contain a JSON list")
    names = []
    for entry in topics:
        if not isinstance(entry, dict) or not isinstance(entry.get("topic"), str):
            raise monitor.MonitorError(f"{monitor.TOPICS_FILE.name} contains an invalid topic")
        name = entry["topic"].strip()
        if name:
            names.append(name)
    return names


def source_excerpt(text, limit):
    text = text.strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def build_research_input(topic, sources, changes=None):
    if changes is None:
        heading = (
            f"Company/topic: {topic}\n"
            "Prepare a concise current brief from these top recent web sources. "
            "Summarize important developments and what changed and why it matters."
        )
        entries = [
            (
                f"[Source {index}] {source.get('title') or source['url']}\n"
                f"URL: {source['url']}\n"
                f"Content:\n{source_excerpt(monitor.source_text(source), 3200)}"
            )
            for index, source in enumerate(sources, start=1)
        ]
    else:
        heading = (
            f"Company/topic: {topic}\n"
            "Analyze the newly discovered or changed web content below against the "
            "previous snapshot. Explain what changed and why it matters. Distinguish "
            "confirmed facts from inference."
        )
        entries = []
        for index, change in enumerate(changes, start=1):
            source = change["source"]
            entry = (
                f"[Source {index}] Change type: {change['kind']}\n"
                f"Title: {source.get('title') or source['url']}\n"
                f"URL: {source['url']}\n"
            )
            previous = change.get("previous")
            if previous:
                entry += (
                    "Previous content:\n"
                    f"{source_excerpt(monitor.source_text(previous), 1500)}\n"
                )
            entry += (
                "Current content:\n"
                f"{source_excerpt(monitor.source_text(source), 1500)}"
            )
            entries.append(entry)

    prompt_suffix = (
        "\n\nAssess whether any of these sources materially disagree. Explain the "
        "specific disagreement if there is one. End with exactly one line: "
        "'Sources disagree: Yes' when claims conflict, or 'Sources disagree: No' "
        "otherwise. Cite claims with source URLs."
    )
    prompt = heading + "\n\n" + "\n\n".join(entries) + prompt_suffix
    if len(prompt) > MAX_RESEARCH_INPUT:
        prompt = prompt[: MAX_RESEARCH_INPUT - len(prompt_suffix)] + prompt_suffix
    return prompt


def safe_sources(sources):
    if not isinstance(sources, list):
        return []
    cleaned = []
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("url"), str):
            continue
        url = source["url"]
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        snippets = source.get("snippets")
        cleaned.append(
            {
                "url": url,
                "title": source.get("title") or url,
                "snippets": (
                    [item for item in snippets if isinstance(item, str)]
                    if isinstance(snippets, list)
                    else []
                ),
            }
        )
    return cleaned


def sources_disagree(content):
    marker = re.search(r"(?im)^\s*Sources disagree:\s*(Yes|No)\s*\.?\s*$", content)
    if marker:
        return marker.group(1).casefold() == "yes"
    if re.search(
        r"\b(?:no|not|do not|does not|don't|doesn't|never)\s+"
        r"(?:the\s+)?sources?\s+(?:materially\s+)?"
        r"(?:disagree|conflict|contradict)\b",
        content,
        re.IGNORECASE,
    ):
        return False
    return bool(
        re.search(
            r"\bsources?\s+(?:materially\s+)?(?:disagree|conflict|contradict)\b"
            r"|\b(?:conflicting|contradictory)\s+"
            r"(?:reports|claims|accounts|findings|evidence|information)\b"
            r"|\b(?:claims|accounts|reports|findings)\s+"
            r"(?:materially\s+)?(?:conflict|contradict)\b",
            content,
            re.IGNORECASE,
        )
    )


def run_research(topic, sources, changes=None, kind="brief"):
    response = monitor.api_post(
        RESEARCH_URL,
        api_key(),
        {
            "input": build_research_input(topic, sources, changes),
            "research_effort": "lite",
        },
    )
    output = response.get("output") if isinstance(response, dict) else None
    content = output.get("content") if isinstance(output, dict) else None
    if not isinstance(content, str):
        raise monitor.MonitorError("Research API returned no text in output.content")

    brief = re.sub(
        r"(?im)^\s*Sources disagree:\s*(?:Yes|No)\s*\.?\s*$",
        "",
        content,
    ).strip()
    created_at = datetime.now(timezone.utc).isoformat()
    return {
        "id": created_at,
        "created_at": created_at,
        "topic": topic,
        "kind": kind,
        "brief": brief,
        "sources_disagree": sources_disagree(content),
        "sources": safe_sources(output.get("sources")),
        "changes": [
            {
                "kind": change["kind"],
                "title": change["source"].get("title") or change["source"]["url"],
                "url": change["source"]["url"],
            }
            for change in changes or []
        ],
    }


def load_alerts():
    alerts = monitor.read_json(ALERTS_FILE, [])
    if not isinstance(alerts, list):
        raise monitor.MonitorError(f"{ALERTS_FILE.name} must contain a JSON list")
    return alerts[:MAX_ALERTS]


def save_alert(alert):
    alerts = load_alerts()
    alerts.insert(0, alert)
    monitor.write_json(ALERTS_FILE, alerts[:MAX_ALERTS])


def page(error=None, message=None, status=200):
    try:
        topics = get_topics()
        alerts = load_alerts()
    except monitor.MonitorError as issue:
        error = str(issue)
        topics = []
        alerts = []
        status = 500
    return (
        render_template(
            "index.html",
            topics=topics,
            alerts=alerts,
            error=error,
            message=message or request.args.get("message"),
        ),
        status,
    )


@app.get("/")
def index():
    return page()


@app.post("/add")
def add_company():
    topic = request.form.get("company", "").strip()
    if not topic:
        return page(error="Enter a company or topic to add.", status=400)
    try:
        added = monitor.add_topic(topic)
    except monitor.MonitorError as error:
        return page(error=str(error), status=500)
    message = f"Added {topic}." if added else f"{topic} is already being monitored."
    return redirect(url_for("index", message=message))


@app.post("/check")
def check_now():
    try:
        topics = get_topics()
        key = api_key()
        found = 0
        for topic in topics:
            result = monitor.check_topic(topic, key, persist=False)
            changes = result["changes"]
            if changes:
                alert = run_research(
                    topic,
                    [item["source"] for item in changes],
                    changes,
                    "change",
                )
                save_alert(alert)
                found += 1
            monitor.save_snapshot(topic, result["current"])
        message = (
            f"Checked {len(topics)} compan{'y' if len(topics) == 1 else 'ies'}; "
            f"created {found} alert{'s' if found != 1 else ''}."
        )
        return redirect(url_for("index", message=message))
    except monitor.MonitorError as error:
        return page(error=str(error), status=502)


@app.post("/brief")
def show_brief():
    topic = request.form.get("topic", "").strip()
    try:
        if topic not in get_topics():
            return page(error="Choose a company from the monitored topics.", status=400)
        current = monitor.fetch_topic(topic, api_key())
        if not current["sources"]:
            return page(error=f"No current sources were found for {topic}.", status=502)
        alert = run_research(topic, current["sources"], kind="brief")
        save_alert(alert)
        return redirect(url_for("index", message=f"Generated a current brief for {topic}."))
    except monitor.MonitorError as error:
        return page(error=str(error), status=502)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
