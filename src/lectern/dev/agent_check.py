"""`lectern dev agent-check`: send a prompt set to a running server and score the answers.

Each prompt goes to a fresh chat in a new project (with the set's files imported), so no
answer sees another. A prompt passes when the chat used the tools it must (for the
calculation set: code.run) and the answer contains the expected numbers or words.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from lectern.agent.numbers import numbers_in
from lectern.dev import pdf_files

PROMPTS_DIR = Path(__file__).parent / "prompts"
TURN_TIMEOUT = 600.0
TOLERANCE = 0.005
REQUIRED_TOOLS = {"calc": ["code.run"]}


@dataclass(frozen=True)
class Case:
    id: str
    prompt: str
    numbers: tuple[float, ...] = ()
    text: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()  # must be called (any order)
    forbid: tuple[str, ...] = ()  # must not be called
    args: Mapping[str, Mapping[str, str]] = field(default_factory=dict)  # tool → what it's given
    # The PDF set: text a PDF saved in the turn must have ("a|b" passes on either), form
    # values it must hold, or that the turn saved no PDF at all.
    pdf_text: tuple[str, ...] = ()
    pdf_form: Mapping[str, str] = field(default_factory=dict)
    pdf_none: bool = False


@dataclass(frozen=True)
class Conversation:
    """One chat: `turns` short prompts cycled from `filler`, then `final`, scored."""

    filler: tuple[str, ...]
    turns: int
    final: Case
    forbid: tuple[str, ...] = ()  # must not be called on any filler turn


@dataclass(frozen=True)
class PromptSet:
    name: str
    cases: list[Case]
    files: list[Path]
    conversation: Conversation | None = None


@dataclass
class Outcome:
    case: Case
    tools: list[str] = field(default_factory=list)  # calls that worked
    attempted: list[str] = field(default_factory=list)  # every call, worked or not
    arguments: list[tuple[str, dict]] = field(default_factory=list)  # every call's, refused too
    answer: str = ""
    seconds: float = 0.0
    error: str | None = None
    required: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    compactions: int = 0
    checks: dict[str, bool] = field(default_factory=dict)  # extra checks a run adds

    @property
    def used_tools(self) -> bool:
        # A required entry may name alternatives: "project.find|project.list" passes on either.
        return (
            all(
                any(option in self.tools for option in needed.split("|"))
                for needed in self.required
            )
            and not any(t in (self.attempted or self.tools) for t in self.forbidden)
            and self.args_given
        )

    @property
    def args_given(self) -> bool:
        """Every call a case asks for was made with what it asks for. A refused card still
        counts: the call is what's being judged, and a set can run without starting the work."""
        return all(
            any(
                name == tool and all(_same(value, given.get(key)) for key, value in wanted.items())
                for name, given in self.arguments
            )
            for tool, wanted in self.case.args.items()
        )

    @property
    def right(self) -> bool:
        return self.error is None and answer_matches(self.case, self.answer)

    @property
    def passed(self) -> bool:
        return self.used_tools and self.right and all(self.checks.values())


def _spelled(value: str) -> str:
    return value.lower().lstrip(".")


def _same(wanted: str, given: object) -> bool:
    """Spelled the way the tool takes it: ".DOCX" exports to docx too."""
    return isinstance(given, str) and _spelled(given) == wanted


def load(name: str) -> PromptSet:
    path = PROMPTS_DIR / f"{name}.yaml"
    if not path.is_file():
        known = ", ".join(sorted(p.stem for p in PROMPTS_DIR.glob("*.yaml")))
        raise ValueError(f"No prompt set {name!r}. Sets: {known}.")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [_case(item) for item in data.get("prompts", [])]
    conversation = None
    if "conversation" in data:
        spec = data["conversation"]
        conversation = Conversation(
            filler=tuple(" ".join(f.split()) for f in spec["filler"]),
            turns=int(spec["turns"]),
            final=_case(spec["final"]),
            forbid=tuple(spec.get("forbid", [])),
        )
    files = [PROMPTS_DIR / "files" / f for f in data.get("files", [])]
    return PromptSet(name, cases, files, conversation)


def load_set(name: str) -> tuple[list[Case], list[Path]]:
    prompt_set = load(name)
    return prompt_set.cases, prompt_set.files


