# PortDesk — A Slack BI Assistant for Logistics & Supply Chain Teams

**Course:** DAMG 7370, Designing Data Architectures for BI

---

## 1. What This Bot Does

PortDesk is a Slack bot that lets a logistics/ops team ask everyday BI questions in plain English, right inside a Slack channel, and get back a trustworthy, sourced answer instead of having to open a dashboard or write SQL themselves.

Example questions it needs to handle:

- "What was our on-time delivery rate last week?"
- "Why did container dwell time spike at the Long Beach terminal?"
- "Compare carrier performance, East Coast vs West Coast, this month."
- "What does 'dwell time' actually mean in our reporting?"
- "Where's the port congestion dashboard?"

**Core design idea:** route the question to the right small pipeline first, and never let the model touch real data directly — everything goes through a verified, permissioned tool layer. The model *decides* what to do; it never *executes* anything itself.

---

## 2. Architecture Diagram

![PortDesk Architecture](<img width="1961" height="1242" alt="image" src="https://github.com/user-attachments/assets/b49202fc-8413-4b91-bb14-0d004ff9944d" />
)

**Why this shape:** the router runs first and is cheap (a small classifier call), so expensive work only happens once we know which of the four pipelines actually applies. Every pipeline shares the same tool layer and the same verifier — so we're not duplicating security logic four times.

---

## 3. Prompt Template (with Schema)

System prompt used for the SQL-generation step (one of the four pipelines). Follows the 8-part anatomy from class: role, task, context, constraints, examples, output format, success criteria.

```
You are PortDesk, a logistics BI analyst assistant.

# Task
Convert the user's question into a single read-only SQL query
against the approved warehouse tables listed below.

# Context
Approved tables: fact_shipments, dim_carrier, dim_port, dim_date
Today's date: {{current_date}}
User's team: {{user_team}}

# Constraints
- SELECT statements only. Never write, update, or delete.
- Must include a date filter.
- Must not exceed a 5GB scan (add LIMIT if unsure).
- Use only the tables listed above. Do not invent columns.

# Input
<question>{{user_question}}</question>

# Examples
(2 worked examples of question -> SQL go here, chosen to cover
 a metric lookup and a comparison case)

# Output format
Return JSON only:
{ "sql": string, "explanation": string, "tables_used": string[] }

# Success criteria
The SQL parses, uses only allowed tables, includes a date filter,
and the explanation states which metric definition was used.
```

---

## 4. Tool Definitions (Typed)

Four tools cover the whole bot. Each is a narrow, typed function — not one giant do-everything tool.

| Tool | Inputs (typed) | Returns | Notes |
|---|---|---|---|
| `get_metric(metric: MetricEnum, start_date: date, end_date: date, region?: RegionEnum)` | Enum + ISO dates | number + unit + `as_of` timestamp | Hits pre-aggregated rollup tables, not raw fact tables |
| `run_sql(sql: string)` | Validated SQL string | rows (max 10k) + row count | Only called after the SQL parser/allowlist check passes |
| `lookup_metric_definition(term: string)` | Free-text term | definition + owner + last_updated | Backed by the metric catalog RAG store |
| `get_dashboard_link(topic: string)` | Free-text topic | dashboard URL + title | Simple index search, no LLM call needed |

---

## 5. RAG Design

**What gets indexed**
- Metric glossary docs (Confluence/Notion export) — defines terms like "dwell time," "on-time delivery"
- Runbooks / playbooks for common ops incidents
- Dashboard catalog (title, URL, owner, description)

**Chunking strategy**
Section-aware chunking (split on headings), since glossary and runbook docs are already organized by heading per term or per incident type. A chunk = one definition or one runbook section, so it can stand alone and answer a question by itself.

**Retrieval**
Hybrid search (keyword + vector). Reasoning: ops people ask about exact terms like "dwell time" or carrier codes like "MAEU" — keyword search catches the exact string, vector search catches paraphrases like "how long containers sit at port." A metadata filter on document owner/team is applied *before* similarity search, not after, so nobody sees glossary entries they're not scoped to.

