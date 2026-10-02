"""QuickBite Refund Desk - Streamlit demo.  Run:  streamlit run app.py"""
import hmac
import json
import os

import pandas as pd
import streamlit as st

import pipeline as P

st.set_page_config(page_title="QuickBite Refund Desk", page_icon="🍔", layout="wide")
import inspect
W = {"width": "stretch"} if "width" in inspect.signature(st.button).parameters else {"use_container_width": True}
env = P.load_env()

# ------------------------------------------------------------------ deployment: secrets, access gate, usage cap
try:                                   # hosted platforms keep keys in st.secrets; the pipeline reads environment variables
    for _k, _v in st.secrets.items():
        if isinstance(_v, str):
            os.environ.setdefault(_k, _v)
except Exception:                      # no secrets file (local run): keys come from the env file instead
    pass

_pw = os.environ.get("APP_PASSWORD", "")
if _pw and not st.session_state.get("authed"):
    st.title("🍔 QuickBite · Refund Desk")
    _entered = st.text_input("Access password", type="password")
    if _entered and hmac.compare_digest(_entered.encode(), _pw.encode()):
        st.session_state.authed = True
        st.rerun()
    elif _entered:
        st.error("Wrong password.")
    st.stop()

try:
    MAX_RUNS = int(os.environ.get("MAX_RUNS_PER_SESSION", "40"))   # protects the API budget of a shared deployment
except ValueError:
    MAX_RUNS = 40


def stat_grid(stats, min_px=170):
    """Stat cards that wrap their text instead of truncating it (st.metric cuts long values with '...')."""
    cells = "".join(
        f'<div class="sg-cell"><div class="sg-label">{label}</div><div class="sg-value">{value}</div></div>'
        for label, value in stats)
    st.markdown(
        f"""<style>
        .sg {{display:grid;grid-template-columns:repeat(auto-fit,minmax({min_px}px,1fr));gap:10px;margin:4px 0 10px}}
        .sg-cell {{border:1px solid rgba(128,128,128,.35);border-radius:8px;padding:10px 12px;min-width:0}}
        .sg-label {{font-size:.8rem;opacity:.7;margin-bottom:2px;white-space:normal;overflow-wrap:anywhere}}
        .sg-value {{font-size:1.25rem;font-weight:600;line-height:1.3;white-space:normal;overflow-wrap:anywhere;
                    word-break:break-word}}
        </style><div class="sg">{cells}</div>""", unsafe_allow_html=True)

# ------------------------------------------------------------------ state
if "form" not in st.session_state:
    st.session_state.form = P.sample_complaint(list(P.SAMPLES)[0])
if "action_log" not in st.session_state:
    st.session_state.action_log = []      # per visitor: never shared between sessions
if "runs" not in st.session_state:
    st.session_state.runs = 0
if "result" not in st.session_state:
    st.session_state.result = None


def load_sample(name):
    st.session_state.form = P.sample_complaint(name)
    st.session_state.result = None
    st.session_state.pop("items_editor", None)


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("Settings")
    st.caption(f"Keys read from: `{env.get('_source', 'none')}`")
    st.write("🔑 Anthropic:", "✅" if P.get_key("ANTHROPIC_API_KEY") else "❌",
             " · OpenRouter:", "✅" if P.get_key("OPEN_ROUTER_API_KEY", "OPENROUTER_API_KEY") else "❌",
             " · TypeSafe:", "✅" if P.get_key("TYPESAFE_API_KEY") else "—")

    ext_label = st.radio("Step 2 · Complaint extraction",
                         ["LLM (Claude Haiku)", "Jev (classifier)", "Offline heuristic"], index=0)
    extractor = {"LLM (Claude Haiku)": "llm", "Jev (classifier)": "jev", "Offline heuristic": "heuristic"}[ext_label]

    provider, model = "anthropic", "claude-haiku-4-5"
    if extractor == "llm":
        provider = st.selectbox("LLM provider", ["anthropic", "openrouter"])
        model = st.text_input("Model", "claude-haiku-4-5" if provider == "anthropic" else "anthropic/claude-haiku-4.5")
    jev_url = st.text_input("Jev endpoint", "https://openrouter.ai/api/alpha/decisions") if extractor == "jev" else ""
    if extractor == "jev":
        st.caption("Jev runs on the OpenRouter Decisions API and uses your OpenRouter key. Do not point it at api.typesafe.ai unless you have a TypeSafe key.")
    else:
        # Jev does not write text; the reply always uses an LLM. Configured separately below.
        pass

    if extractor in ("jev", "heuristic"):
        st.divider()
        st.caption("Reply writing (step 5) always uses an LLM:")
        provider = st.selectbox("Writer provider", ["anthropic", "openrouter"], key="wprov")
        model = st.text_input("Writer model", "claude-haiku-4-5" if provider == "anthropic" else "anthropic/claude-haiku-4.5", key="wmodel")
    writer = st.radio("Step 5 · Reply writing", ["LLM + validation", "Template only (no API)"], index=0)

    st.divider()
    st.subheader("Prices (USD / 1M tokens)")
    pin, pout = P.PRICES.get(model.split("/")[-1].replace("4.5", "4-5"), (1.0, 5.0))
    pin = st.number_input("LLM · input", value=float(pin), step=0.1)
    pout = st.number_input("LLM · output", value=float(pout), step=0.1)
    jin = st.number_input("Jev · input", value=0.042, step=0.001, format="%.3f")
    st.caption("Verify current prices before the demo.")

