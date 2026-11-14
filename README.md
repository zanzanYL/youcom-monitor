# You.com Company Monitor

A local CLI and Flask dashboard that tracks recent web results for companies,
fetches source pages as Markdown, and creates cited Research API briefs for new
or changed content.

## Setup

1. Create and activate a virtual environment, then install dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Create a `.env` file in this folder:

   ```dotenv
   YDC_API_KEY=your_api_key
   ```

   Get an API key from [You.com](https://you.com/platform). The key is loaded
   from `.env`; it is not stored in the source code or displayed in the app.

## Web app

Start the local dashboard:

```bash
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000). Add a company, then use:

- **Check now** to search all monitored companies. New or changed sources are
  sent to the Research API with `research_effort: "lite"` for a brief about
  what changed and why it matters.
- **Show brief now** to generate a cited brief from the selected company's
  current top sources even when there are no changes.

Briefs and clickable citations appear in the alert feed. A **Sources disagree**
badge is shown when Research marks a material conflict between sources.

## CLI

```bash
python monitor.py add "Stripe"
python monitor.py check
```

Searches request up to five results with `freshness: "week"` and
`extraction_mode: "highlights"`, then fetch result URLs through the Contents
API.

Topics are saved in `topics.json`, snapshots in `snapshots/`, and the web
alert feed in `alerts.json`. These local files are ignored by Git.
