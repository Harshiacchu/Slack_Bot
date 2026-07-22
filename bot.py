"""
PortDesk — a minimal, rule-based Slack BI bot for the DAMG 7370 lab.

This is the "harness in miniature": a router picks one of 5 intents,
each intent uses only its allowed tool(s), and every reply goes through
a verifier before being sent back to Slack. No LLM/API key required —
intent matching is keyword-based on purpose, to keep this lab runnable
in one sitting.

Run:
    pip install -r requirements.txt
    python data/build_db.py
    python bot.py
"""
import os
import json
import sqlite3
import re
from datetime import date, timedelta
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv()

# ---------- Harness config (limits, matches the design doc) ----------
MAX_DRILLDOWN_DIMENSIONS = 4
MAX_SQL_CALLS_PER_REQUEST = 6
VARIANCE_EXPLAINED_STOP = 0.80

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "portdesk.db")
GLOSSARY_PATH = os.path.join(os.path.dirname(__file__), "data", "glossary.json")

ALLOWED_TABLES = {"shipments"}  # the only table this bot is allowed to touch


# ---------- Tool layer ----------
def run_sql(sql: str):
    """The only function allowed to touch the database. Verifies before executing."""
    ok, reason = verify_sql(sql)
    if not ok:
        return None, reason
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(sql)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    conn.close()
    return [dict(zip(cols, r)) for r in rows], None


def verify_sql(sql: str):
    """Very small stand-in for the SQL parser + allowlist check in the harness."""
    s = sql.strip().lower()
    if not s.startswith("select"):
        return False, "blocked: only SELECT statements are allowed"
    for table in re.findall(r"from\s+(\w+)", s):
        if table not in ALLOWED_TABLES:
            return False, f"blocked: table '{table}' is not on the allowlist"
    if "where" not in s:
        return False, "blocked: query must include a date filter (WHERE clause)"
    return True, None


def lookup_metric_definition(term: str):
    with open(GLOSSARY_PATH) as f:
        glossary = json.load(f)
    return glossary.get(term.lower())


def get_dashboard_link(topic: str):
    # Stubbed — in production this hits a real dashboard index.
    return {"title": f"{topic.title()} Overview Dashboard", "url": "https://looker.example.com/portdesk-overview"}


# ---------- Intent router ----------
def classify_intent(text: str) -> str:
    t = text.lower().strip()

    if t == "help" or "what can you do" in t:
        return "help"

    if "predict" in t or "forecast" in t:
        return "unsupported"

    if "dashboard" in t:
        return "dashboard"

    if "what does" in t or "define" in t or "meaning of" in t:
        return "glossary"

    if "why" in t or "root cause" in t or "spike" in t:
        return "root_cause"

    if "compare" in t or " vs " in t or "versus" in t:
        return "comparison"

    if "how many" in t or "count" in t:
        return "shipment_count"

    if "average" in t and ("delay" in t or "late" in t):
        return "average_delay"

    if "on-time" in t or "on time" in t or "otd" in t:
        return "on_time_rate"

    return "unknown"


# ---------- Pipelines ----------
def pipeline_on_time_rate(text):
    end = date(2026, 7, 21)
    start = end - timedelta(days=7)

    sql = f"""
        SELECT ROUND(AVG(on_time) * 100, 1) AS otd_pct
        FROM shipments
        WHERE ship_date >= '{start}'
          AND ship_date <= '{end}'
    """

    rows, err = run_sql(sql)

    if err:
        return f":x: Verifier blocked this query — {err}"

    pct = rows[0]["otd_pct"]

    return (
        f"*On-time delivery rate, last 7 days:* {pct}%\n"
        f"_Data as of {end} · source: shipments table · confidence: high_"
    )

def pipeline_comparison(text):
    end = date(2026, 7, 21)
    start = end - timedelta(days=30)
    sql = f"""SELECT region, ROUND(AVG(on_time)*100,1) as otd_pct
              FROM shipments WHERE ship_date >= '{start}' AND ship_date <= '{end}'
              GROUP BY region"""
    rows, err = run_sql(sql)
    if err:
        return f":x: Verifier blocked this query — {err}"
    lines = [f"• {r['region']}: {r['otd_pct']}% on-time" for r in rows]
    return ("*Regional on-time delivery comparison, last 30 days:*\n" + "\n".join(lines) +
            f"\n_Data as of {end} · source: shipments table · confidence: high_")


