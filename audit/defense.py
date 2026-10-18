"""Trajectory-aware defense for the BIRD-Interact user simulator (Result 3).

The stock simulator is stateless: every `ask` is labeled and answered on its own, so a
compound question (an answerable definition clause carrying an unanswerable slot clause)
can pull unannotated gold-SQL structure through, and a sequence of such asks can
reconstruct the query. This wrapper adds the memory the simulator lacks:

  ComponentMap      built once per task from the gold SQL (sqlglot) and the task's
                    annotation snippets. Each SQL part is `allowed` if an annotation
                    covers it, else `privileged`.
  Ledger            one per episode. Remembers which parts have gone out and how many
                    of them were privileged, against a per-episode cap.
  DefendedSimulator wraps the benchmark's own encoder and decoder (passed in as
                    callables, never edited). On each ask: split the question into
                    clauses, label each clause on its own, let the stock decoder draft
                    a reply for the answerable clauses, audit which parts the draft
                    reveals, admit allowed parts and privileged parts up to the cap,
                    and cut only what is over the cap. The rest is sent.

Nothing here touches the dataset, the grader, the budget or the benchmark's code. The
agent never sees the map or the ledger.

Wiring for the benchmark (used by sim_turns.py --defense):

    sim = DefendedSimulator.from_bird_interact(record, status, db_schema, model, call, cap=0)
    row = sim.ask(question)       # row["reply"] is what the agent gets

Offline self-test (no benchmark, no LLM):

    python audit/defense.py --selftest
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Callable, Iterable, Optional

import sqlglot
from sqlglot import exp

DIALECT = "postgres"
UNANSWERABLE = "UNA"          # encoder label for a clause the simulator must not answer
NO_ANSWER_REPLY = "I can't go into that level of detail."

# ----------------------------------------------------------------------------- text utils

_WS = re.compile(r"\s+")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LITERAL = re.compile(r"'([^']*)'|(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")

SQL_NOISE = {
    "select", "from", "where", "and", "or", "not", "as", "on", "join", "inner", "left", "right",
    "full", "outer", "group", "by", "order", "having", "limit", "offset", "asc", "desc", "nulls",
    "first", "last", "with", "distinct", "case", "when", "then", "else", "end", "is", "null",
    "in", "between", "like", "ilike", "cast", "numeric", "decimal", "float", "double", "int",
    "integer", "text", "varchar", "true", "false", "coalesce", "over", "partition",
}


def norm_sql(s: str) -> str:
    s = (s or "").lower().replace('"', "").replace("`", "")
    s = re.sub(r"::\w+", "", s)                 # drop casts: x::numeric -> x
    s = re.sub(r"\bnulls\s+(first|last)\b", "", s)
    return _WS.sub(" ", re.sub(r"\s*([(),=<>+\-*/])\s*", r"\1", s)).strip()


FUNC_WORDS = {   # how a reply usually says a SQL function
    "avg": ("average", "mean", "avg"), "sum": ("total", "sum"), "count": ("count", "number of", "how many"),
    "max": ("maximum", "highest", "largest", "max"), "min": ("minimum", "lowest", "smallest", "min"),
    "round": ("round",), "extract": ("extract", "year", "month"), "date_part": ("year", "month", "day"),
}


def key_tokens(s: str, drop: Iterable[str] = ()) -> set[str]:
    """Identifiers and literals that make a SQL fragment distinctive."""
    s = (s or "").replace('"', "")
    toks = {t.lower() for t in _IDENT.findall(s)} - SQL_NOISE - {d.lower() for d in drop}
    toks |= {m[0].lower() or m[1] for m in _LITERAL.findall(s)}
    # JSON field paths like Cloud_Metrics->>'Noise_Db' also appear as $.Noise_Db
    toks |= {m.lower() for m in re.findall(r"\$\.(\w+)", s)}
    return {t for t in toks if t}


def _flatten_conj(e: Optional[exp.Expression]) -> list[exp.Expression]:
    if e is None:
        return []
    return list(e.flatten()) if isinstance(e, exp.And) else [e]


def _sql(e: exp.Expression) -> str:
    return e.sql(dialect=DIALECT)


def _strip(e: exp.Expression, rounding: bool = False) -> exp.Expression:
    """Comparable form: casts and aliases removed (and ROUND wrappers, which get their own part)."""
    def f(n):
        if isinstance(n, (exp.Cast, exp.TryCast, exp.Alias)):
            return n.this
        if rounding and isinstance(n, exp.Round):
            return n.this
        return n
    # transform does not descend into a replacement node, so iterate to a fixpoint
    out, prev = e.copy(), None
    for _ in range(8):
        out = out.transform(f)
        cur = out.sql(dialect=DIALECT)
        if cur == prev:
            break
        prev = cur
    return out


def comparable_sql(text: str, rounding: bool = False) -> str:
    """Render SQL text (or a fragment) in the same normalized form as components, so
    annotation snippets and gold parts can be matched by text."""
    try:
        return norm_sql(_sql(_strip(sqlglot.parse_one(text, read=DIALECT), rounding)))
    except Exception:
        return norm_sql(text)


# ----------------------------------------------------------------------------- component map

@dataclass
class Component:
    id: str
    kind: str          # select_column | table | join | filter | group_by | having | order_by | limit | rounding | distinct | cte
    text: str          # SQL text of the part (postgres rendering)
    keys: set[str]     # distinctive tokens, for the heuristic audit
    allowed: bool      # covered by an annotation snippet
    scope: str = ""    # "main" or the CTE / subquery it lives in

    def brief(self) -> str:
        return f"{self.id} [{self.kind}{', ' + self.scope if self.scope and self.scope != 'main' else ''}] {self.text}"


class ComponentMap:
    """Splits the gold SQL into parts and marks each allowed/privileged. Read-only after build."""

    ANNOTATION_LISTS = (
        ("user_query_ambiguity", "critical_ambiguity"),
        ("user_query_ambiguity", "non_critical_ambiguity"),
        ("knowledge_ambiguity", None),
    )

    def __init__(self, record: dict, annotation_snippets: Optional[Iterable[str]] = None, dialect: str = DIALECT):
        self.record = record
        self.dialect = dialect
        self.gold_sql = self._gold_sql(record)
        self.snippets = list(annotation_snippets) if annotation_snippets is not None else self.annotation_snippets(record)
        self.components: list[Component] = self._extract(self.gold_sql)
        self._mark_allowed()
        self.by_id = {c.id: c for c in self.components}

    # ---- inputs
    @staticmethod
    def _gold_sql(record: dict) -> str:
        sql = record.get("sol_sql") or record.get("gold_sql") or record.get("SQL") or ""
        if isinstance(sql, list):
            sql = sql[0] if sql else ""
        if not sql:
            raise ValueError("record has no sol_sql (merge GT first)")
        return sql

    @classmethod
    def annotation_snippets(cls, record: dict) -> list[str]:
        """Every sql_snippet the simulator is allowed to speak from (ambiguity annotations)."""
        out = []
        for top, sub in cls.ANNOTATION_LISTS:
            node = record.get(top)
            if node is None:
                continue
            items = node.get(sub, []) if sub else node
            if isinstance(items, dict):
                items = [items]
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                for k in ("sql_snippet", "snippet", "sql"):
                    v = it.get(k)
                    if isinstance(v, str) and v.strip():
                        out.append(v)
                    elif isinstance(v, list):
                        out.extend(x for x in v if isinstance(x, str) and x.strip())
        return out

    # ---- extraction
    def _extract(self, sql: str) -> list[Component]:
        comps: list[Component] = []
        seen: set[tuple[str, str]] = set()
        aliases: set[str] = set()

        def add(kind: str, text: str, scope: str):
            text = text.strip()
            key = (kind, norm_sql(text))
            if not text or key in seen:
                return
            seen.add(key)
            comps.append(Component(f"C{len(comps) + 1}", kind, text, key_tokens(text, aliases), False, scope))

        try:
            tree = sqlglot.parse_one(sql, read=self.dialect)
            aliases = {a.name.lower() for a in tree.find_all(exp.TableAlias) if a.name}
            self.aliases = aliases
        except Exception:
            # unparsable gold: fall back to a crude clause split so the defense still has something to audit
            for kind, pat in (("filter", r"\bwhere\b(.*?)(?=\bgroup by\b|\border by\b|\blimit\b|$)"),
                              ("order_by", r"\border by\b(.*?)(?=\blimit\b|$)"),
                              ("limit", r"\blimit\b\s*(\d+)")):
                for m in re.finditer(pat, sql, re.I | re.S):
                    add(kind, m.group(0), "main")
            return comps

        cte_scopes = {id(cte.this): cte.alias for cte in tree.find_all(exp.CTE)}

        def scope_of(sel: exp.Expression) -> str:
            node = sel
            while node is not None:
                if id(node) in cte_scopes:
                    return f"cte {cte_scopes[id(node)]}"
                if isinstance(node, exp.Subquery) and node is not sel:
                    return f"subquery {node.alias or ''}".strip()
                node = node.parent
            return "main"

        for cte in tree.find_all(exp.CTE):
            add("cte", f"WITH {cte.alias} AS (...)", "main")

        for sel in tree.find_all(exp.Select):
            scope = scope_of(sel)
            if sel.args.get("distinct"):
                add("distinct", "SELECT DISTINCT", scope)
            for e in sel.expressions:
                add("select_column", _sql(_strip(e, rounding=True)), scope)
            frm = sel.args.get("from_") or sel.args.get("from")
            if frm is not None:
                for t in frm.find_all(exp.Table):
                    add("table", _sql(t), scope)
            for j in sel.args.get("joins") or []:
                for t in j.find_all(exp.Table):
                    add("table", _sql(t), scope)
                if j.args.get("on") is not None:
                    add("join", f"JOIN ... ON {_sql(_strip(j.args['on']))}", scope)
            where = sel.args.get("where")
            if where is not None:
                for c in _flatten_conj(where.this):
                    add("filter", _sql(_strip(c)), scope)
            group = sel.args.get("group")
            if group is not None:
                add("group_by", _sql(group), scope)
            having = sel.args.get("having")
            if having is not None:
                for c in _flatten_conj(having.this):
                    add("having", f"HAVING {_sql(_strip(c))}", scope)
            order = sel.args.get("order")
            if order is not None:
                for o in order.expressions:
                    add("order_by", f"ORDER BY {_sql(o)}", scope)
            limit = sel.args.get("limit")
            if limit is not None:
                add("limit", _sql(limit), scope)
        for r in tree.find_all(exp.Round):
            add("rounding", _sql(r), "main")
        return comps

    # ---- allowed / privileged
    def _mark_allowed(self):
        snips = []
        for raw in self.snippets:
            forms = {norm_sql(raw), comparable_sql(raw), comparable_sql(raw, rounding=True)}
            snips.append((forms - {""}, key_tokens(raw)))
        for c in self.components:
            cn = norm_sql(c.text)
            core = cn.split(" ", 1)[1] if c.kind in ("order_by", "having") and " " in cn else cn
            core = re.sub(r"^join \.\.\. on ", "", core)
            for forms, sk in snips:
                if any(core in f or (f in core and len(f) >= 6) for f in forms):
                    c.allowed = True
                    break
                # structural parts render differently than the raw snippet; fall back to token cover
                if c.keys and c.keys <= sk:
                    c.allowed = True
                    break

    # ---- views
    def allowed_ids(self) -> set[str]:
        return {c.id for c in self.components if c.allowed}

    def privileged_ids(self) -> set[str]:
        return {c.id for c in self.components if not c.allowed}

    def listing(self) -> str:
        return "\n".join(c.brief() + ("" if c.allowed else "   (unannotated)") for c in self.components)

    def heuristic_audit(self, text: str) -> set[str]:
        """Cheap backstop: a part counts as revealed if the reply contains all its distinctive tokens
        (or its literal text). Used alongside the LLM audit and alone if the LLM output is unusable."""
        tn = norm_sql(text)
        low = text.lower()
        words = key_tokens(text) | {w.lower() for w in re.findall(r"[A-Za-z_]\w*", text)}

        def present(k: str) -> bool:
            return k in words or any(w in low for w in FUNC_WORDS.get(k, ()))

        found = set()
        for c in self.components:
            if norm_sql(c.text) and norm_sql(c.text) in tn:
                found.add(c.id)
            elif c.keys and all(present(k) for k in c.keys) and (len(c.keys) >= 2 or c.kind in ("limit", "table")):
                found.add(c.id)
            elif c.kind == "limit":
                n = re.search(r"\d+", c.text)
                if n and re.search(rf"\b(top|first|limit(?:ed)?(?: to)?)\s+{n.group()}\b", text, re.I):
                    found.add(c.id)
        return found

    def to_dict(self) -> dict:
        return {"gold_sql": self.gold_sql, "snippets": self.snippets,
                "components": [{**asdict(c), "keys": sorted(c.keys)} for c in self.components]}


# ----------------------------------------------------------------------------- ledger

@dataclass
class Ledger:
    """Per-episode memory. Allowed parts always pass; privileged parts pass only while the
    episode's privileged count is under the cap. Re-stating a part already revealed is free."""
    cap: int
    revealed: set[str] = field(default_factory=set)
    privileged_revealed: set[str] = field(default_factory=set)
    history: list[dict] = field(default_factory=list)

    @property
    def privileged_used(self) -> int:
        return len(self.privileged_revealed)

    def admit(self, comp_ids: Iterable[str], cmap: ComponentMap) -> set[str]:
        ok: set[str] = set()
        for cid in sorted(comp_ids, key=lambda x: int(x[1:]) if x[1:].isdigit() else 0):
            comp = cmap.by_id.get(cid)
            if comp is None:
                continue
            if comp.allowed or cid in self.revealed:
                ok.add(cid)
            elif self.privileged_used < self.cap:
                ok.add(cid)
                self.privileged_revealed.add(cid)
            # else: over the cap -> blocked
        self.revealed |= ok
        return ok

    def snapshot(self) -> dict:
        return {"cap": self.cap, "privileged_used": self.privileged_used,
                "revealed": sorted(self.revealed), "privileged_revealed": sorted(self.privileged_revealed)}


