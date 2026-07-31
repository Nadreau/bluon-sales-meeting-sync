#!/usr/bin/env python3
"""
Bluon daily sales-meeting sync.

Copies the AI meeting note for the daily sales standup out of Niko's PERSONAL
Notion workspace and into the shared Bluon "Internal Sales Meetings" database so
the whole sales team can read it (summary + action items + full transcript).

Why this exists: Jonathan is usually on his phone during the 10am ET standup and
can't transcribe it. Niko's personal Notion records + summarizes it. This script
mirrors that note into the team space on a schedule.

Detection: the standup is the meeting whose `Meeting Date` falls on the target
day with a start time inside the morning ET window (default 09:45-10:45 ET).
Per Niko, no other calls are scheduled at 10am ET (the team is West Coast), so a
10am ET meeting is reliably the sales standup. We additionally require the note's
status to be `notes_ready` (the AI summary has finished generating).

Idempotent: if an entry for the target day already exists in the Bluon database,
the script does nothing (safe to run from multiple cron times / reruns).

Env vars:
  NOTION_PERSONAL_TOKEN   integration token with read access to Niko's personal
                          "Meeting Notes" database (source)
  NOTION_BLUON_TOKEN      integration token with insert access to the Bluon
                          "Internal Sales Meetings" database (destination)

Optional flags:
  --date YYYY-MM-DD   override target day (default: today in America/New_York)
  --dry-run           detect + extract, but write nothing
  --force             create the entry even if one already exists for the day
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import transcript_clean

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
NOTION_VERSION = "2025-09-03"
ET = ZoneInfo("America/New_York")

# Source: Niko's personal "Meeting Notes" database (data source id)
PERSONAL_MEETINGS_DS = os.environ.get(
    "PERSONAL_MEETINGS_DS", "2b2ac0ae-c1df-8090-9b8b-000baee1f4dc"
)
# Destination: Bluon "Internal Sales Meetings" database (data source id)
BLUON_DS = os.environ.get("BLUON_DS", "2cc576a5-c12d-8010-b539-000b28ef2ad8")

# Morning ET window that identifies the standup (inclusive minutes-of-day)
WINDOW_START = (9, 45)   # 09:45 ET
WINDOW_END = (10, 45)    # 10:45 ET

# Keywords that increase confidence this is the sales standup (informational).
SALES_HINTS = ["standup", "sales", "account management", "marketing", "team"]

PERSONAL_TOKEN = os.environ.get("NOTION_PERSONAL_TOKEN", "")
BLUON_TOKEN = os.environ.get("NOTION_BLUON_TOKEN", "")

CLONEABLE_TYPES = {
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "to_do", "quote",
    "callout", "divider",
}


# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------
def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def api(token, method, path, **kwargs):
    url = f"https://api.notion.com/{path.lstrip('/')}"
    r = requests.request(method, url, headers=_headers(token), timeout=60, **kwargs)
    if r.status_code >= 300:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
    return r.json() if r.text else {}


def all_children(token, block_id):
    """Return every child block of `block_id`, following pagination."""
    out, cursor = [], None
    while True:
        path = f"v1/blocks/{block_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        d = api(token, "GET", path)
        out += d.get("results", [])
        if d.get("has_more"):
            cursor = d["next_cursor"]
        else:
            return out


# ----------------------------------------------------------------------------
# Source extraction (personal workspace)
# ----------------------------------------------------------------------------
def rich_text_plain(rt):
    return "".join(x.get("plain_text", "") for x in (rt or []))


def meeting_start(page):
    """Parse the meeting start time (ET) for a personal meeting-note page.

    The note's start time is stored as the page TITLE (property "Meeting Date"),
    an ISO string like '2026-06-17T10:00:00.000-04:00'. Fall back to the page's
    created_time if the title is not parseable.
    """
    title = rich_text_plain(page.get("properties", {}).get("Meeting Date", {}).get("title"))
    for value in (title.strip(), page.get("created_time", "")):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ET)
        except (ValueError, AttributeError):
            continue
    return None


def find_standup_candidates(target_date):
    """Return ALL morning-window meeting notes for `target_date`, earliest first.

    There can be more than one note around 10am (e.g. an empty placeholder note
    plus the real standup). The caller picks the first that is `notes_ready` and
    passes the sales-hint guard, rather than blindly taking the earliest.
    """
    day_start = datetime.combine(target_date, datetime.min.time(), ET)
    day_end = day_start + timedelta(days=1)
    body = {
        "filter": {
            "and": [
                {"timestamp": "created_time", "created_time": {"on_or_after": day_start.isoformat()}},
                {"timestamp": "created_time", "created_time": {"before": day_end.isoformat()}},
            ]
        },
        "page_size": 50,
    }
    d = api(PERSONAL_TOKEN, "POST", f"v1/data_sources/{PERSONAL_MEETINGS_DS}/query", json=body)

    lo = WINDOW_START[0] * 60 + WINDOW_START[1]
    hi = WINDOW_END[0] * 60 + WINDOW_END[1]
    candidates = []
    for page in d.get("results", []):
        start = meeting_start(page)
        if not start or start.date() != target_date:
            continue
        minutes = start.hour * 60 + start.minute
        if lo <= minutes <= hi:
            candidates.append((start, page))

    candidates.sort(key=lambda t: t[0])
    return [c[1] for c in candidates]


# Notion's AI meeting-note wrapper block is named differently across API
# versions: `meeting_notes` (older) vs `transcription` (2025-09-03).
MEETING_BLOCK_TYPES = {"meeting_notes", "transcription"}


def get_meeting_note_block(page_id):
    """Return the AI meeting-note wrapper block on the page, or None."""
    for b in all_children(PERSONAL_TOKEN, page_id):
        if b["type"] in MEETING_BLOCK_TYPES:
            return b
    return None


def extract_note(page):
    """Pull title, summary blocks, action items, and transcript from a note page.

    Returns dict: {title, status, summary_blocks, action_items, transcript_lines}
    or None if the AI summary is not ready yet.
    """
    page_id = page["id"]
    mn = get_meeting_note_block(page_id)
    if not mn:
        return None
    mn_data = mn.get(mn["type"], {})
    status = mn_data.get("status")
    title = rich_text_plain(mn_data.get("title")) or "Daily Sales Standup"
    # The block title often ends with an inlined date mention (e.g. "... 2026-06-17");
    # strip a trailing ISO date so the Topic reads cleanly.
    title = re.sub(r"\s+\d{4}-\d{2}-\d{2}\s*$", "", title)

    # The meeting_notes block wraps 2-3 paragraph containers: summary, (notes),
    # transcript. Detect each by content rather than relying on order.
    wrappers = all_children(PERSONAL_TOKEN, mn["id"])
    summary_blocks, transcript_lines = [], []
    for w in wrappers:
        if not w.get("has_children"):
            continue
        kids = all_children(PERSONAL_TOKEN, w["id"])
        texts = [rich_text_plain(k.get(k["type"], {}).get("rich_text")) for k in kids]
        joined = " ".join(texts)[:200].lower()
        is_transcript = "transcribing this meeting" in joined or (
            len(kids) > 10 and all(k["type"] == "paragraph" for k in kids)
        )
        if is_transcript:
            for k in kids:
                t = rich_text_plain(k.get("paragraph", {}).get("rich_text")).strip()
                if t and t.lower() != "notion ai is transcribing this meeting.":
                    transcript_lines.append(t)
        else:
            summary_blocks.extend(kids)

    # Action items = to_do blocks before the first divider in the summary.
    action_items = []
    for b in summary_blocks:
        if b["type"] == "divider":
            break
        if b["type"] == "to_do":
            txt = rich_text_plain(b.get("to_do", {}).get("rich_text")).strip()
            if txt:
                action_items.append(txt)

    return {
        "title": title.strip(),
        "status": status,
        "summary_blocks": summary_blocks,
        "action_items": action_items,
        "transcript_lines": transcript_lines,
    }


# ----------------------------------------------------------------------------
# Block construction (for the destination page)
# ----------------------------------------------------------------------------
def rt(content, bold=False, italic=False, color="default"):
    out, content = [], content or ""
    # Notion caps a single rich_text object at 2000 chars.
    for i in range(0, max(len(content), 1), 2000):
        chunk = content[i:i + 2000]
        out.append({
            "type": "text",
            "text": {"content": chunk},
            "annotations": {"bold": bold, "italic": italic, "strikethrough": False,
                            "underline": False, "code": False, "color": color},
        })
    return out


def clone_rich_text(rich):
    """Rebuild a rich_text array as plain text+annotations (drops mentions/links)."""
    out = []
    for x in rich or []:
        plain = x.get("plain_text", "")
        if not plain:
            continue
        ann = x.get("annotations", {}) or {}
        out.append({
            "type": "text",
            "text": {"content": plain[:2000]},
            "annotations": {
                "bold": ann.get("bold", False), "italic": ann.get("italic", False),
                "strikethrough": ann.get("strikethrough", False),
                "underline": ann.get("underline", False), "code": ann.get("code", False),
                "color": ann.get("color", "default"),
            },
        })
    return out or rt("")


def clone_block(b):
    """Convert a source block into a fresh block payload for the new page."""
    t = b["type"]
    if t == "divider":
        return {"object": "block", "type": "divider", "divider": {}}
    if t not in CLONEABLE_TYPES:
        # Fall back to a paragraph of plain text for unsupported block types.
        plain = rich_text_plain(b.get(t, {}).get("rich_text"))
        return {"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": rt(plain)}}
    src = b.get(t, {})
    payload = {"rich_text": clone_rich_text(src.get("rich_text"))}
    if t == "to_do":
        payload["checked"] = src.get("checked", False)
    if t == "callout":
        payload["icon"] = src.get("icon") or {"type": "emoji", "emoji": "💡"}
    return {"object": "block", "type": t, t: payload}


def heading(text, level=2):
    return {"object": "block", "type": f"heading_{level}",
            f"heading_{level}": {"rich_text": rt(text, bold=False)}}


def callout(text, emoji="🤖"):
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": rt(text), "icon": {"type": "emoji", "emoji": emoji},
                        "color": "gray_background"}}


def paragraph(text):
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rt(text)}}


# ----------------------------------------------------------------------------
# Destination write (Bluon workspace)
# ----------------------------------------------------------------------------
def existing_entry(readable_date):
    """Return True if an entry for `readable_date` already exists in Bluon DB."""
    d = api(BLUON_TOKEN, "POST", f"v1/data_sources/{BLUON_DS}/query",
            json={"page_size": 100})
    for p in d.get("results", []):
        name = rich_text_plain(p.get("properties", {}).get("Name", {}).get("title"))
        if readable_date in name:
            return p
    return None


def chunk_text_property(items, limit=1900):
    """Pack action items into rich_text chunks under Notion's 2000-char cap."""
    blob = "\n".join(f"• {x}" for x in items)
    chunks, cur = [], ""
    for line in blob.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    rich = []
    for c in chunks:
        rich.append({"type": "text", "text": {"content": c[:2000]}})
    return rich or [{"type": "text", "text": {"content": ""}}]