def _case(item: dict) -> Case:
    return Case(
        id=item["id"],
        prompt=" ".join(item["prompt"].split()),
        numbers=tuple(float(n) for n in item.get("numbers", [])),
        text=tuple(str(t) for t in item.get("text", [])),
        tools=tuple(item.get("tools", [])),
        forbid=tuple(item.get("forbid", [])),
        args={
            tool: {key: _spelled(str(value)) for key, value in wanted.items()}
            for tool, wanted in item.get("args", {}).items()
        },
        pdf_text=tuple(str(t) for t in item.get("pdf_text", [])),
        pdf_form={str(k): str(v) for k, v in item.get("pdf_form", {}).items()},
        pdf_none=bool(item.get("pdf_none", False)),
    )


# --- files a set needs that are made at run time --------------------------------------------

# The lease the tools and long sets read. Made here, not committed as a binary: every clause
# is visible, and the text a test quotes is this text. All names are made up.
LEASE_TITLE = "Договір оренди нежитлового приміщення"
LEASE_CLAUSES = [
    ("1. Сторони", "Орендодавець: ТОВ «Приклад-Сервіс». Орендар: Ковальчук Олена Петрівна."),
    ("2. Предмет договору", "Приміщення площею 42 кв. м за адресою: м. Київ, вул. Зразкова, 7."),
    ("3. Орендна плата", "3.2. Орендна плата становить 250,00 грн на місяць і сплачується до "
     "10 числа поточного місяця.\n3.3. За кожен день прострочення платежу Орендар сплачує пеню "
     "у розмірі 0,5% від суми заборгованості, включно з днем оплати."),
    ("5. Строк дії", "Договір діє до 31 грудня 1999 року."),
]  # fmt: skip
CLAUSE_3_3 = (
    "За кожен день прострочення платежу Орендар сплачує пеню у розмірі 0,5% від суми "
    "заборгованості, включно з днем оплати."
)


def materialise(files: list[Path], into: Path) -> list[Path]:
    """The set's files: from prompts/files/, or made here (lease-1998.docx, scan-lease.png,
    and the pdf set's agreement.pdf and form.pdf, in lectern.dev.pdf_files)."""
    ready = []
    for path in files:
        if path.is_file():
            ready.append(path)
        elif path.name == "lease-1998.docx":
            ready.append(_write_lease(into / path.name))
        elif path.name == "scan-lease.png":
            ready.append(_write_scan(into / path.name))
        elif path.name == "agreement.pdf":
            ready.append(pdf_files.write_agreement(into / path.name))
        elif path.name == "form.pdf":
            ready.append(pdf_files.write_form(into / path.name))
        else:
            raise ValueError(f"The prompt set needs {path.name}, which isn't there.")
    return ready


def _write_scan(path: Path) -> Path:
    """A page image with no text layer. The OCR set only asks for it to be read: what counts is
    how the tool is called, so its card is refused and the page is never read."""
    from PIL import Image, ImageDraw

    page = Image.new("L", (800, 1100), "white")
    ImageDraw.Draw(page).text((80, 80), "Lease 17/2026", fill="black")
    page.save(path)
    return path


def _write_lease(path: Path) -> Path:
    import docx

    document = docx.Document()
    document.add_heading(LEASE_TITLE, level=1)
    document.add_paragraph("м. Київ, 12 травня 1998 року.")
    for heading, body in LEASE_CLAUSES:
        document.add_heading(heading, level=2)
        for paragraph in body.split("\n"):
            document.add_paragraph(paragraph)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))
    return path


# --- scoring ---------------------------------------------------------------------------------

# Small counts are often written out ("Three payments were made…"): read as their numbers by
# the scoring only, so numbers_in stays what a number in digits is.
NUMBER_WORDS = {word: value for value, word in enumerate([
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen", "twenty",
])}  # fmt: skip


def answer_matches(case: Case, answer: str) -> bool:
    words = re.findall(r"[^\W\d_]+", answer.casefold())
    found = numbers_in(answer) + [float(NUMBER_WORDS[w]) for w in words if w in NUMBER_WORDS]
    for expected in case.numbers:
        if not any(abs(value - expected) <= TOLERANCE for value in found):
            return False
    lowered = answer.casefold()
    return all(word.casefold() in lowered for word in case.text)


# --- running ---------------------------------------------------------------------------------