cfg = P.Config(extractor=extractor, provider=provider, model=model,
               writer="template" if writer.startswith("Template") else "llm",
               jev_url=jev_url or "https://openrouter.ai/api/alpha/decisions", jev_model="typesafe/jev-1.13",
               price_overrides={model: (pin, pout), "typesafe/jev-1.13": (jin, 0.0)})

# ------------------------------------------------------------------ header
st.title("🍔 QuickBite · Refund Desk")
st.caption("Hybrid pipeline: models understand and write; code decides the money.")
tab_run, tab_appeal, tab_log, tab_cost = st.tabs(["Complaint", "Appeal", "Action log", "Costs and policy"])

# ------------------------------------------------------------------ complaint
with tab_run:
    left, right = st.columns([1, 1.15], gap="large")
    with left:
        st.subheader("1 · Enter a complaint")
        st.caption("Load a sample from the activity:")
        cols = st.columns(2)
        for i, name in enumerate(P.SAMPLES):
            cols[i % 2].button(name, key=f"s{i}", on_click=load_sample, args=(name,), **W)

        f = st.session_state.form
        c1, c2, c3 = st.columns(3)
        order_id = c1.text_input("Order ID", f["order_id"])
        customer_id = c2.text_input("Customer ID", f["customer_id"])
        refunds = c3.number_input("Refunds (30d)", 0, 50, int(f["refunds_last_30_days"]))
        st.write("**Order items**")
        items_df = st.data_editor(pd.DataFrame(f["items"]), num_rows="dynamic", key="items_editor", **W,
                                  column_config={"price": st.column_config.NumberColumn("price", format="$%.2f", min_value=0.0)})
        items = [{"name": str(r["name"]), "price": float(r["price"])} for _, r in items_df.dropna().iterrows() if str(r["name"]).strip()]
        stat_grid([("Order total", f"${sum(i['price'] for i in items):.2f}")], min_px=200)
        message = st.text_area("Customer message", f["message"], height=100)
        go = st.button("▶ Process complaint", type="primary", **W)

    complaint = {"order_id": order_id, "customer_id": customer_id, "items": items,
                 "order_total": round(sum(i["price"] for i in items), 2), "refunds_last_30_days": int(refunds), "message": message}
    if go and st.session_state.runs >= MAX_RUNS:
        st.session_state.result = None
        st.session_state.error = f"Run limit reached for this session ({MAX_RUNS}). Reload the page to start a new session."
    elif go:
        with st.spinner("Processing..."):
            try:
                st.session_state.runs += 1
                st.session_state.result = P.run_pipeline(cfg, complaint)
                st.session_state.action_log.extend(st.session_state.result["executed"])
                st.session_state.complaint = complaint
                st.session_state.error = None
            except Exception as e:
                st.session_state.result = None
                st.session_state.error = f"{type(e).__name__}: {e}"

    with right:
        st.subheader("2 · Result")
        if st.session_state.get("error"):
            st.error(st.session_state.error)
            st.info("Tip: if an API fails, try 'Offline heuristic' + 'Template only' to keep the demo going.")
        r = st.session_state.result
        if r:
            d = r["decision"]
            badge = {"auto": "✅ Automatic", "flag": "🚩 Account flagged", "human": "🙋 Human agent", "none": "⛔ No refund"}[d["route"]]
            stat_grid([("Decision", badge)], min_px=300)
            stat_grid([("Cash", f"${d['cash_total']:.2f}"), ("Store credit", f"${d['credit_total']:.2f}")])
            for line in d["log"]:
                st.caption("• " + line)
            st.write("**Actions executed**")
            st.table(pd.DataFrame(r["executed"]))   # st.table wraps long cells; st.dataframe would cut them
            st.write("**Reply sent to the customer**")
            with st.chat_message("assistant"):
                st.write(r["reply"])
            stat_grid([("Cost of this complaint", f"${r['cost_usd']:.6f}"),
                       ("Projection at 40,000 / month", f"${P.monthly_cost(r['cost_usd']):,.2f}")], min_px=200)
        else:
            st.info("Load a sample or write a complaint and press **Process**.")

    if st.session_state.result:
        st.divider()
        st.subheader("3 · Every step: who ran it, what it received and what it returned")
        expand_all = st.toggle("Expand all steps", value=True)
        for s in st.session_state.result["trace"]:
            tag = "🧮" if s["runner"].startswith("Code") else ("⏭️" if s["runner"] == "Skipped" else "🤖")
            with st.expander(f"{tag} {s['step']}  ·  {s['runner']}  ·  {s['ms']} ms  ·  ${s['cost_usd']:.6f}", expanded=expand_all):
                if s.get("what"):
                    st.markdown(f"**What happened:** {s['what']}")
                if s.get("why"):
                    st.markdown(f"**Why:** {s['why']}")
                if s["note"]:
                    st.caption(s["note"])
                a, b = st.columns(2)
                a.write("**Exact input**"); a.code(json.dumps(s["input"], indent=2, ensure_ascii=False, default=str), language="json")
                b.write("**Raw output**"); b.code(json.dumps(s["output"], indent=2, ensure_ascii=False, default=str), language="json")
                if s["tokens_in"] or s["tokens_out"]:
                    st.caption(f"tokens in/out: {s['tokens_in']} / {s['tokens_out']}")

