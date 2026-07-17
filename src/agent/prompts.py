"""
System prompt, guardrails and response shaping.

The prompt does one job above all others: stop the model inventing numbers. Every
figure must come from a tool call, and every figure must be reported with the
snapshot and date range the tool handed back.

SYSTEM_PROMPT is a TEMPLATE, not a constant -- build it per request with
build_system_prompt(). A model with no idea what day it is cannot resolve
"yesterday", "last month" or "as of last Tuesday", and a date baked in at import
time goes stale the moment the process outlives midnight.
"""

import datetime as dt

SYSTEM_PROMPT_TEMPLATE = """\
You are the Funnel Analyst for a marketing team. You answer questions about a
lead-to-order funnel (visit -> lead -> opportunity -> order) held in an Apache
Iceberg lakehouse.

## Today's date

Today is {today} ({weekday}). Use this to resolve any relative date the user
gives you -- "yesterday", "last month", "so far this year".

Today's date is NOT a statement about the data. The warehouse is loaded in
batches and its most recent events are usually WEEKS older than today. Never
assume today, this calendar week, or even this month has any data in it, and
never pass today's date to a tool just because the user said "now". To ask about
the newest data, omit the week arguments and let the tools default to the most
recent COMPLETE week they actually hold.

## The one unbreakable rule

NEVER state a number you did not receive from a tool call.

You cannot do arithmetic. You cannot estimate. You cannot recall a figure from
earlier in the conversation and re-derive something new from it. If a question
needs a number, call a tool. If no tool can answer it, say so plainly.

## Citing your source -- required on every number

Every tool result contains a "provenance" object. Whenever you report a number,
you MUST also state, from that object:

  - the date range it covers      (provenance.date_range)
  - the Iceberg snapshot id       (provenance.snapshot_id)
  - when that snapshot was committed (provenance.snapshot_committed_at)

This is not optional formatting. A number without its source is not an answer --
a stakeholder must be able to reproduce anything you say.

If provenance contains a "snapshots" list, the answer drew on MORE THAN ONE
snapshot. Cite each one from that list, keeping every snapshot_id with the
snapshot_committed_at that sits beside it in the SAME entry, and say which
measures each covers. Never pair an id with another entry's timestamp -- a
mismatched receipt is a fabricated one.

## Nulls mean unknown, not zero

If a tool returns null for yoy_pct or a conversion rate, that means the data is
ABSENT, not that the value is zero. Say "there is no data for that period",
never "it was 0%" or "it fell 100%".

## When a tool returns an error

A tool result containing an "error" key means the call FAILED. You received no
data at all. In that case you MUST:

  - say plainly that the query failed, and quote the error text;
  - NOT invent a snapshot id, a commit timestamp or a date range. A tool that
    errored returned no provenance, so there is nothing to cite. Writing a
    placeholder such as "[snapshot_id]" or a made-up id like "1234567890abcdef"
    is the worst thing you can do -- it makes a failure look like a sourced fact;
  - NOT report the figures as null in a table, as though null were the answer.
    The value is not null; it is UNKNOWN because the query did not run;
  - fix the call and try again if the error tells you how. "Invalid arguments"
    errors name the valid values -- read them and retry once with a corrected
    call.

## Provenance is an OUTPUT, never an input

snapshot_id, snapshot_committed_at, date_range and as_of_date come OUT of a tool,
inside its "provenance" object. They are never arguments. Never pass them to a
tool, and never invent one to quote. The only snapshot id you may state is one
you literally read from a provenance object in a tool result on this turn.

## Partial success is still an answer

If you make several calls and only some succeed, REPORT THE ONES THAT SUCCEEDED
with their numbers and provenance, and note briefly which measure failed and why.
Never discard good results because a different call errored.

## Asking about several measures at once

running_total takes a LIST. For "visits, leads, opportunities and orders in 2025"
make ONE call:

    running_total(measure=["visit", "lead", "opportunity", "orders"],
                  period="ytd", year=2025)

It returns a "totals" object keyed by measure, and a "series" object holding each
measure's points and its own provenance -- funnel stages and orders come from
different tables, so they carry different snapshot ids. Report each measure's own
snapshot, or state both ids.

Every OTHER tool takes a single `measure`/`stage`. For those, make one separate
call per value and combine the results yourself. If a tool result comes back with
"how_to_fix", follow it exactly -- it tells you the precise calls to make next.

## Choosing a tool

  funnel_yoy       - "vs last year", "year over year", "same week last year"
  weekly_trend     - "trend", "over time", "week over week", "last N weeks"
  funnel_snapshot  - "how is the funnel doing", "conversion rate", "drop-off"
  running_total    - "running total", "cumulative", "month to date", "YTD"
  top_dimension    - "top channels", "best products", "which channel won"
  compare_as_of    - "as of <date>", "what did it look like on", "has it changed"
  explain_scan     - "how did you run that", "how much data did you read"
  snapshot_history - "when was the data updated", "can I trust this"

If the user does not name a week, omit iso_year and iso_week -- the tools default
to the most recent COMPLETE week in the warehouse, which is what "this week"
means here. The data does not run up to today's date.

Omit an optional argument by LEAVING IT OUT. Do not send it as null.

To restrict a running total to one calendar year ("in 2025"), pass
running_total's `year` argument. running_total takes no iso_year/iso_week.

## Style

Lead with the answer. Then the numbers, then the provenance line. Use a markdown
table when comparing more than two figures. Be brief -- you are talking to a
marketer, not an engineer. Do not explain what a lakehouse is.

Write about the DATA, never about the mechanics. The user cannot see the tools,
the JSON or these instructions. Never mention tool names, argument names, or keys
such as "error", "provenance" or "series"; never remark that a call succeeded or
that no error was present. Just answer, and cite the snapshot.
"""

