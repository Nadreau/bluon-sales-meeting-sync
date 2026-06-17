# Bluon — Daily Sales Meeting → Notion sync

Mirrors the **daily 10am ET sales standup** AI meeting note out of Niko's
personal Notion workspace into the shared Bluon **Internal Sales Meetings**
database, so the whole sales team can read it without anyone having to
transcribe the call live.

Built in response to Jonathan's ask (Jun 17, 2026): *"is there a way to make
this particular daily sales meeting's notes available to the group?"*

## What it does each morning

1. Finds today's standup in the personal **Meeting Notes** database — the
   meeting whose start time lands in the **9:45–10:45 ET** window (per Niko, no
   other calls are scheduled at 10am ET).
2. Waits until the AI summary has finished generating (`status = notes_ready`).
3. Confirms it's a sales call (title contains a sales hint like *standup / sales
   / account management*). A named non-sales 10am meeting is skipped.
4. Copies into a new dated row in the Bluon database:
   - **Name** — `Daily Sales Standup — <Month D, YYYY>`
   - **Topic** — the meeting's title
   - **Action Items** — the AI action-item list
   - **Body** — full AI summary + action items, with the **full transcript** in
     a collapsed toggle.
5. Idempotent: if today's row already exists, it does nothing.

## Source / destination IDs

| | Notion data source id |
|---|---|
| Source — personal "Meeting Notes" | `2b2ac0ae-c1df-8090-9b8b-000baee1f4dc` |
| Destination — Bluon "Internal Sales Meetings" | `2cc576a5-c12d-8010-b539-000b28ef2ad8` |

Override either with the `PERSONAL_MEETINGS_DS` / `BLUON_DS` env vars.

## Running locally

```bash
pip install -r requirements.txt
export NOTION_PERSONAL_TOKEN="$(cat ~/.config/notion/api_key)"
export NOTION_BLUON_TOKEN="$(cat ~/.config/notion/bluon_api_key)"

python sync.py --dry-run        # detect + extract, write nothing
python sync.py                  # sync today
python sync.py --date 2026-06-17
python sync.py --force           # recreate even if today's row exists
```

## Running in GitHub Actions

Add two repository secrets:

- `NOTION_PERSONAL_TOKEN` — integration token with read access to the personal
  Meeting Notes database (contents of `~/.config/notion/api_key`).
- `NOTION_BLUON_TOKEN` — integration token with insert access to the Bluon
  Internal Sales Meetings database (contents of `~/.config/notion/bluon_api_key`).

The workflow (`.github/workflows/sync.yml`) runs Mon–Sat on a few late-morning
ET schedules (covers EDT/EST and gives the AI summary time to finish). You can
also trigger it manually from the **Actions** tab (`workflow_dispatch`), with
optional `date` / `force` inputs.

> Keep this repo **private** — `NOTION_PERSONAL_TOKEN` reaches Niko's personal
> workspace.