# ------------------------------------------------------------------ appeal
with tab_appeal:
    st.subheader("Appeals (~15 per day)")
    st.caption("Design rule: an appeal can uphold the decision (it is explained) or escalate to a human; "
               "the system never increases amounts on its own.")
    if not st.session_state.result:
        st.info("First process a complaint in the 'Complaint' tab.")
    else:
        txt = st.text_area("Appeal message", "I think this is unfair, please reconsider.")
        if st.button("Process appeal"):
            try:
                if st.session_state.runs >= MAX_RUNS:
                    raise RuntimeError(f"Run limit reached for this session ({MAX_RUNS}).")
                st.session_state.runs += 1
                ap = P.handle_appeal(cfg, st.session_state.complaint, st.session_state.result, txt)
                st.session_state.action_log.extend(ap["executed"])
                stat_grid([("Outcome", "Decision upheld" if ap["outcome"] == "upheld" else "Escalated to a human")], min_px=300)
                with st.chat_message("assistant"):
                    st.write(ap["reply"])
                st.caption(f"Cost: ${ap['cost_usd']:.6f}")
                for s in ap["trace"]:
                    with st.expander(f"{s['step']} · {s['runner']}", expanded=True):
                        if s.get("what"):
                            st.markdown(f"**What happened:** {s['what']}")
                        if s.get("why"):
                            st.markdown(f"**Why:** {s['why']}")
                        st.code(json.dumps({"input": s["input"], "output": s["output"]}, indent=2, ensure_ascii=False, default=str), language="json")
            except Exception as e:
                st.error(f"{type(e).__name__}: {e}")

# ------------------------------------------------------------------ log
with tab_log:
    st.subheader("Action log (mocks)")
    st.dataframe(pd.DataFrame(st.session_state.action_log) if st.session_state.action_log else pd.DataFrame({"info": ["no actions yet"]}),
                 hide_index=True, **W)

# ------------------------------------------------------------------ costs / policy
with tab_cost:
    st.subheader("Estimated cost")
    last = st.session_state.result["cost_usd"] if st.session_state.result else 0.0
    st.write(f"Last complaint processed: **${last:.6f}** → at 40,000/month: **${P.monthly_cost(last):,.2f}**")
    st.caption("Reference scenarios with ~350 input and ~100 output tokens per call:")
    rows = []
    for label, (a, b) in {"Jev (input only)": (P.PRICES["jev-latest"][0], 0.0),
                          "Claude Haiku 4.5": P.PRICES["claude-haiku-4-5"], "Claude Sonnet 4.5": P.PRICES["claude-sonnet-4-5"]}.items():
        per_call = (350 * a + 100 * b) / 1e6
        rows.append({"Model": label, "USD / call": per_call, "USD / month (40k, 1 call)": per_call * 40000})
    st.dataframe(pd.DataFrame(rows), hide_index=True, **W)
    st.subheader("Active policy (editable in code: `POLICY` in pipeline.py)")
    st.json(P.POLICY)