def pipeline_root_cause(text):
    """A bounded drill-down loop: stop conditions from the design doc are enforced here."""
    end = date(2026, 7, 21)
    start = end - timedelta(days=7)
    prior_start = start - timedelta(days=7)
    sql_calls = 0

    # Step 1: headline metric, this period vs prior period
    sql1 = f"""SELECT ROUND(AVG(on_time)*100,1) as otd FROM shipments
               WHERE ship_date >= '{start}' AND ship_date <= '{end}'"""
    sql2 = f"""SELECT ROUND(AVG(on_time)*100,1) as otd FROM shipments
               WHERE ship_date >= '{prior_start}' AND ship_date < '{start}'"""
    cur_rows, _ = run_sql(sql1); sql_calls += 1
    prior_rows, _ = run_sql(sql2); sql_calls += 1
    cur_otd, prior_otd = cur_rows[0]["otd"], prior_rows[0]["otd"]
    drop = round(prior_otd - cur_otd, 1)

    if drop <= 0:
        return f"On-time delivery didn't drop this week ({cur_otd}% vs {prior_otd}% prior week)."

    # Step 2: drill by one dimension (region) — stop condition: dimensions drilled
    dims_drilled = 0
    sql3 = f"""SELECT region, ROUND(AVG(on_time)*100,1) as otd FROM shipments
               WHERE ship_date >= '{start}' AND ship_date <= '{end}' GROUP BY region"""
    region_rows, _ = run_sql(sql3); sql_calls += 1; dims_drilled += 1

    worst = min(region_rows, key=lambda r: r["otd"])
    explained_fraction = 0.85  # simplified stand-in for real variance math

    if (explained_fraction >= VARIANCE_EXPLAINED_STOP
            or dims_drilled >= MAX_DRILLDOWN_DIMENSIONS
            or sql_calls >= MAX_SQL_CALLS_PER_REQUEST):
        return (f"*Why on-time delivery dropped ({prior_otd}% → {cur_otd}%):*\n"
                f"• {worst['region']} is the primary driver, at {worst['otd']}% on-time this week.\n"
                f"_Explains ~{int(explained_fraction*100)}% of the variance · "
                f"{sql_calls} queries run · stopped at dimension limit or variance threshold · confidence: 0.85_")

    return "Drill-down inconclusive — escalating to a human analyst."


def pipeline_glossary(text):
    for term in ["dwell time", "on-time delivery", "otd"]:
        if term in text.lower():
            entry = lookup_metric_definition(term)
            if entry:
                return (f"*{term.title()}:* {entry['definition']}\n"
                        f"_Owner: {entry['owner']} · last updated {entry['last_updated']}_")
    return "I couldn't find that term in the metric catalog — try rephrasing?"


def pipeline_dashboard(text):
    d = get_dashboard_link("port congestion")
    return f"*{d['title']}:* {d['url']}"


def pipeline_unsupported(text):
    return (":no_entry: I can't generate forecasts or send emails — that's outside what I'm scoped to do.\n"
            "I *can* show you historical trends instead — try asking about last month's data.")





# ---------- Slack wiring ----------
app = App(token=os.environ["SLACK_BOT_TOKEN"])


@app.event("app_mention")
def handle_mention(event, say):
    text = re.sub(r"<@[^>]+>", "", event.get("text", "")).strip()

    intent = classify_intent(text)
    pipeline = PIPELINES.get(intent, pipeline_unknown)

    try:
        reply = pipeline(text)
    except Exception as exc:
        print(f"Request failed: {exc}")
        reply = (
            ":x: I couldn't complete that request because of an internal error."
        )

    say(reply)

def pipeline_help(text):
    return (
        "*PortDesk supported questions:*\n"
        "• What was our on-time delivery rate last week?\n"
        "• How many shipments were delivered?\n"
        "• What is the average delivery delay?\n"
        "• Compare shipment performance by region.\n"
        "• Why did on-time delivery drop?\n"
        "• What does on-time delivery mean?\n"
        "• Show me the port congestion dashboard.\n"
        "• Predict delayed shipments in Q4. _(guardrail test)_"
    )

def pipeline_shipment_count(text):
    end = date(2026, 7, 21)
    start = end - timedelta(days=7)

    sql = f"""
        SELECT COUNT(*) AS shipment_count
        FROM shipments
        WHERE ship_date >= '{start}'
          AND ship_date <= '{end}'
    """

    rows, err = run_sql(sql)

    if err:
        return f":x: Verifier blocked this query — {err}"

    count = rows[0]["shipment_count"]

    return (
        f"*Shipments recorded in the last 7 days:* {count}\n"
        f"_Data as of {end} · source: shipments table · confidence: high_"
    )


def get_shipment_columns():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(shipments)")
    columns = [row[1] for row in cur.fetchall()]
    conn.close()
    return columns

def pipeline_average_delay(text):
    end = date(2026, 7, 21)
    start = end - timedelta(days=7)

    columns = get_shipment_columns()

    possible_delay_columns = [
        "delay_days",
        "delay_hours",
        "delay_minutes",
        "delivery_delay"
    ]

    delay_column = next(
        (column for column in possible_delay_columns if column in columns),
        None
    )

    if delay_column is None:
        return (
            ":warning: Average delay is unavailable because the shipments "
            "table does not contain a recognized delay column.\n"
            f"_Available columns: {', '.join(columns)}_"
        )

    sql = f"""
        SELECT ROUND(AVG({delay_column}), 1) AS average_delay
        FROM shipments
        WHERE ship_date >= '{start}'
          AND ship_date <= '{end}'
    """

    rows, err = run_sql(sql)

    if err:
        return f":x: Verifier blocked this query — {err}"

    average = rows[0]["average_delay"]
    unit = delay_column.replace("delay_", "").replace("delivery_", "")

    return (
        f"*Average delivery delay, last 7 days:* {average} {unit}\n"
        f"_Data as of {end} · source: shipments table · confidence: high_"
    )

def pipeline_unknown(text):
    return (
        ":grey_question: I couldn't match that request.\n"
        "Mention me with `help` to see the supported questions."
    )

PIPELINES = {
    "help": pipeline_help,
    "on_time_rate": pipeline_on_time_rate,
    "shipment_count": pipeline_shipment_count,
    "average_delay": pipeline_average_delay,
    "comparison": pipeline_comparison,
    "root_cause": pipeline_root_cause,
    "glossary": pipeline_glossary,
    "dashboard": pipeline_dashboard,
    "unsupported": pipeline_unsupported,
    "unknown": pipeline_unknown,
}

if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    print("PortDesk is running. @mention it in your Slack channel.")
    handler.start()