def append_children(page_or_block_id, blocks):
    """Append blocks in batches of <=100."""
    for i in range(0, len(blocks), 90):
        api(BLUON_TOKEN, "PATCH", f"v1/blocks/{page_or_block_id}/children",
            json={"children": blocks[i:i + 90]})


def create_entry(note, readable_date, target_date):
    title_name = f"Daily Sales Standup — {readable_date}"
    props = {
        "Name": {"title": rt(title_name)},
        "Topic": {"rich_text": rt(note["title"])},
        "Action Items": {"rich_text": chunk_text_property(note["action_items"])},
    }
    intro = callout(
        f"Auto-synced from the daily sales standup recording • {readable_date}. "
        "Summary and action items are below; the full transcript is in the toggle "
        "at the bottom."
    )
    page = api(BLUON_TOKEN, "POST", "v1/pages", json={
        "parent": {"type": "data_source_id", "data_source_id": BLUON_DS},
        "properties": props,
        "children": [intro, heading("Summary & Action Items", 2)],
    })
    page_id = page["id"]

    # Summary blocks (may exceed 100 -> batched).
    summary = [clone_block(b) for b in note["summary_blocks"]]
    if summary:
        append_children(page_id, summary)

    # Transcript inside a collapsed toggle.
    if note["transcript_lines"]:
        toggle = {
            "object": "block", "type": "toggle",
            "toggle": {"rich_text": rt(f"🎙️ Full Transcript ({len(note['transcript_lines'])} segments)")},
        }
        res = api(BLUON_TOKEN, "PATCH", f"v1/blocks/{page_id}/children",
                  json={"children": [heading("Transcript", 2), toggle]})
        toggle_id = [b for b in res["results"] if b["type"] == "toggle"][0]["id"]
        tblocks = [paragraph(line) for line in note["transcript_lines"]]
        # Say so when audio was dropped, rather than quietly shipping a short
        # transcript — the team should never wonder whether content went missing.
        if note.get("trimmed_count"):
            tblocks.append(paragraph(
                f"— {note['trimmed_count']} trailing segment(s) omitted here: "
                f"audio recorded after the meeting ended (mic left on). "
                f"The full recording is intact in the original note. —"))
        append_children(toggle_id, tblocks)

    return page


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="target day YYYY-MM-DD (default: today ET)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if not PERSONAL_TOKEN or not BLUON_TOKEN:
        sys.exit("ERROR: set NOTION_PERSONAL_TOKEN and NOTION_BLUON_TOKEN")

    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target_date = datetime.now(ET).date()
    readable_date = target_date.strftime("%B %-d, %Y")
    print(f"[sync] target day: {readable_date} (ET)")

    candidates = find_standup_candidates(target_date)
    if not candidates:
        print("[sync] no 10am-ET meeting note found for this day — nothing to do.")
        return
    print(f"[sync] {len(candidates)} candidate 10am note(s) found")

    # Pick the first candidate that is ready AND looks like the sales standup.
    # Skip empty placeholder notes and non-sales 10am meetings.
    note, pending = None, False
    for page in candidates:
        n = extract_note(page)
        if not n:
            continue
        if n["status"] != "notes_ready":
            pending = True
            print(f"[sync]   - {n['title'][:40]!r} not ready (status={n['status']})")
            continue
        hint = [h for h in SALES_HINTS if h in n["title"].lower()]
        if n["title"] and not hint and not args.force:
            print(f"[sync]   - {n['title'][:40]!r} ready but no sales hint — not the standup")
            continue
        note = n
        break

    if not note:
        if pending:
            print("[sync] standup summary not ready yet — will retry on the next run.")
        else:
            print("[sync] no qualifying sales standup among today's 10am notes — nothing to do.")
        return

    hint = [h for h in SALES_HINTS if h in note["title"].lower()]
    print(f"[sync] using: {note['title']!r}  (hints: {hint or 'none'})")
    print(f"[sync] action items: {len(note['action_items'])}, "
          f"summary blocks: {len(note['summary_blocks'])}, "
          f"transcript segments: {len(note['transcript_lines'])}")

    # Niko sometimes leaves his mic on, so the recording picks up whatever plays
    # after the call. Keep that out of the shared team database — his personal
    # note is never touched. See transcript_clean.py for how this is judged.
    if not transcript_clean.looks_like_meeting(note["transcript_lines"]):
        print("[sync] transcript is essentially all non-meeting audio "
              "(mic left on, no standup content) — nothing to mirror.")
        return

    kept, removed, reason = transcript_clean.clean_transcript(note["transcript_lines"])
    if removed:
        print(f"[sync] 🧹 {reason}")
        note["transcript_lines"] = kept
        note["trimmed_count"] = len(removed)

    dup = existing_entry(readable_date)
    if dup and not args.force:
        print(f"[sync] entry for {readable_date} already exists ({dup['id']}) — skipping.")
        return

    if args.dry_run:
        print("[sync] DRY RUN — no write performed.")
        return

    created = create_entry(note, readable_date, target_date)
    url = created.get("url", "(no url)")
    print(f"[sync] ✅ created entry: {url}")


if __name__ == "__main__":
    main()
