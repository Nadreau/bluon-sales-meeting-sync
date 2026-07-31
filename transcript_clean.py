"""Strip obvious non-meeting audio from the transcript before it goes to Bluon.

Niko sometimes leaves his mic on after the standup, so the recording picks up
whatever is playing afterwards — a TV broadcast, music, audio off his phone.
That junk is harmless in his personal Notion note but it should not land in the
shared team database.

This module ONLY affects the Bluon copy. The personal source note is never
modified.

Design notes (Jul 31 2026)
--------------------------
Calibrated against real data: 24 synced standup transcripts (Jun 17 – Jul 31)
plus the one known junk case, 2026-06-30, whose note the AI actually titled
"Sports Broadcast Audio Captured During Meeting Recording" — 33 lines of World
Cup and tennis commentary, zero meeting content.

Two things that fell out of that data and drive the approach:

1. Every one of the 24 real standups ends on sign-off language ("See you guys",
   "Thanks everybody", "let's have a good one"). That makes the last sign-off a
   reliable anchor for "the meeting ended here."
2. None of the 24 has trailing junk. So the trimmer's first duty is to do
   NOTHING on normal days. It is deliberately biased toward under-trimming:
   losing a little real tail is worse than leaving a little junk, because the
   team reads these and silently dropping meeting content is a trust problem.

Hence a cut requires BOTH signals to agree — the lines come after a sign-off AND
they score as non-meeting content. Either alone is not enough.
"""

import re

# ---------------------------------------------------------------------------
# Tunables. Deliberately conservative — see design notes above.
# ---------------------------------------------------------------------------
MIN_TAIL_LINES = 4      # never bother trimming a tail shorter than this
MAX_TRIM_RATIO = 0.40   # refuse to trim more than this share of a transcript
MIN_JUNK_LINES = 3      # absolute floor: this many lines must be positively junk
MIN_JUNK_DENSITY = 0.25         # share of the tail that must be positively junk
MIN_JUNK_DENSITY_SIGNOFF = 0.15  # relaxed when a sign-off corroborates the cut
ALL_JUNK_RATIO = 0.55   # share of WHOLE transcript that means "no meeting here"
MIN_LINES_FOR_ALL_JUNK = 5

# Meeting is over. Drawn from the real sign-offs across all 24 transcripts.
SIGNOFF_PATTERNS = [
    r"\bsee (you|ya|y'all)\b", r"\bsee you (guys|all|later|today|tomorrow)\b",
    r"\bhave a (good|great) (one|day|week|weekend)\b",
    r"\blet'?s have a (good|great)\b", r"\bthanks?,? (everybody|everyone|all|guys|team)\b",
    r"\bappreciate (it|you|y'all)\b", r"\btalk (soon|later|to you)\b",
    r"\bwe will talk\b", r"\bwe'?ll talk\b", r"\blet'?s (go|rock)\b",
    r"\bhappy friday\b", r"\bbye\b", r"\blater\b", r"\bpeace out\b",
    r"\bgot to hop\b", r"\bgotta hop\b", r"\bi'?ll hop off\b",
]
SIGNOFF_RE = re.compile("|".join(SIGNOFF_PATTERNS), re.I)

# Hard markers the transcriber itself emits for non-speech audio.
HARD_JUNK_RE = re.compile(
    r"(\[\s*(music|applause|cheering|singing|instrumental|theme)\s*\]"
    r"|\(\s*(music|applause|cheering|singing|instrumental)\s*\)"
    r"|♪|♪|♫)", re.I)

# Broadcast / entertainment commentary. Every one of these appears in the
# 6/30 sports-audio sample or is standard sportscast/ad-read vocabulary.
BROADCAST_TERMS = {
    # play-by-play
    "world cup", "tournament", "the finalist", "halftime", "first half",
    "second half", "overtime", "penalty", "free kick", "corner kick", "offside",
    "touchdown", "quarterback", "home run", "innings", "rebound", "free throw",
    "knockout", "the match", "the tournament", "unchanged team", "back three",
    "defensive midfield", "midfield", "goalkeeper", "scored by",
    # tennis. NB: "love" and "ace" are deliberately absent — far too common in
    # ordinary speech ("I love that", "aced it") to use as junk evidence.
    "drop shot", "first serve", "second serve", "the volley", "deuce",
    "match point", "set point", "break point", "baseline", "out wide",
    "forehand", "backhand",
    # broadcast furniture
    "coming up after the break", "stay tuned", "brought to you by",
    "we'll be right back", "back to you", "in the studio", "our sponsor",
    "this broadcast", "live from", "commentary", "the commentator",
    "ladies and gentlemen", "subscribe", "like and subscribe",
}