def run_set(client, name: str, *, allow_cards: bool, only: list[str] | None = None,
            echo=print) -> list[Outcome]:  # noqa: ANN001  # fmt: skip
    """Run the set against a running server through `client` (an httpx2.Client with the
    token and Origin headers). Cards are answered Allow when `allow_cards`, else Don't allow."""
    prompt_set = load(name)
    cases = [c for c in prompt_set.cases if not only or c.id in only]
    project = client.post("/api/projects", json={"name": f"agent-check {name}"}).json()
    pid = project["id"]
    with tempfile.TemporaryDirectory() as made:
        for path in materialise(prompt_set.files, Path(made)):
            response = client.put(
                f"/api/projects/{pid}/files", params={"name": path.name}, content=path.read_bytes()
            )
            response.raise_for_status()
    outcomes = []
    for case in cases:
        chat = client.post(f"/api/projects/{pid}/sessions", json={"title": case.id}).json()
        pdfs_before = _pdfs(client, pid)
        outcome = _ask(client, pid, chat["id"], case, allow_cards=allow_cards)
        if case.pdf_text or case.pdf_form or case.pdf_none:
            outcome.checks.update(_check_pdfs(client, pid, case, pdfs_before))
        outcome.required = case.tools or tuple(REQUIRED_TOOLS.get(name, ()))
        outcome.forbidden = case.forbid
        outcomes.append(outcome)
        echo(_line(outcome))
    if prompt_set.conversation and (not only or prompt_set.conversation.final.id in only):
        outcome = run_conversation(
            client, pid, prompt_set.conversation, allow_cards=allow_cards, echo=echo
        )
        outcomes.append(outcome)
        echo(_line(outcome))
    return outcomes


def run_conversation(client, pid: int, conversation: Conversation, *, allow_cards: bool,
                     echo=print) -> Outcome:  # noqa: ANN001  # fmt: skip
    """`turns` filler prompts in one chat, then the final prompt. Passes when the final answer
    is right, compaction ran at least once, the transcript still has every message, and no
    filler turn called a forbidden tool."""
    chat = client.post(
        f"/api/projects/{pid}/sessions", json={"title": conversation.final.id}
    ).json()
    compactions = 0
    forbidden_turns = 0
    started = time.monotonic()
    for i in range(conversation.turns):
        filler = Case(f"turn-{i + 1}", conversation.filler[i % len(conversation.filler)])
        step = _ask(client, pid, chat["id"], filler, allow_cards=allow_cards)
        compactions += step.compactions
        called = [t for t in step.attempted if t in conversation.forbid]
        if called:
            forbidden_turns += 1
            echo(f"  turn {i + 1}: called {', '.join(called)}")
        if step.error:
            echo(f"  turn {i + 1}: error: {step.error}")
        if (i + 1) % 10 == 0:
            echo(f"  {i + 1}/{conversation.turns} turns · compactions so far: {compactions}")
    outcome = _ask(client, pid, chat["id"], conversation.final, allow_cards=allow_cards)
    outcome.compactions += compactions
    outcome.required = conversation.final.tools
    outcome.forbidden = conversation.final.forbid
    outcome.seconds = round(time.monotonic() - started, 1)
    transcript = client.get(f"/api/projects/{pid}/sessions/{chat['id']}").json()
    asked = sum(1 for m in transcript["messages"] if m["role"] == "user")
    outcome.checks = {
        "compacted": outcome.compactions > 0,
        "transcript kept": asked == conversation.turns + 1,
    }
    if conversation.forbid:
        outcome.checks["no forbidden tools in filler"] = forbidden_turns == 0
    return outcome