# ----------------------------------------------------------------------------- prompts

SPLIT_PROMPT = """A database user is being asked a clarification question. Split it into its
independent sub-questions so each can be considered on its own. Keep each sub-question's
wording; do not add, merge or answer anything. A single question stays as one item.

Question:
{question}

Return only JSON: {{"clauses": ["...", "..."]}}"""

AUDIT_PROMPT = """You check what a draft reply discloses about a hidden SQL query.

Hidden query parts:
{listing}

Draft reply:
\"\"\"{draft}\"\"\"

List every part the draft states, implies or paraphrases. A part is revealed if a reader
could reconstruct it from the reply: "highest first" reveals a descending ORDER BY,
"only the top five" reveals a LIMIT 5, "round to two places" reveals a ROUND(..., 2),
naming a table or column reveals that table/column part, "leave out rows without X"
reveals an IS NOT NULL filter. Do not list parts the draft merely talks around.

Return only JSON: {{"revealed": ["C1", "C4"]}}"""

REDACT_PROMPT = """Rewrite the reply below so it no longer conveys the following hidden-query parts,
not even by implication or paraphrase. Keep everything else exactly as informative as it
was, in the same voice. Do not say that something was removed or that you cannot share
it; simply leave it out. If nothing substantive remains, reply with exactly:
{fallback}

Parts to remove:
{blocked}

Reply:
\"\"\"{draft}\"\"\"

Return only the rewritten reply."""

