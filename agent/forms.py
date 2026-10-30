"""The forms (layer 1 of the office, instance/granskning/2026-10-20-omgang-1-grunden.md): what kinds of things the roles
can create, what each must contain, and who may write what.

Everything worth following over time is an item in one table (agent/items.py) with the same core: an id, a form, an
owner, a status, what it serves (parent), where it came from (source), when it should be looked at again and how
long it holds. Each form adds its own fields, checked here, so a plan can't become an idea by mistake and an
experiment can't run without a date to judge it. What the roles put *in* the forms is theirs (layer 2); a new form
is a new entry here, not a new table.

The ids carry the form's letter (#S5, #U31), so they read as what they are. They are the office's tools, never
shown to the owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

ROLES = ("assistant", "director", "researcher", "manager")
# The profile's sections, in order (agent/beliefs.py renders the profile from the current beliefs).
AREAS = ("who", "compensate", "social", "limits", "school", "interests", "opportunities", "not_interested", "interrupt",
         "schedule", "communication", "name", "other")
# The experience settings the engine follows (agent/assumptions.py has what each means).
SETTINGS = ("quiet_hours", "daily_contacts", "bundle_times", "message_length")
SOURCE_TYPES = ("owner", "observed", "inferred", "external", "assumed")   # assumed: a starting assumption of the system, never confirmed   # the owner said it · seen in what happened · a role's conclusion · a source outside


class FormError(ValueError):
    """What is wrong with an item, in words the role can act on."""


@dataclass(frozen=True)
class Field:
    name: str
    type: str = "text"   # text | date | datetime | int | url | bool | enum
    required: bool = False
    choices: tuple[str, ...] = ()
    help: str = ""


@dataclass(frozen=True)
class Form:
    name: str
    letter: str
    label: str           # Swedish, for the pages
    help: str
    statuses: dict[str, tuple[str, ...]]   # kind -> statuses, first is the default; "" is the only kind of a form without kinds
    closed: tuple[str, ...]                # statuses that mean it is over
    fields: tuple[Field, ...] = ()
    needs_review: bool = False             # must have a date to be looked at again (nothing runs forever)
    needs_source: bool = False             # must say where it came from

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(k for k in self.statuses if k)

    def statuses_of(self, kind: str | None) -> tuple[str, ...]:
        return self.statuses.get(kind or "", ())

    def field(self, name: str) -> Field | None:
        return next((f for f in self.fields if f.name == name), None)


AIM_OPEN = ("active", "paused", "interested", "preparing", "applied", "interviewing", "offer", "exploring", "shared")

FORMS: dict[str, Form] = {f.name: f for f in (
    Form(
        "aim", "S", "Spår",
        "What the owner goes for: a goal (what they want and why, what helps, what is noise, and its plan in steps), a "
        "project, an opportunity they showed interest in, applied to or turned down, or an idea Director is thinking through.",
        {
            "goal": ("active", "paused", "done", "dropped"),
            "project": ("active", "paused", "done", "dropped"),
            "pursuit": ("interested", "preparing", "applied", "interviewing", "offer", "accepted", "rejected", "declined", "missed"),
            "idea": ("exploring", "shared", "dismissed"),
        },
        closed=("done", "dropped", "accepted", "rejected", "declined", "missed", "dismissed"),
        fields=(
            Field("why", help="Swedish: why it matters to the owner"),
            Field("about", help="Swedish notes: a goal's what helps, what is noise and where it stands; an idea's content"),
            Field("next_step", help="Swedish, short"),
            Field("deadline", "date"),
            Field("remind_at", "date", help="an opportunity not yet applied to: when a nudge would help most"),
            Field("organization"),
            Field("url", "url"),
            Field("candidate_id", "int"),
            Field("nudged_at", "datetime"),
            Field("nudge_candidate_id", "int"),
            Field("opportunity", "int", help="the #O it is about (an opportunity): its link and dates come from there"),
        ),
    ),
    Form(
        "step", "T", "Steg",
        "One step of a plan: the plan of a goal, a project or an experiment is its steps. Each has an owner (the role "
        "that does it), when (a date, or after something), and what counts as done.",
        {"": ("planned", "doing", "waiting", "done", "dropped")},
        closed=("done", "dropped"),
        fields=(
            Field("action", "enum", True, ("ask_owner", "tell_owner", "research", "decide", "check", "other"),
                  "what kind of work: ask the owner, tell the owner, research, decide, check, other"),
            Field("done_when", required=True, help="Swedish: what counts as done"),
            Field("after", help="Swedish: a condition instead of (or as well as) a date"),
            Field("result", help="Swedish: what came of it"),
        ),
        needs_review=True,
    ),
    Form(
        "experiment", "E", "Experiment",
        "Something a role tries, to improve (make what is there better) or to explore (try something outside the current "
        "goals, to see if it opens something). It must end in a decision.",
        {"improve": ("planned", "running", "evaluating", "kept", "adjusted", "dropped"),
         "explore": ("planned", "running", "evaluating", "kept", "adjusted", "dropped")},
        closed=("kept", "adjusted", "dropped"),
        fields=(
            Field("area", help="what it is about: content, initiative, timing, amount, tone, format …"),
            Field("hypothesis", required=True, help="Swedish: what we expect"),
            Field("intervention", required=True, help="Swedish: what is done"),
            Field("measure", required=True, help="Swedish: what will show whether it worked"),
            Field("outcome", help="Swedish: what happened"),
            Field("decision", help="Swedish: keep, adjust or drop, and why"),
            Field("setting", "enum", False, SETTINGS, "set by the engine: the experience setting this tries (agent/assumptions.py)"),
            Field("test_value", help="set by the engine: the value tried"),
            Field("starts", help="set by the engine: when the trial began, YYYY-MM-DD HH:MM"),
        ),
        needs_review=True,
    ),
    Form(
        "belief", "U", "Uppfattning",
        "Something Audax holds about the owner: a preference, a fact, a pattern, a value or a hypothesis, with its source, "
        "how sure, and how long it holds. A new one can replace an older one; the old one stays in the history.",
        {k: ("current", "superseded", "retracted") for k in ("preference", "fact", "pattern", "value", "hypothesis")},
        closed=("superseded", "retracted"),
        fields=(
            Field("area", "enum", False, AREAS, "where it belongs in the profile: " + ", ".join(AREAS)),
            Field("subject", help="what it is about, a few words (\"bank\", \"utbyte\", \"meddelandelängd\")"),
            Field("confidence", "enum", False, ("low", "medium", "high")),
            Field("bubble", "enum", False, ("excludes", "maybe_limit", "never_considered"),
                  "excludes: the owner ruled it out · maybe_limit: may be uncertainty rather than what they want · "
                  "never_considered: nowhere in the picture of them"),
            Field("scope", help="Swedish: when it holds, if not always (\"under tentaperioden\")"),
            Field("setting", "enum", False, SETTINGS,
                  "a belief the engine follows directly (agent/assumptions.py): quiet_hours (HH:MM-HH:MM), daily_contacts "
                  "(a number), bundle_times (HH:MM,HH:MM or inga), message_length (characters)"),
            Field("value", help="the setting's value, e.g. 22:00-06:30"),
        ),
        needs_source=True,
    ),
    Form(
        "question", "Q", "Fråga",
        "Something a role doesn't know and that matters: what is unclear, what hangs on it, its best guess now, what would "
        "settle it, and where the answer comes from: the owner, another role, the world (Researcher finds out) or itself "
        "(an experiment, or a signal to wait for). The answer comes back to the one who asked, who says what it meant.",
        {k: ("open", "asked", "answered", "unanswered", "settled", "dropped") for k in ("clarify", "intent", "check", "followup", "challenge")},
        closed=("answered", "unanswered", "settled", "dropped"),
        fields=(
            Field("to", "enum", True, ("owner", *ROLES, "world", "self"),
                  help="who answers: owner (the Assistant asks), a role, world (a step for Researcher), self (an experiment or a signal)"),
            Field("why", required=True, help="Swedish: what hangs on it"),
            Field("guess", help="Swedish: your best guess now, and how sure"),
            Field("settles_it", help="Swedish: what would settle it"),
            Field("context", help="Swedish: what the asker knows that helps ask it well and follow up: background, what was said before, what to leave alone"),
            Field("answer", help="Swedish: the answer, in the owner's words where possible"),
            Field("conclusion", help="Swedish: what the answer meant, and what changed because of it"),
            Field("asked_at", "datetime"),
            Field("reminded_at", "datetime", help="when the Assistant brought it up once more, after it was forgotten in a conversation"),
        ),
    ),
    Form(
        "opportunity", "O", "Möjlighet",
        "One real thing out there that the owner could apply to or go to: a job posting, a programme, an event, a course, "
        "a competition. One item per real thing, with its exact link, so two postings with the same title for different "
        "places stay two, and copies of one posting on other sites are the same. Every fact says where it comes from.",
        {k: ("open", "upcoming", "closed", "gone") for k in ("job", "programme", "event", "course", "competition", "other")},
        closed=("closed", "gone"),
        fields=(
            Field("url", "url", True, help="the exact page of this thing: the posting for this place, not a search or a sibling posting"),
            Field("organization", required=True),
            Field("place", help="where, as the page says it (cities one can choose, online)"),
            Field("deadline", "date", help="last day to apply"),
            Field("date", "date", help="when it happens (an event)"),
            Field("who", help="Swedish: who may apply, as the page says it"),
            Field("checked", "enum", False, ("page", "other_sites", "search_results"),
                  help="where the facts come from: the page at url itself, copies elsewhere, or only search results"),
            Field("unconfirmed", help="Swedish: what is not confirmed by the page itself"),
            Field("also_at", help="other addresses that lead to this same thing, one per line"),
            Field("differs_from", help="Swedish: similar things it is NOT, and how it differs (e.g. the London posting #O12)"),
        ),
    ),
    Form(
        "rule", "G", "Regel",
        "Something the code does without Claude, made by a role: a filter for a source, a check before something is sent.",
        {"": ("active", "paused", "retired")},
        closed=("retired",),
        fields=(
            Field("applies_to", required=True, help="a source id, a kind of finding, or all"),
            Field("condition", required=True, help="when it applies"),
            Field("action", "enum", True, ("drop", "hold", "tag", "check")),
            Field("reason", required=True, help="Swedish: why"),
            Field("hits", "int"),
        ),
    ),
    Form(
        "routine", "N", "Rutin",
        "Work that recurs: watching a source, a search, a check-in. It serves something (its parent) or was asked for by "
        "the owner, and ends when that is done.",
        {"": ("active", "paused", "ended")},
        closed=("ended",),
        fields=(
            Field("what", required=True, help="Swedish: what is done"),
            Field("cadence", help="how often, in words or as an interval"),
            Field("ends_when", help="Swedish"),
            Field("config", help="JSON for the code that runs it"),
            Field("health", help="what the last runs gave, or failed with"),
        ),
    ),
)}

LETTERS = {f.letter: f.name for f in FORMS.values()}
CORE = ("id", "form", "kind", "title", "status", "owner", "parent_id", "source", "review_at", "valid_until",
        "by", "run_id", "created_at", "updated_at")

# Who may create which forms. Every role may update and close what it owns; the exceptions below are the owner's
# words (the Assistant records them wherever they land) and Director's oversight of goals and beliefs.
CREATE = {
    "assistant": {"aim", "belief", "question", "step"},
    "director": {"aim", "belief", "question", "step", "experiment", "rule"},
    "researcher": {"step", "question", "rule", "routine", "opportunity", "experiment"},
    "manager": {"belief", "question", "step", "experiment", "rule", "routine"},
}
UPDATE_ANY = {
    "assistant": {"aim", "belief", "question", "step"},
    "director": {"aim", "belief"},
}


def may(role: str, action: str, form: str, owner: str | None = None) -> bool:
    if role in ("system", "owner"):
        return True
    if action == "create":
        return form in CREATE.get(role, set())
    return owner == role or form in UPDATE_ANY.get(role, set())


def check(form_name: str, kind: str | None, status: str | None, data: dict, *, title: str | None, source: dict | None,
          review_at: str | None, creating: bool) -> tuple[str, str | None, str, dict]:
    """Checks an item against its form. Returns (form, kind, status, data) cleaned, or raises FormError with every
    problem at once."""
    form = FORMS.get(form_name)
    if form is None:
        raise FormError(f"No form called {form_name!r}. Forms: {', '.join(FORMS)}.")
    problems = []
    if form.kinds:
        if kind not in form.kinds:
            problems.append(f"kind must be one of {', '.join(form.kinds)}")
    else:
        kind = None
    allowed = form.statuses_of(kind)
    if status is None and creating:
        status = allowed[0] if allowed else None
    if status is not None and allowed and status not in allowed:
        problems.append(f"status must be one of {', '.join(allowed)}")
    if creating and not (title or "").strip():
        problems.append("a title is needed")
    clean = {}
    for name, value in data.items():
        f = form.field(name)
        if f is None:
            problems.append(f"{form.name} has no field {name!r} (fields: {', '.join(x.name for x in form.fields)})")
            continue
        if value in (None, ""):
            clean[name] = None
            continue
        try:
            clean[name] = _value(f, value)
        except ValueError as e:
            problems.append(f"{name}: {e}")
    if creating:
        for f in form.fields:
            if f.required and clean.get(f.name) in (None, ""):
                problems.append(f"{f.name} is required ({f.help or f.type})")
        if form.needs_review and not review_at:
            problems.append("review_at is required: the date it is looked at again, so nothing runs forever")
        if form.needs_source and not source:
            problems.append("a source is required: who said it or how it is known")
    if source is not None:
        if not isinstance(source, dict) or source.get("type") not in SOURCE_TYPES:
            problems.append(f"source.type must be one of {', '.join(SOURCE_TYPES)}")
    if problems:
        raise FormError(f"{form.label} ({form.name}): " + "; ".join(problems) + ".")
    return form.name, kind, status, clean


def _value(f: Field, value):
    if f.type == "int":
        return int(value)
    if f.type == "bool":
        return bool(value)
    if f.type == "date":
        return date.fromisoformat(str(value)[:10]).isoformat()
    if f.type == "datetime":
        return datetime.fromisoformat(str(value)).isoformat(timespec="minutes")
    if f.type == "enum":
        if value not in f.choices:
            raise ValueError(f"must be one of {', '.join(f.choices)}")
        return value
    if f.type == "url":
        if not str(value).startswith(("http://", "https://")):
            raise ValueError("must be a web address")
        return str(value)
    return str(value).strip()