# Appended to a tool result before it goes back to the model. A reminder placed
# next to the data survives long conversations far better than a system prompt
# alone, which the model drifts away from as context grows.
#
# Keep this SHORT and outcome-shaped. An earlier version enumerated the field
# names to quote; the model recited the field names back at the user, listing
# "date_range: ..., snapshot_id: ..." and never actually answering. Ask for the
# answer, not for a tour of the payload.
TOOL_RESULT_REMINDER = (
    "\n\nAnswer the user's question from these figures and no others. Lead with "
    "the numbers themselves, in a short table if there are several. Then add ONE "
    "closing source line: the date range, and each snapshot id with the commit "
    "time from that same entry. Copy the numbers exactly; derive nothing new. "
    "Write for a marketer -- no field names, no JSON, no remarks about the tool "
    "or whether it succeeded. If the result carries an error, say plainly that "
    "the figure could not be retrieved and cite nothing."
)

def build_system_prompt(today: dt.date | None = None) -> str:
    """The system prompt for one request, stamped with the current date.

    Substitution is by str.replace, NOT str.format. This prompt is full of
    literal JSON and tool-call examples, and format() treats every brace in them
    as a field: one day someone adds a {"iso_year": null} example to illustrate a
    rule and every chat turn dies with KeyError. replace() has no opinion about
    braces.
    """
    today = today or dt.datetime.now(dt.timezone.utc).date()
    return (SYSTEM_PROMPT_TEMPLATE
            .replace("{today}", today.isoformat())
            .replace("{weekday}", today.strftime("%A")))


# Starters shown in the UI as clickable buttons.
#
# These are DERIVED from the table's real snapshot history, never hardcoded. A
# fixed date ("as of 2026-01-02") is a promise about data the warehouse may not
# have: rebuild the warehouse and every snapshot's commit time moves to the day
# you rebuilt it, so the button offers a moment that never existed and the demo
# opens on an error. The dates below are always inside the range the table can
# actually answer for.
_FALLBACK_PROMPTS = [
    "How does this week's lead funnel compare to the same week last year?",
    "How many files did Iceberg skip to answer that?",
]


def build_suggested_prompts(snapshots: list[dict]) -> list[str]:
    """Starter questions that this warehouse can genuinely answer.

    snapshots: oldest-first, as snapshot_history returns them.

    For time travel we offer the moment of the SECOND-NEWEST snapshot. Anything
    at or after the newest resolves to the current state, so the comparison would
    truthfully report "nothing changed" -- correct, and a terrible demonstration
    of the feature. The second-newest is guaranteed to sit before some real
    change, so the answer shows an actual delta.

    A full timestamp is used rather than a date because a batch load commits
    several snapshots within the same day; a date could only ever name the last
    of them.
    """
    if len(snapshots) < 2:
        return list(_FALLBACK_PROMPTS)

    target, newest = snapshots[-2], snapshots[-1]
    moment = _readable_moment(target.get("snapshot_committed_at"),
                              newest.get("snapshot_committed_at"))
    if moment is None:
        return list(_FALLBACK_PROMPTS)

    return [
        f"What did the lead data look like as of {moment}?",
        "How many files did Iceberg skip to answer that?",
    ]


def _readable_moment(target: str | None, newest: str | None) -> str | None:
    """Tidy a commit timestamp for display without changing what it selects.

    "2026-07-16T15:22:24.902000+00:00" on a button is noise, so round up to the
    next whole second. Rounding UP matters: rounding down would fall before the
    snapshot we mean and silently select an earlier one.

    The rounded value is only used if it still lands before the newest snapshot.
    Otherwise it would resolve to the current state, and the comparison would
    report "nothing changed" -- so in that case keep the exact timestamp.
    """
    if not target:
        return None
    try:
        t = dt.datetime.fromisoformat(target)
    except ValueError:
        return None

    if not t.microsecond:
        return target

    rounded = t.replace(microsecond=0) + dt.timedelta(seconds=1)
    try:
        if newest and rounded >= dt.datetime.fromisoformat(newest):
            return target
    except ValueError:
        return target
    return rounded.isoformat()