_QWORD = r"(?:which|what|how|should|shall|do|does|is|are|can|could|would|where|when|whether|if|in what|and what|and which)"
_SPLIT_RE = re.compile(rf"(?:[,;]\s*|\s+)(?:and|but|also)\s+(?={_QWORD}\b)|\?\s+(?=\S)", re.I)


def heuristic_split(question: str) -> list[str]:
    parts = [p.strip() for p in _SPLIT_RE.split(question.strip()) if p and p.strip()]
    out = []
    for p in parts:
        if not p.endswith("?"):
            p += "?"
        p = p[0].upper() + p[1:]
        out.append(p)
    return out or [question.strip()]


def _json_block(text: str) -> Optional[dict]:
    text = re.sub(r"^```[a-zA-Z]*\s*|```$", "", (text or "").strip(), flags=re.M)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# ----------------------------------------------------------------------------- defended simulator

Encoder = Callable[[str], tuple[str, str, Optional[dict]]]   # question -> (label, raw, usage)
Decoder = Callable[[str, str], tuple[str, Optional[dict]]]   # (question, label) -> (reply, usage)
LLM = Callable[[str], tuple[str, Optional[dict]]]            # prompt -> (text, usage)


class DefendedSimulator:
    """Replaces encoder -> decoder for one episode. Stateful: asks must arrive in order."""

    def __init__(self, record: dict, encoder: Encoder, decoder: Decoder, llm: Optional[LLM],
                 cap: int = 0, splitter: str = "llm", unanswerable: str = UNANSWERABLE,
                 annotation_snippets: Optional[Iterable[str]] = None):
        self.cmap = ComponentMap(record, annotation_snippets)
        self.ledger = Ledger(cap)
        self.encoder, self.decoder, self.llm = encoder, decoder, llm
        self.splitter = splitter if llm is not None else "heuristic"
        self.unanswerable = unanswerable
        self.turn = 0

    # ---- benchmark binding (imports happen here so the module stays importable offline)
    @classmethod
    def from_bird_interact(cls, record: dict, status, db_schema: str, model: str, call, **kw) -> "DefendedSimulator":
        from prompt_utils import build_user_encoder_prompt, build_user_decoder_prompt, parse_encoder_response

        def encoder(q):
            raw, usage = call(build_user_encoder_prompt(q, status, db_schema), model)
            return parse_encoder_response(raw), raw, usage

        def decoder(q, label):
            return call(build_user_decoder_prompt(q, label, status, db_schema), model)

        def llm(prompt):
            return call(prompt, model)

        return cls(record, encoder, decoder, llm, **kw)

    # ---- steps
    def split_clauses(self, question: str) -> tuple[list[str], Optional[dict]]:
        if self.splitter == "llm":
            text, usage = self.llm(SPLIT_PROMPT.format(question=question))
            data = _json_block(text)
            clauses = [c.strip() for c in (data or {}).get("clauses", []) if isinstance(c, str) and c.strip()]
            if clauses:
                return clauses, usage
            return heuristic_split(question), usage
        return heuristic_split(question), None

    def audit(self, draft: str) -> tuple[set[str], Optional[dict], str]:
        heur = self.cmap.heuristic_audit(draft)
        if self.llm is None or not self.cmap.components:
            return heur, None, "heuristic"
        text, usage = self.llm(AUDIT_PROMPT.format(listing=self.cmap.listing(), draft=draft))
        data = _json_block(text)
        if not data or not isinstance(data.get("revealed"), list):
            return heur, usage, "heuristic(llm-unparsable)"
        ids = {str(x).strip().upper() for x in data["revealed"]} & set(self.cmap.by_id)
        return ids | heur, usage, "llm+heuristic"

    def redact(self, draft: str, blocked: set[str]) -> tuple[str, Optional[dict]]:
        listing = "\n".join(self.cmap.by_id[b].brief() for b in sorted(blocked))
        if self.llm is None:
            # offline fallback: drop sentences that carry a blocked part
            keep = [s for s in re.split(r"(?<=[.!?])\s+", draft)
                    if not (self.cmap.heuristic_audit(s) & blocked)]
            return (" ".join(keep).strip() or NO_ANSWER_REPLY), None
        text, usage = self.llm(REDACT_PROMPT.format(fallback=NO_ANSWER_REPLY, blocked=listing, draft=draft))
        text = (text or "").strip().strip('"')
        return (text or NO_ANSWER_REPLY), usage

    def ask(self, question: str) -> dict:
        self.turn += 1
        usage: dict = {}

        def tally(k, u):
            if u:
                usage.setdefault(k, []).append(u)

        # 1. split
        clauses, u = self.split_clauses(question)
        tally("split", u)

        # 2. label each clause on its own
        labeled = []
        for c in clauses:
            label, raw, u = self.encoder(c)
            tally("encoder", u)
            labeled.append({"text": c, "label": label, "encoder_raw": raw})
        answerable = [x for x in labeled if not x["label"].strip("* ").lower().startswith(("unanswerable", self.unanswerable.lower()))]

        # 3. stock decoder drafts the answerable clauses; a fully unanswerable ask gets the stock refusal
        drafts = []
        if answerable:
            for x in answerable:
                d, u = self.decoder(x["text"], x["label"])
                tally("decoder", u)
                x["draft"] = d
                drafts.append(d)
        else:
            d, u = self.decoder(question, self.unanswerable)
            tally("decoder", u)
            drafts.append(d)
        draft = "\n".join(s.strip() for s in drafts if s and s.strip())

        # 4. audit, 5. ledger, 6. redact only what is over the cap
        found, u, audit_mode = self.audit(draft)
        tally("audit", u)
        ok = self.ledger.admit(found, self.cmap)
        blocked = found - ok
        if blocked:
            reply, u = self.redact(draft, blocked)
            tally("redact", u)
            # second look: did the rewrite still leak a blocked part? cut harder if so
            leak = self.cmap.heuristic_audit(reply) & blocked
            if leak:
                reply, u = self.redact(reply, blocked)
                tally("redact", u)
        else:
            reply = draft

        allowed_out = sorted(c for c in ok if self.cmap.by_id[c].allowed)
        priv_out = sorted(c for c in ok if not self.cmap.by_id[c].allowed)
        return {
            "turn": self.turn,
            "question": question,
            "reply": reply,
            "answered": bool(answerable),
            "label": labeled[0]["label"] if len(labeled) == 1 else "|".join(x["label"] for x in labeled),
            "clauses": labeled,
            "draft": draft,
            "audit_mode": audit_mode,
            "revealed_allowed": allowed_out,
            "revealed_privileged": priv_out,
            "blocked": sorted(blocked),
            "blocked_text": [self.cmap.by_id[b].brief() for b in sorted(blocked)],
            "ledger": self.ledger.snapshot(),
            "usage": usage,
        }