def _ask(client, pid: int, session_id: int, case: Case, *, allow_cards: bool) -> Outcome:  # noqa: ANN001
    """Send one message to a chat and follow the turn to its end."""
    base = f"/api/projects/{pid}/sessions/{session_id}"
    outcome = Outcome(case)
    calls: dict[str, str] = {}  # call id → tool, until its result arrives
    started = time.monotonic()
    sent = client.post(f"{base}/messages", json={"text": case.prompt})
    if sent.status_code != 202:
        outcome.error = f"send failed: {sent.status_code} {sent.text[:200]}"
        return outcome
    # Sending clears the chat's event buffer before the turn starts, so everything the stream
    # replays belongs to this turn, even if it finished before the stream connected.
    with client.stream("GET", f"{base}/events", timeout=TURN_TIMEOUT) as stream:
        for line in stream.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line.removeprefix("data: "))
            kind = event.get("type")
            if kind == "tool":
                outcome.attempted.append(event["name"])
                outcome.arguments.append((event["name"], _arguments(event.get("arguments", ""))))
                calls[event["call_id"]] = event["name"]
            elif kind == "tool_result":
                # Only a call that worked counts as using the tool; a refused or failed one
                # still counts as trying it (a forbidden tool fails the prompt either way).
                name = calls.pop(event.get("call_id"), None)
                if name and not event.get("error"):
                    outcome.tools.append(name)
            elif kind == "delta":
                outcome.answer += event["text"]
            elif kind == "assistant" and event.get("tool_calls"):
                outcome.answer = ""  # text before a tool call isn't the answer
            elif kind == "confirm":
                client.post(
                    f"{base}/confirmations/{event['confirmation_id']}",
                    json={"approve": allow_cards},
                )
            elif kind == "compacted":
                outcome.compactions += 1
            elif kind == "error":
                outcome.error = event["detail"]
            elif kind == "turn" and event.get("status") != "running":
                break
            if time.monotonic() - started > TURN_TIMEOUT:
                outcome.error = "timed out"
                client.post(f"{base}/stop")
                break
    outcome.seconds = round(time.monotonic() - started, 1)
    return outcome


def _pdfs(client, pid: int) -> dict[int, str]:  # noqa: ANN001
    items = client.get(f"/api/projects/{pid}/items").json()
    return {a["id"]: a["name"] for a in items["artifacts"] if a["name"].lower().endswith(".pdf")}


def _check_pdfs(client, pid: int, case: Case, before: dict[int, str]) -> dict[str, bool]:  # noqa: ANN001
    """The PDFs this turn saved, against what the case expects of them."""
    new = {i: n for i, n in _pdfs(client, pid).items() if i not in before}
    if case.pdf_none:
        return {"no PDF saved": not new}
    if not new:
        return {"a PDF saved": False}
    checks: dict[str, bool] = {}
    with tempfile.TemporaryDirectory() as folder:
        texts, forms = [], []
        for artifact_id in new:
            response = client.get(f"/api/projects/{pid}/artifacts/{artifact_id}/content")
            path = Path(folder) / f"{artifact_id}.pdf"
            path.write_bytes(response.content)
            texts.append(pdf_files.pdf_text(path))
            forms.append(pdf_files.form_values(path))
    for wanted in case.pdf_text:
        checks[f"PDF has {wanted!r}"] = any(
            option in text for text in texts for option in wanted.split("|")
        )
    for name, value in case.pdf_form.items():
        checks[f"form {name}={value!r}"] = any(form.get(name) == value for form in forms)
    return checks


def _arguments(text: str) -> dict:
    """A call's arguments, as far as the model wrote them properly."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _line(o: Outcome) -> str:
    mark = "PASS" if o.passed else "FAIL"
    failed = [name for name, ok in o.checks.items() if not ok]
    if o.forbidden and any(t in (o.attempted or o.tools) for t in o.forbidden):
        failed.append("called a forbidden tool")
    if not o.args_given:
        failed.append("wrong tool arguments")
    tools = ",".join(t.split(".")[-1] if t.startswith("code.") else t for t in o.tools) or "-"
    failed_calls = len(o.attempted) - len(o.tools)
    if failed_calls > 0:
        tools += f" (+{failed_calls} failed)"
    answer = " ".join(o.answer.split())[:90]
    note = f" · error: {o.error}" if o.error else ""
    if failed:
        note += " · failed: " + ", ".join(failed)
    if o.compactions:
        note += f" · compactions: {o.compactions}"
    return f"{mark} {o.case.id:<13} {o.seconds:>6.1f}s  tools: {tools:<28} {answer}{note}"


def summary(outcomes: list[Outcome]) -> str:
    total = len(outcomes)
    used = sum(o.used_tools for o in outcomes)
    right = sum(o.right for o in outcomes)
    passed = sum(o.passed for o in outcomes)
    requirements = {o.required for o in outcomes}
    if len(requirements) == 1:
        required = ", ".join(next(iter(requirements))) or "none"
        tools = f"required tools ({required}) used in {used}/{total}"
    else:
        tools = f"right tools in {used}/{total}"
    return f"{passed}/{total} passed · {tools} · right answer in {right}/{total}"