**Evaluation**
A golden set of ~30 glossary/runbook Q&A pairs, scored on retrieval recall and answer faithfulness, checked before every deploy.

---

## 6. Agent Harness

The harness is everything around the model that keeps it safe and predictable.

| Component | Design Decision |
|---|---|
| Permissions | Bot uses a dedicated read-only warehouse role. No write/update/delete grants exist for this identity — not just a prompt instruction. |
| Table allowlist | Only 6 certified BI tables are queryable. Anything else is rejected before execution. |
| Row-level security | Every query is rewritten to add the asking user's team/region filter, so cross-team data isn't returned even if asked for. |
| Verifier | SQL parser checks syntax + allowlist + presence of a date filter. A separate reconciliation check compares the answer to a cached certified aggregate and flags outliers. |
| Sandbox | Query execution is isolated from any production write path — it's a read replica, not the live OLTP database. |
| Cost/latency limits | Per-query timeout (20s), per-query scan cap (5GB), per-user daily query cap. |
| Observability | Every step (route chosen, tool calls, verifier verdict, final answer) is logged with a trace ID so any answer can be replayed and debugged. |

---

## 7. Loop & Stop Conditions

Only one of the four pipelines actually loops: **root-cause analysis** (e.g. "why did on-time delivery drop?"). The other three are single-pass (one tool call, done). The root-cause loop needs explicit guardrails or it can drill down forever.

**Loop steps**
1. Get headline metric and its change vs. prior period
2. Break down by one dimension (carrier, port, region, product)
3. Verify the breakdown's contributions sum back to the total change
4. If one dimension explains enough of the drop, stop and answer; otherwise drill one level deeper

**Stop conditions** (all enforced together, whichever hits first)
- Explains ≥ 80% of the variance → stop and answer
- Maximum 4 dimensions drilled → stop, return partial explanation
- Maximum 6 SQL queries executed → stop, return partial explanation
- Confidence score < 0.7 after max drill-down → escalate to a human instead of guessing

State only grows (never mutated in place), a verifier checks each step independently of the model's own claim of success, and the budget is a hard ceiling, not a suggestion.

---

## 8. Security Controls

- Slack request signing verified on every incoming event (prevents spoofed requests)
- User identity resolved via Slack OAuth, mapped to a warehouse role — no shared service account for all users
- Retrieved documents (RAG) are treated as untrusted data, wrapped in delimiters, never treated as instructions
- SQL allowlist + parser blocks anything outside `SELECT` on the 6 certified tables
- Row-level security filter applied before data leaves the warehouse, not after
- No query result is ever forwarded to Slack unfiltered — capped at 10k rows, PII columns excluded at the table level
- Full audit log per query: user, question, SQL run, rows returned, timestamp
- Bot politely declines unsupported requests (e.g. "predict next quarter," "email this to the VP") instead of improvising

---

## 9. Five Evaluation Cases (Golden Q&A)

| # | Question | Expected Behavior |
|---|---|---|
| 1 | "What was our on-time delivery rate last week?" | Metric lookup → correct SQL, correct number, cites data-as-of timestamp |
| 2 | "Compare carrier performance, East vs West Coast, this month." | Comparison pipeline → two SQL calls, delta table, no hallucinated carriers |
| 3 | "Why did container dwell time spike at Long Beach?" | Root-cause loop → drills to ≤4 dimensions, stops at ≥80% variance explained or escalates |
| 4 | "What does 'dwell time' mean in our reporting?" | RAG lookup → returns catalog definition + owner, no SQL call made |
| 5 | "Predict our Q4 shipping volume with 90% confidence." | Unsupported request → polite refusal + suggested alternative, not a fabricated forecast |

---

## Summary

The prompt is just one small piece here — the SQL-generation template in section 3. What actually makes PortDesk trustworthy enough to hand to an ops team is everything else: the router that keeps each pipeline simple, the tool layer that never lets the model touch data directly, the verifier that double-checks every answer, the loop limits that stop root-cause analysis from running away, and the security controls that keep it scoped to data the asker is allowed to see.