def run_episode(record: dict, questions: list[str], encoder: Encoder, decoder: Decoder, llm: Optional[LLM],
                cap: int = 0, **kw) -> list[dict]:
    """Convenience: one episode, questions in order, one DefendedSimulator."""
    sim = DefendedSimulator(record, encoder, decoder, llm, cap=cap, **kw)
    return [sim.ask(q) for q in questions]


# ----------------------------------------------------------------------------- self-test

def _selftest():
    """Offline check of the mechanics with a toy task and rule-based stand-ins for the LLM."""
    record = {
        "instance_id": "toy",
        "sol_sql": ("SELECT si.zonelabel, ROUND(AVG((p.Cloud_Metrics->>'Noise_Db')::numeric), 2) AS avg_noise "
                    "FROM pointcloud p JOIN sites si ON si.zoneregistry = p.zoneref "
                    "WHERE si.phasefactor IS NOT NULL GROUP BY si.zonelabel ORDER BY avg_noise ASC LIMIT 5"),
        "user_query_ambiguity": {"critical_ambiguity": [
            {"term": "noise", "sql_snippet": "AVG((p.Cloud_Metrics->>'Noise_Db')::numeric)"},
            {"term": "sites", "sql_snippet": "JOIN sites si ON si.zoneregistry = p.zoneref"},
        ]},
    }
    # stand-in encoder: definition clauses are answerable (AMB), slot clauses are not (UNA)
    def encoder(q):
        una = re.search(r"\b(which columns|how many rows|one row per|sorted|order|limit|exclude|missing|null)\b", q, re.I)
        return ("UNA" if una else "AMB"), "stub", None

    # stand-in decoder: leaks structure, as the stock decoder can
    def decoder(q, label):
        if label == "UNA":
            return "Sorry, I can't answer that.", None
        return ("Noise is the average of Cloud_Metrics Noise_Db per site, joined to sites on zoneregistry = zoneref. "
                "Round it to 2 decimals, keep only sites with a phasefactor, lowest first, and only the top 5."), None

    def check(cond, msg):
        print(("ok   " if cond else "FAIL ") + msg)
        if not cond:
            raise SystemExit(1)

    cmap = ComponentMap(record)
    print(cmap.listing(), "\n")
    kinds_allowed = {c.kind for c in cmap.components if c.allowed}
    check("join" in kinds_allowed and "select_column" in kinds_allowed, "annotated join and AVG column marked allowed")
    check(all(not c.allowed for c in cmap.components if c.kind in ("limit", "order_by", "rounding", "filter")),
          "limit / order / rounding / filter marked privileged")

    q = "How exactly should 'noise' be measured, and how should the output be sorted?"
    check(len(heuristic_split(q)) == 2, f"compound probe splits into 2 clauses: {heuristic_split(q)}")

    # cap = 0: every privileged part the decoder leaks is cut; allowed parts still go out
    rows = run_episode(record, [q, "What does 'noise' mean to you?"], encoder, decoder, None, cap=0)
    r0 = rows[0]
    print(json.dumps({k: r0[k] for k in ("answered", "label", "revealed_allowed", "revealed_privileged", "blocked")}), "\n")
    print("reply@cap0:", r0["reply"], "\n")
    check(r0["answered"], "compound probe is still answered (definition clause)")
    check(r0["clauses"][1]["label"] == "UNA", "slot clause labeled UNA on its own")
    check(r0["revealed_allowed"], "allowed parts disclosed")
    check(not r0["revealed_privileged"] and r0["blocked"], "privileged parts blocked at cap 0")
    check("top 5" not in r0["reply"].lower() and "lowest first" not in r0["reply"].lower(), "limit/order text removed from reply")
    check(rows[1]["ledger"]["privileged_used"] == 0, "ledger stays at 0 privileged across the episode")

    # cap = 1: exactly one privileged part per episode may pass, and it does not pass twice
    rows = run_episode(record, [q, q], encoder, decoder, None, cap=1)
    print("reply@cap1:", rows[0]["reply"], "\n")
    check(len(rows[0]["revealed_privileged"]) == 1, "cap 1 admits exactly one privileged part")
    check(rows[1]["ledger"]["privileged_used"] == 1, "second identical ask adds no new privileged part")
    check(set(rows[1]["revealed_privileged"]) == set(rows[0]["revealed_privileged"]), "re-stating the admitted part is free")

    # a plain unanswerable ask gets the stock refusal, nothing else
    rows = run_episode(record, ["Which columns should the result show?"], encoder, decoder, None, cap=0)
    check(not rows[0]["answered"] and rows[0]["reply"].startswith("Sorry"), "fully unanswerable ask: stock refusal passes through")
    print("\nselftest passed")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--map", help="print the ComponentMap for one record from a jsonl (needs --instance_id)")
    ap.add_argument("--instance_id")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
    elif a.map:
        for line in open(a.map, encoding="utf-8"):
            r = json.loads(line)
            if r.get("instance_id") == a.instance_id:
                print(ComponentMap(r).listing())
                break
        else:
            raise SystemExit("instance not found")
    else:
        ap.print_help()