# Business / standup vocabulary. Presence of these means it IS the meeting.
MEETING_TERMS = {
    "account", "accounts", "customer", "customers", "client", "deal", "deals",
    "pipeline", "churn", "renewal", "renewals", "kickoff", "onboarding",
    "demo", "call", "calls", "email", "emailed", "follow up", "followed up",
    "reach out", "outreach", "landing page", "dashboard", "notion", "hubspot",
    "slack", "crm", "usage", "techs", "technician", "technicians", "rep",
    # NB: bare "team" is deliberately absent — sports commentary is full of it
    # ("an unchanged team"), and it was the one term that made 6/30's broadcast
    # audio score as meeting content.
    "the team", "our team", "meeting", "standup", "sales", "marketing",
    "revenue", "quota",
    "invoice", "contract", "pricing", "proposal", "trial", "training",
    "action item", "priority", "priorities", "this week", "next week",
    "let me know", "circle back", "touch base", "servicetitan", "podium",
    "bluon", "blueon", "manager", "management", "leadership", "report",
}


def _norm(line):
    return re.sub(r"\s+", " ", (line or "")).strip().lower()


def _hits(text, terms):
    return sum(1 for t in terms if t in text)


def classify_line(line):
    """Return 'junk', 'meeting', or 'neutral' for a single transcript line."""
    t = _norm(line)
    if not t:
        return "neutral"
    if HARD_JUNK_RE.search(line or ""):
        return "junk"
    b = _hits(t, BROADCAST_TERMS)
    m = _hits(t, MEETING_TERMS)
    if b and b > m:
        return "junk"
    if m:
        return "meeting"
    return "neutral"


def _ratio(lines, kind):
    """Share of NON-neutral lines that are `kind`. Neutral lines don't vote.

    Backchannel ("Yeah.", "Right.", "Mm-hmm.") is neutral and shows up heavily
    in both real meetings and junk, so letting it vote would wash out the
    signal in both directions.
    """
    votes = [classify_line(l) for l in lines]
    decided = [v for v in votes if v != "neutral"]
    if not decided:
        return 0.0
    return sum(1 for v in decided if v == kind) / len(decided)


def looks_like_meeting(lines):
    """False when a transcript is essentially all non-meeting audio.

    This is the 2026-06-30 case: the mic caught a sports broadcast and there is
    no standup in there at all. Such a day should be skipped outright rather
    than mirrored into the team database.
    """
    if len(lines) < MIN_LINES_FOR_ALL_JUNK:
        return True  # too short to judge; let the normal guards decide
    return _ratio(lines, "junk") < ALL_JUNK_RATIO


def find_last_signoff(lines):
    """Index of the last line that reads like the meeting wrapping up."""
    for i in range(len(lines) - 1, -1, -1):
        if SIGNOFF_RE.search(lines[i] or ""):
            return i
    return None


def _trailing_run_start(lines):
    """Index where the maximal trailing run of non-meeting lines begins."""
    i = len(lines)
    while i > 0 and classify_line(lines[i - 1]) != "meeting":
        i -= 1
    return i


def clean_transcript(lines):
    """Trim obvious post-meeting audio off the tail.

    Returns (kept_lines, removed_lines, reason); reason is None when nothing was
    trimmed. The cut point is the last line with real meeting content — anything
    after it is a candidate.

    A sign-off is treated as corroboration, not a requirement. Requiring one
    was the first design and it failed on 7/31, whose transcript just stops
    mid-discussion with no goodbye recorded; junk appended to such a day would
    have sailed through untouched.

    Safety comes instead from three independent limits: the run must be at
    least MIN_TAIL_LINES long, must not exceed MAX_TRIM_RATIO of the
    transcript, and must contain at least MIN_JUNK_LINES positively-junk lines.
    That last one matters most — a normal meeting often trails off into
    backchannel ("Yeah." "Okay.") which is neutral, never junk, so it can never
    reach the floor on its own.
    """
    if not lines:
        return lines, [], None

    start = _trailing_run_start(lines)

    # The run starts at the last line carrying business vocabulary, but the
    # team's goodbye ("See ya", "let's have a good one") carries none and would
    # be swept up with the junk. If the run contains a sign-off, cut after it —
    # that keeps every real line and makes the removal count honest.
    signoff_in_run = None
    for k in range(len(lines) - 1, start - 1, -1):
        if SIGNOFF_RE.search(lines[k] or "") and classify_line(lines[k]) != "junk":
            signoff_in_run = k
            break
    if signoff_in_run is not None:
        start = signoff_in_run + 1

    tail = lines[start:]
    if len(tail) < MIN_TAIL_LINES:
        return lines, [], None
    if len(tail) / len(lines) > MAX_TRIM_RATIO:
        # Most of the transcript scoring as non-meeting means something odd is
        # going on. Leave it whole and let looks_like_meeting() judge the day.
        return lines, [], None

    junk_lines = sum(1 for l in tail if classify_line(l) == "junk")
    if junk_lines < MIN_JUNK_LINES:
        return lines, [], None

    density = junk_lines / len(tail)
    corroborated = signoff_in_run is not None or (
        start > 0 and bool(SIGNOFF_RE.search(lines[start - 1] or "")))
    floor = MIN_JUNK_DENSITY_SIGNOFF if corroborated else MIN_JUNK_DENSITY
    if density < floor:
        return lines, [], None

    return lines[:start], tail, (
        f"{len(tail)} trailing segment(s) removed — non-meeting audio picked up "
        f"after the call ended ({junk_lines} clearly off-topic"
        + (", after sign-off" if corroborated else "") + ")"
    )
