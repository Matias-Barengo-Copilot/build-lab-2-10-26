"""
QuickBite Refund Desk - hybrid pipeline (code + models).

Design rule:
  - MODELS only do what needs language: understand the complaint and write the reply.
  - CODE decides the money: applies the policy, computes amounts, validates and executes.

Steps:
  1. Pre-check          (code)          rule 5: >3 refunds in 30 days -> flag
  2. Extraction         (Jev | LLM | offline heuristic)
  3. Matching + policy  (code)          rules 1-4 and 6
  4. Execution          (code, mocks)   refund / credit / flag / escalate
  5. Reply writing      (LLM, with template fallback)
  6. Validation         (code)          rule 7: the reply never promises more than was granted
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

# --------------------------------------------------------------------------
# Policy (all rule configuration lives here: changing a rule = 1 line)
# --------------------------------------------------------------------------
POLICY = {
    "max_refunds_30d": 3,       # rule 5: "more than 3" => 4 or more triggers the flag
    "goodwill_credit": 5.00,    # rule 4
    "min_confidence": 0.60,     # rule 6: below this -> human agent
}
CASH_ISSUES = {"missing", "wrong"}     # rule 1 -> cash
CREDIT_ISSUES = {"quality"}            # rule 2 -> store credit

# Prices in USD per 1M tokens (input, output). EDITABLE in the UI: verify before the demo.
PRICES = {
    "typesafe/jev-1.13": (0.042, 0.0),
    "jev-latest": (0.042, 0.0),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-5": (3.00, 15.00),
}

# --------------------------------------------------------------------------
# Config / entorno
# --------------------------------------------------------------------------


def load_env(extra_paths: list[str] | None = None) -> dict:
    """Reads a KEY=VALUE file named 'env' or '.env' (the one from the activity is named 'env')."""
    here = Path(__file__).resolve().parent
    candidates = [Path(p) for p in (extra_paths or [])]
    for base in (Path.cwd(), here, here.parent, Path.home() / "Downloads"):
        candidates += [base / ".env", base / "env"]
    loaded = {}
    for p in candidates:
        try:
            if p.is_file():
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        loaded.setdefault(k.strip(), v.strip().strip('"').strip("'"))
                loaded.setdefault("_source", str(p))
                break
        except OSError:
            continue
    for k, v in loaded.items():
        if not k.startswith("_"):
            os.environ.setdefault(k, v)
    return loaded


def get_key(*names: str) -> str:
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return ""


@dataclass
class Config:
    extractor: str = "llm"                    # "jev" | "llm" | "heuristic"
    provider: str = "anthropic"               # "anthropic" | "openrouter"  (for the LLM)
    model: str = "claude-haiku-4-5"
    writer: str = "llm"                       # "llm" | "template"
    jev_url: str = "https://openrouter.ai/api/alpha/decisions"   # OpenRouter Decisions API (uses your OpenRouter key)
    jev_model: str = "typesafe/jev-1.13"
    price_overrides: dict = field(default_factory=dict)


def price_for(model: str, cfg: Config) -> tuple[float, float]:
    if model in cfg.price_overrides:
        return cfg.price_overrides[model]
    base = model.split("/")[-1]
    return PRICES.get(model) or PRICES.get(base) or (0.0, 0.0)


def cost_of(model: str, tokens_in: int, tokens_out: int, cfg: Config) -> float:
    pin, pout = price_for(model, cfg)
    return (tokens_in * pin + tokens_out * pout) / 1_000_000


# --------------------------------------------------------------------------
# LLM client (plain HTTP: no SDKs)
# --------------------------------------------------------------------------


def call_llm(cfg: Config, system: str, user: str, max_tokens: int = 500) -> dict:
    """Returns {text, tokens_in, tokens_out, request, response}."""
    if cfg.provider == "anthropic":
        key = get_key("ANTHROPIC_API_KEY")
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        body = {"model": cfg.model, "max_tokens": max_tokens, "temperature": 0, "system": system,
                "messages": [{"role": "user", "content": user}]}
    else:
        key = get_key("OPEN_ROUTER_API_KEY", "OPENROUTER_API_KEY")
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
        body = {"model": cfg.model, "max_tokens": max_tokens, "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if not key:
        raise RuntimeError(f"Missing API key for {cfg.provider}")
    r = requests.post(url, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    j = r.json()
    if cfg.provider == "anthropic":
        text = "".join(b.get("text", "") for b in j["content"])
        tin, tout = j["usage"]["input_tokens"], j["usage"]["output_tokens"]
    else:
        text = j["choices"][0]["message"]["content"]
        tin, tout = j["usage"]["prompt_tokens"], j["usage"]["completion_tokens"]
    return {"text": text, "tokens_in": tin, "tokens_out": tout, "request": body, "response": j}


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("the model did not return JSON")
    return json.loads(m.group(0))


# --------------------------------------------------------------------------
# Traceability: each step records runner / input / raw output / cost
# --------------------------------------------------------------------------



# --------------------------------------------------------------------------
# Plain-language narration: every step says WHAT happened in this run and WHY it works this way
# --------------------------------------------------------------------------


def describe_extraction(ex: dict) -> str:
    parts = []
    for cl in ex.get("claims", []):
        who = cl.get("item") or f"'{cl.get('mentioned')}' (not an item in the order)"
        parts.append(f"{who}: {cl.get('issue')}")
    txt = "; ".join(parts) if parts else "no specific item problem"
    flags = []
    if ex.get("whole_order_unusable"):
        flags.append("the whole order is unusable")
    if ex.get("unclear"):
        flags.append("the message is unclear")
    return (f"Claims found: {txt}." + (f" Flags: {', '.join(flags)}." if flags else "")
            + f" Confidence: {ex.get('confidence')}.")


def _validation_what(reply: str, v: dict) -> str:
    quoted = ", ".join("$%.2f" % float(x.replace(",", ".")) for x in AMOUNT_RE.findall(reply))
    granted = ", ".join("$%.2f" % a for a in v["allowed_amounts"])
    amounts = (f"every amount it quotes ({quoted}) is one that was granted ({granted})" if quoted
               else "it quotes no amounts, and none were required" if not granted else "it quotes no amounts")
    return (f"Checked the reply: {amounts}; it makes no 'full refund' claim and no cash or credit promise beyond what was granted. "
            "It passed.")


def describe_decision(d: dict) -> str:
    rules = " ".join(f"{x}." if not x.endswith(".") else x for x in d["log"])
    route = {"auto": "automatic resolution", "flag": "account flagged for review", "human": "sent to a human agent",
             "none": "no refund"}[d["route"]]
    return f"{rules} Outcome: {route}; cash ${d['cash_total']:.2f}, store credit ${d['credit_total']:.2f}."


class Trace(list):
    def add(self, name, runner, inp, out, ms, model=None, tin=0, tout=0, cfg: Config | None = None, note="",
            cost_override=None, what="", why=""):
        cost = cost_override if cost_override is not None else (cost_of(model, tin, tout, cfg) if (model and cfg) else 0.0)
        self.append({"step": name, "runner": runner, "input": inp, "output": out,
                     "ms": round(ms), "tokens_in": tin, "tokens_out": tout, "cost_usd": cost, "note": note,
                     "what": what, "why": why})

    @property
    def total_cost(self) -> float:
        return sum(s["cost_usd"] for s in self)


# --------------------------------------------------------------------------
# STEP 1 - Pre-check (code)
# --------------------------------------------------------------------------


def precheck(c: dict) -> dict:
    n = int(c.get("refunds_last_30_days", 0))
    over = n > POLICY["max_refunds_30d"]
    return {"refunds_last_30_days": n, "limit": POLICY["max_refunds_30d"], "flag_account": over,
            "rule": "5" if over else None}


# --------------------------------------------------------------------------
# STEP 2 - Extraction (3 interchangeable implementations, same output)
#   output: {"claims":[{"item": str|None, "mentioned": str, "issue": missing|wrong|quality}],
#            "whole_order_unusable": bool, "unclear": bool, "confidence": float}
# --------------------------------------------------------------------------

EXTRACT_SYSTEM = """You extract structured facts from a food-delivery complaint. You do NOT decide refunds.
Return ONLY JSON:
{"claims":[{"item":"<EXACT name from the order, or null if the customer talks about something NOT in the order>",
            "mentioned":"<words the customer used for the item>",
            "issue":"missing|wrong|quality"}],
 "whole_order_unusable": true|false,   // everything ruined / nothing edible
 "unclear": true|false,                // vague: you cannot tell which item or what problem
 "confidence": 0.0-1.0}
issue: missing = never arrived; wrong = a different item arrived instead; quality = cold, soggy, late, damaged.
If whole_order_unusable is true you may leave claims empty. Never invent items."""


def extract_llm(cfg: Config, c: dict, trace: Trace) -> dict:
    items = [i["name"] for i in c["items"]]
    user = json.dumps({"order_items": items, "customer_message": c["message"]}, ensure_ascii=False)
    t = time.time()
    r = call_llm(cfg, EXTRACT_SYSTEM, user, 400)
    out = parse_json(r["text"])
    trace.add("2. Complaint extraction", f"LLM: {cfg.provider}/{cfg.model}", {"system": EXTRACT_SYSTEM, "user": json.loads(user)},
              {"raw_text": r["text"], "parsed": out}, (time.time() - t) * 1000, cfg.model, r["tokens_in"], r["tokens_out"], cfg,
              what="The model read the customer's message next to the list of order items and returned structured JSON. "
                   + describe_extraction(out),
              why="The message is free-form language that code can't parse reliably (for example 'ordered a Coke, got a Sprite'). "
                  "A small, cheap model is enough because it only classifies and never decides money. Its JSON is verified against "
                  "the real order in step 3, so a wrong item name can't turn into a payout.")
    return out


def extract_jev(cfg: Config, c: dict, trace: Trace) -> dict:
    """Jev is a classifier (Choice / Noul / Score) that returns probabilities, not text.
    That is why extraction is expressed as typed questions about each order item."""
    items = [i["name"] for i in c["items"]]
    q: dict = {}
    for k, n in enumerate(items):
        q[f"item_{k}"] = {
            "type": "choice",
            "instructions": f"What problem, if any, does the customer report about the order item '{n}'?",
            "criteria": {
                "none": f"The customer does not report a problem with '{n}'",
                "missing": f"'{n}' never arrived / was missing from the delivery",
                "wrong": f"A different item arrived instead of '{n}'",
                "quality": f"'{n}' arrived but was cold, soggy, late, damaged or otherwise poor",
            },
        }
    # The OpenRouter schema requires `criteria` with "true" and "false" keys on every noul question.
    q["unlisted_item"] = {"type": "noul",
                          "instructions": "Does the customer complain about a food item that is NOT in order_items?",
                          "criteria": {"true": "The customer talks about a food or drink that is not in order_items.",
                                       "false": "Every item the customer talks about is in order_items."}}
    q["whole_order_unusable"] = {"type": "noul",
                                 "instructions": "Is the entire order ruined, with nothing edible left?",
                                 "criteria": {"true": "The customer says everything is ruined or unusable.",
                                              "false": "Only specific items have a problem, or nothing is wrong."}}
    q["unclear"] = {"type": "noul",
                    "instructions": "Is the complaint too vague to tell which item or what problem it refers to?",
                    "criteria": {"true": "It is impossible to tell which item or what problem is meant.",
                                 "false": "The item(s) and the problem are reasonably clear."}}
    body = {"state": {"order_items": items, "customer_message": c["message"]}, "model": cfg.jev_model, "questions": q}
    # Send each key only to its own host: the OpenRouter key goes to openrouter.ai, the TypeSafe key to typesafe.ai.
    if "openrouter.ai" in cfg.jev_url:
        key = get_key("OPEN_ROUTER_API_KEY", "OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("Missing OPEN_ROUTER_API_KEY for Jev via OpenRouter")
    else:
        key = get_key("TYPESAFE_API_KEY")
        if not key:
            raise RuntimeError("Missing TYPESAFE_API_KEY: a TypeSafe endpoint needs a TypeSafe key, not an OpenRouter key")
    t = time.time()
    r = requests.post(cfg.jev_url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json=body, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Jev HTTP {r.status_code} from {cfg.jev_url}: {r.text[:300]}")
    j = r.json()
    ans = j["answers"]
    claims, confs = [], []
    for k, n in enumerate(items):
        a = ans[f"item_{k}"]
        confs.append(a["confidence"])
        if a["choice"] != "none":
            claims.append({"item": n, "mentioned": n, "issue": a["choice"]})
    noul = lambda key: ans[key]["noul"]
    unlisted = noul("unlisted_item") >= 0.5
    if unlisted:
        claims.append({"item": None, "mentioned": "(item not in order)", "issue": "missing"})
    # Noul has no 'confidence': we use the distance from 0.5 as certainty
    certainty = lambda p: abs(2 * p - 1)
    confs += [certainty(noul(k)) for k in ("unlisted_item", "whole_order_unusable", "unclear")]
    out = {"claims": claims, "whole_order_unusable": noul("whole_order_unusable") >= 0.5,
           "unclear": noul("unclear") >= 0.5, "confidence": round(min(confs), 3)}
    u = j.get("usage", {})
    trace.add("2. Complaint extraction", f"Jev ({cfg.jev_model}) - classifier", body,
              {"raw_response": j, "mapped": out}, (time.time() - t) * 1000, cfg.jev_model,
              u.get("input_tokens", 0), u.get("output_tokens", 0), cfg, cost_override=u.get("cost"),
              note="confidence = minimum across all answers; <0.6 -> human",
              what=f"Jev answered {len(q)} typed questions in one call: one choice per order item (none / missing / wrong / quality) "
                   f"plus three yes/no checks. Its probabilities were mapped to claims. " + describe_extraction(out),
              why="Extraction is classification, which is what Jev is built for: typed questions in, probabilities and a confidence "
                  "value out. It generates no text, bills input tokens only, and the confidence feeds rule 6 (low confidence goes to a human).")
    return out


_MISSING = ["missing", "never got", "didn't get", "did not get", "not there", "forgot", "never received",
            "faltó", "falto", "no llegó", "no llego", "no me llegó", "nunca me llegó", "nunca llegó"]
_QUALITY = ["cold", "soggy", "late", "damaged", "stale", "frío", "frio", "tarde", "mojad", "dañad"]
_UNUSABLE = ["couldn't eat any", "could not eat any", "all over", "nothing edible", "no pude comer nada", "desparramad"]
_VAGUE = ["not sure", "kind of off", "weird", "no sé", "no se ", "rara", "raro"]


def extract_heuristic(cfg: Config, c: dict, trace: Trace) -> dict:
    """Offline keyword fallback. Brittle on purpose: it shows WHY a model is used."""
    t = time.time()
    msg = c["message"].lower()
    items = [i["name"] for i in c["items"]]
    def mentions(name: str, text: str) -> bool:
        toks = [w for w in re.findall(r"\w+", name.lower()) if len(w) > 2]
        return name.lower() in text or any(w in re.findall(r"\w+", text) for w in toks)

    claims = []
    for n in items:
        toks = "|".join(re.escape(w) for w in re.findall(r"\w+", n.lower()) if len(w) > 2)
        if re.search(r"ordered (?:an? |the )?(?:" + toks + r").{0,20}(?:got|received|came)", msg):
            claims.append({"item": n, "mentioned": n, "issue": "wrong"})
    for clause in re.split(r",|;|\.| and | but | y | pero ", msg):
        if not clause.strip():
            continue
        hit = next((n for n in items if mentions(n, clause)), None)
        issue = ("missing" if any(w in clause for w in _MISSING) else
                 "quality" if any(w in clause for w in _QUALITY) else None)
        if issue and hit and not any(cl["item"] == hit for cl in claims):
            claims.append({"item": hit, "mentioned": hit, "issue": issue})
        elif issue == "missing" and not hit:
            m = re.search(r"(?:my|the|mi|mis|la|el)\s+(\w+)", clause)
            claims.append({"item": None, "mentioned": m.group(1) if m else "?", "issue": "missing"})
    unusable = any(w in msg for w in _UNUSABLE)
    unclear = not claims and not unusable
    out = {"claims": claims, "whole_order_unusable": unusable, "unclear": unclear, "confidence": 0.7 if not unclear else 0.3}
    trace.add("2. Complaint extraction", "Code (keyword heuristic, offline)",
              {"order_items": items, "message": c["message"]}, out, (time.time() - t) * 1000,
              note="No-API fallback. Do not use in production.",
              what="Keyword rules split the message into clauses and matched each clause to an order item and a problem word. "
                   + describe_extraction(out),
              why="Offline plan B: it needs no API and costs nothing, so the demo survives an outage. It is brittle on purpose, which is "
                  "the reason production uses a model for this step.")
    return out


def extract(cfg: Config, c: dict, trace: Trace) -> dict:
    return {"jev": extract_jev, "llm": extract_llm, "heuristic": extract_heuristic}[cfg.extractor](cfg, c, trace)


# --------------------------------------------------------------------------
# STEP 3 - Matching + policy engine (pure code)
# --------------------------------------------------------------------------


def decide(c: dict, ex: dict) -> dict:
    t = time.time()
    price = {i["name"].lower(): float(i["price"]) for i in c["items"]}
    names = {i["name"].lower(): i["name"] for i in c["items"]}
    cash: dict[str, float] = {}
    credit: dict[str, float] = {}
    declined: list[dict] = []
    log: list[str] = []

    if ex.get("unclear") or ex.get("confidence", 1) < POLICY["min_confidence"]:
        return {"route": "human", "actions": [], "declined": [], "cash_total": 0.0, "credit_total": 0.0,
                "log": [f"Rule 6: ambiguous or confidence {ex.get('confidence')} < {POLICY['min_confidence']} -> human agent"]}

    for cl in ex.get("claims", []):
        key = (cl.get("item") or "").lower()
        if key not in price:                                      # rule 3 (and guard against model hallucination)
            declined.append({"mentioned": cl.get("mentioned"), "reason": "not_in_order"})
            log.append(f"Rule 3: '{cl.get('mentioned')}' is not in the order -> no refund")
            continue
        nm = names[key]
        if cl["issue"] in CASH_ISSUES:                            # rule 1
            cash[nm] = price[key]; credit.pop(nm, None)
            log.append(f"Rule 1: {nm} ({cl['issue']}) -> cash ${price[key]:.2f}")
        elif cl["issue"] in CREDIT_ISSUES and nm not in cash:     # rule 2
            credit[nm] = price[key]
            log.append(f"Rule 2: {nm} (quality) -> credit ${price[key]:.2f}")

    goodwill = 0.0
    if ex.get("whole_order_unusable"):                            # rule 4
        for k, n in names.items():
            if n not in cash and n not in credit:
                credit[n] = price[k]
        goodwill = POLICY["goodwill_credit"]
        log.append(f"Rule 4: whole order unusable -> credit for all items + ${goodwill:.2f} goodwill")

    if not cash and not credit and not declined:
        return {"route": "human", "actions": [], "declined": [], "cash_total": 0.0, "credit_total": 0.0,
                "log": ["Rule 6: no concrete claim detected -> human agent"]}

    actions = []
    if cash:
        actions.append({"type": "cash_refund", "amount": round(sum(cash.values()), 2), "items": list(cash)})
    if credit or goodwill:
        actions.append({"type": "store_credit", "amount": round(sum(credit.values()) + goodwill, 2),
                        "items": list(credit), "goodwill": goodwill})
    cash_total = round(sum(cash.values()), 2)
    credit_total = round(sum(credit.values()) + goodwill, 2)
    return {"route": "auto" if actions else "none", "actions": actions, "declined": declined,
            "cash_total": cash_total, "credit_total": credit_total, "log": log,
            "_ms": (time.time() - t) * 1000}


# --------------------------------------------------------------------------
# STEP 4 - Execution (mocks that log the action)
# --------------------------------------------------------------------------
# Mock executors only BUILD the log entry; the caller owns the log, so visitors of a deployed app never see each other's actions.


def _log(kind, **kw):
    e = {"ts": time.strftime("%H:%M:%S"), "action": kind, **kw}
    return e


def issue_cash_refund(order_id, customer_id, amount): return _log("CASH_REFUND", order_id=order_id, customer_id=customer_id, amount=amount)
def issue_store_credit(customer_id, amount): return _log("STORE_CREDIT", customer_id=customer_id, amount=amount)
def flag_account(customer_id, reason): return _log("FLAG_ACCOUNT", customer_id=customer_id, reason=reason)
def escalate_to_agent(order_id, reason): return _log("ESCALATE_TO_AGENT", order_id=order_id, reason=reason)


def execute(c: dict, decision: dict) -> list[dict]:
    done = []
    if decision["route"] == "flag":
        done.append(flag_account(c["customer_id"], f">{POLICY['max_refunds_30d']} refunds in 30d"))
    elif decision["route"] == "human":
        done.append(escalate_to_agent(c["order_id"], "; ".join(decision["log"])))
    for a in decision["actions"]:
        if a["type"] == "cash_refund":
            done.append(issue_cash_refund(c["order_id"], c["customer_id"], a["amount"]))
        else:
            done.append(issue_store_credit(c["customer_id"], a["amount"]))
    return done


# --------------------------------------------------------------------------
# STEP 5/6 - Reply writing (LLM) + rule 7 validation (code) + template fallback
# --------------------------------------------------------------------------
WRITER_SYSTEM = """You are QuickBite customer support. Write a SHORT, warm reply (max 90 words) to the customer.
Reply in the same language as the customer's message. Use ONLY the facts in DECISION.
- Mention only the amounts present in DECISION, written exactly like "$9.00".
- Never promise or imply anything not in DECISION (no 'full refund', no extra credit, no future compensation).
- If part of the request was not granted (declined items) or the case goes to a human/review, say so politely and clearly.
- Do not mention internal rule numbers or policies by number. No emojis. Output only the message."""

AMOUNT_RE = re.compile(r"\$\s?(\d+(?:[.,]\d{1,2})?)")


def allowed_amounts(decision: dict) -> set[float]:
    s = {decision["cash_total"], decision["credit_total"], round(decision["cash_total"] + decision["credit_total"], 2)}
    for a in decision["actions"]:
        s.add(a["amount"])
        if a.get("goodwill"):
            s.add(a["goodwill"])
    return {x for x in s if x > 0}


def validate_reply(reply: str, decision: dict) -> dict:
    """Rule 7. Deterministic checks: (a) every amount quoted was actually granted,
    (b) no talk of cash/refund if there was no cash, (c) no talk of credit if there was none."""
    problems = []
    ok_amounts = allowed_amounts(decision)
    for m in AMOUNT_RE.findall(reply):
        v = round(float(m.replace(",", ".")), 2)
        if v not in ok_amounts:
            problems.append(f"amount ${v:.2f} was not granted")
    low = reply.lower()
    if decision["cash_total"] == 0 and re.search(r"\b(cash refund|refunded|reembolsad|reembolso en efectivo|devolv)", low):
        problems.append("promises a cash refund that does not exist")
    if decision["credit_total"] == 0 and re.search(r"(store credit|crédito|credito)", low):
        problems.append("promises credit that does not exist")
    if re.search(r"(full refund|reembolso completo|reembolso total)", low):
        problems.append("promises a full refund")
    return {"ok": not problems, "problems": problems, "allowed_amounts": sorted(ok_amounts)}


def _is_es(msg: str) -> bool:
    return bool(re.search(r"\b(mi|me|llegó|llego|pedí|pedi|nunca|comida|bolsa|faltó|falto|otra vez|pizza)\b|[áéíóúñ¿¡]", msg.lower())) \
        and not re.search(r"\b(my|the|i|was|were)\b", msg.lower())


def template_reply(c: dict, d: dict) -> str:
    es = _is_es(c["message"])
    parts = []
    if d["route"] == "flag":
        return ("Gracias por escribirnos. Tu cuenta quedó en revisión y un agente de soporte se pondrá en contacto contigo; "
                "por ahora no podemos procesar un reembolso automático.") if es else \
               ("Thanks for reaching out. Your account has been flagged for review and a support agent will contact you; "
                "we can't process an automatic refund right now.")
    if d["route"] == "human":
        return ("Gracias por escribirnos. Un agente de soporte revisará tu caso personalmente y te responderá pronto."
                if es else "Thanks for reaching out. A support agent will review your case personally and get back to you soon.")
    if d["cash_total"]:
        parts.append(f"Te reembolsamos ${d['cash_total']:.2f} en efectivo." if es else f"We've refunded ${d['cash_total']:.2f} in cash.")
    if d["credit_total"]:
        parts.append(f"Te acreditamos ${d['credit_total']:.2f} en crédito de la tienda." if es else f"We've added ${d['credit_total']:.2f} in store credit.")
    if d["declined"]:
        parts.append("No encontramos en tu pedido lo que mencionas, así que no podemos reembolsarlo." if es
                     else "We couldn't find the item you mention in your order, so we can't refund it.")
    if not parts:
        parts.append("No encontramos nada en tu pedido que corresponda a un reembolso." if es
                     else "We couldn't find anything in your order eligible for a refund.")
    return (("Lamentamos el inconveniente. " if es else "Sorry about the trouble. ") + " ".join(parts))


def write_reply(cfg: Config, c: dict, d: dict, trace: Trace, extra_context: str = "") -> str:
    facts = {"route": d["route"], "cash_refund_total": d["cash_total"], "store_credit_total": d["credit_total"],
             "actions": d["actions"], "declined_items": d["declined"]}
    if extra_context:
        facts["context"] = extra_context
    if cfg.writer == "template" or d["route"] in ("flag", "human") and cfg.writer != "llm_always":
        # Routes with no money: a template is 100% safe and free -> no LLM spend.
        reply = template_reply(c, d)
        trace.add("5. Reply writing", "Code (template)", facts, {"reply": reply}, 0,
                  note="No money at stake: deterministic template, cost $0",
                  what=f"Used a fixed template for route '{d['route']}' (language detected from the customer's message).",
                  why=("No money is being granted on this route, so nothing creative is needed. A template cannot over-promise and "
                       "costs $0, so the LLM call is skipped." if d["route"] in ("flag", "human") else
                       "Template-only mode is selected in the sidebar, so no model was called."))
        return reply
    user = json.dumps({"DECISION": facts, "customer_message": c["message"]}, ensure_ascii=False)
    t = time.time()
    r = call_llm(cfg, WRITER_SYSTEM, user, 300)
    reply = r["text"].strip()
    trace.add("5. Reply writing", f"LLM: {cfg.provider}/{cfg.model}", {"system": WRITER_SYSTEM, "user": json.loads(user)},
              {"reply": reply}, (time.time() - t) * 1000, cfg.model, r["tokens_in"], r["tokens_out"], cfg,
              what=f"The model received only the final decision (cash ${d['cash_total']:.2f}, credit ${d['credit_total']:.2f}, "
                   f"{len(d['declined'])} declined item(s)) and the customer's message, and wrote a {len(reply.split())}-word reply "
                   "in the customer's language.",
              why="A warm reply in the customer's own language is a language task, so a model fits. It never sees the policy or the "
                  "prices, only the outcome, so it has no numbers to invent. Step 6 still checks the result.")
    return reply


# --------------------------------------------------------------------------
# Orquestador
# --------------------------------------------------------------------------


def run_pipeline(cfg: Config, c: dict) -> dict:
    trace = Trace()
    # 1
    t = time.time()
    pre = precheck(c)
    trace.add("1. Refund pre-check (30d)", "Code (Python)", {"refunds_last_30_days": c["refunds_last_30_days"]}, pre,
              (time.time() - t) * 1000, note="Rule 5. If it triggers, the extraction LLM is skipped (saves money).",
              what=(f"Customer {c['customer_id']} has {pre['refunds_last_30_days']} refund{'s' if pre['refunds_last_30_days'] != 1 else ''} in the last 30 days; the limit is "
                    f"{pre['limit']}. " + ("That is above the limit, so the account is flagged and the model is not called."
                                           if pre["flag_account"] else "That is within the limit, so the complaint continues to extraction.")),
              why="Rule 5 is a number compared with a threshold. Plain code is exact, free and instant; a model would add cost and a "
                  "chance of error to a comparison. Running it first also saves the extraction call on accounts that will be flagged anyway.")
    if pre["flag_account"]:
        decision = {"route": "flag", "actions": [], "declined": [], "cash_total": 0.0, "credit_total": 0.0,
                    "log": [f"Rule 5: {pre['refunds_last_30_days']} refunds in 30d (> {pre['limit']}) -> no automatic refund, account flagged"]}
        ex = None
        trace.add("2. Complaint extraction", "Skipped", {"reason": "account flagged in step 1"}, {"skipped": True}, 0,
                  what="Not run: step 1 already flagged the account, so there is nothing to classify.",
                  why="Skipping saves one model call per flagged complaint and removes any chance of a model changing "
                      "an outcome that rule 5 has already fixed.")
    else:
        ex = extract(cfg, c, trace)
        decision = decide(c, ex)
    trace.add("3. Matching + policy engine", "Code (Python, rules in POLICY)", {"extraction": ex, "items": c["items"]},
              decision, decision.pop("_ms", 0), note="ALL the money is computed here. The model never sees amounts to decide.",
              what=describe_decision(decision),
              why="This is where money is decided, so it has to be deterministic, testable and auditable. Each claimed item is looked up "
                  "in the real order, so a model can't pay out for an item that doesn't exist, and every price comes from the order, "
                  "never from the model. Changing a rule means editing one line in POLICY.")
    # 4
    t = time.time()
    done = execute(c, decision)
    trace.add("4. Action execution", "Code (mock functions)", {"actions": decision["actions"], "route": decision["route"]},
              done, (time.time() - t) * 1000,
              what=("Ran " + (", ".join(f"{e['action']}" + (f" ${e['amount']:.2f}" if 'amount' in e else "") for e in done) or "no actions")
                    + ". Each one is a mock function that writes to the action log."),
              why="A decision only matters if something happens. Each action is its own small function, so it can be swapped for a "
                  "real payments or CRM call later, and every one is logged for audit (see the Action log tab).")
    # 5 + 6
    try:
        reply = write_reply(cfg, c, decision, trace)
    except Exception as e:                       # API down -> template
        reply = template_reply(c, decision)
        trace.add("5b. Writing fallback", "Code (template)", {"error": str(e)}, {"reply": reply}, 0,
                  what=f"The writing call failed ({type(e).__name__}), so the safe template was used instead.",
                  why="An API outage must not stop the customer from getting a correct answer, and a template can't over-promise.")
    t = time.time()
    v = validate_reply(reply, decision)
    if not v["ok"]:
        bad = reply
        reply = template_reply(c, decision)
        trace.add("6. Validation (rule 7)", "Code (regex)", {"reply": bad, "allowed_amounts": v["allowed_amounts"]},
                  {**v, "action": "reply rejected -> safe template sent", "final_reply": reply}, (time.time() - t) * 1000,
                  what="The reply failed the check (" + "; ".join(v["problems"]) + "), so it was discarded and the safe template was sent.",
                  why="Rule 7: a reply must never promise more than the customer received. A model can't be trusted to follow that "
                      "every time, so code enforces it. The check is cheap, deterministic and can't be talked around.")
    else:
        trace.add("6. Validation (rule 7)", "Code (regex)", {"reply": reply, "allowed_amounts": v["allowed_amounts"]}, v,
                  (time.time() - t) * 1000,
                  what=_validation_what(reply, v),
                  why="Rule 7: a reply must never promise more than the customer received. A model can't be trusted to follow that "
                      "every time, so code enforces it. The check is cheap, deterministic and can't be talked around.")
    return {"decision": decision, "extraction": ex, "executed": done, "reply": reply, "trace": trace,
            "cost_usd": trace.total_cost}


# --------------------------------------------------------------------------
# Appeals: they never increase the amount automatically
# --------------------------------------------------------------------------


def handle_appeal(cfg: Config, c: dict, original: dict, appeal_text: str) -> dict:
    trace = Trace()
    c2 = {**c, "message": f"{c['message']}\n[APPEAL] {appeal_text}"}
    pre = precheck(c)
    trace.add("A1. Original decision context", "Code", {"original_route": original["decision"]["route"]},
              {"cash": original["decision"]["cash_total"], "credit": original["decision"]["credit_total"]}, 0,
              what=f"Loaded the original decision: route '{original['decision']['route']}', cash ${original['decision']['cash_total']:.2f}, "
                   f"credit ${original['decision']['credit_total']:.2f}.",
              why="An appeal is judged against what the customer actually received, so the first thing the system needs is that record.")
    if pre["flag_account"]:
        new = original["decision"]
    else:
        ex = extract(cfg, c2, trace)
        new = decide(c2, ex)
        new.pop("_ms", None)
    more = (new["cash_total"] + new["credit_total"]) > (original["decision"]["cash_total"] + original["decision"]["credit_total"]) + 1e-9
    if more or new["route"] == "human":
        d = {"route": "human", "actions": [], "declined": [], "cash_total": 0.0, "credit_total": 0.0,
             "log": ["Appeal with new facts or ambiguous: a human decides; the system never increases amounts on its own."]}
        outcome = "escalated"
    else:
        d = original["decision"]
        outcome = "upheld"
    trace.add("A2. Appeal rule", "Code", {"new_total": new["cash_total"] + new["credit_total"]},
              {"outcome": outcome, "log": d["log"]}, 0, note="Uphold = explain. More money = human.",
              what=(f"Re-ran the engine with the appeal text. The new total is ${new['cash_total'] + new['credit_total']:.2f} against "
                    f"${original['decision']['cash_total'] + original['decision']['credit_total']:.2f} originally, so the outcome is "
                    + ("'escalated': a human will decide." if outcome == "escalated" else "'upheld': the decision stays and is explained.")),
              why="The system never raises an amount by itself, because an appeal is exactly where a customer has the most reason to push. "
                  "More money, or new facts, needs a person. Without new facts, a clear written explanation is the right answer.")
    ctx = ("The customer appealed. The original decision stands; explain it kindly and clearly, restate only the original amounts."
           if outcome == "upheld" else "The customer appealed with new information; a human agent will review it.")
    if d["route"] == "human":
        reply = template_reply(c2, d)
        trace.add("A3. Appeal reply", "Code (template)", {"route": "human"}, {"reply": reply}, 0,
                  what="Used the human-review template to tell the customer a person will look at the appeal.",
                  why="Nothing is being granted, so no model is needed; the template can't promise anything.")
    else:
        try:
            reply = write_reply(cfg, c2, d, trace, extra_context=ctx)
        except Exception:
            reply = template_reply(c2, d)
        if not validate_reply(reply, d)["ok"]:
            reply = template_reply(c2, d)
    executed = execute(c, d) if outcome == "escalated" else []
    return {"outcome": outcome, "reply": reply, "trace": trace, "cost_usd": trace.total_cost, "executed": executed}


# --------------------------------------------------------------------------
# Costos
# --------------------------------------------------------------------------
MONTHLY_COMPLAINTS = 40_000
DAILY_APPEALS = 15


def monthly_cost(per_complaint: float, per_appeal: float = 0.0) -> float:
    return per_complaint * MONTHLY_COMPLAINTS + per_appeal * DAILY_APPEALS * 30


# --------------------------------------------------------------------------
# Samples from the activity
# --------------------------------------------------------------------------
BASE_ITEMS = [{"name": "Chicken burger", "price": 9.00}, {"name": "Fries", "price": 3.50}, {"name": "Coke", "price": 2.00}]
SAMPLES = {
    "Base example: missing burger + cold fries": ("C-12", 1, "My burger was missing and the fries were cold. I want a full refund."),
    "Pizza that never arrived (not in the order)": ("C-12", 1, "I never got my pizza."),
    "Ordered Coke, got Sprite": ("C-12", 1, "Ordered a Coke, got a Sprite."),
    "Vague: 'the food was kind of off'": ("C-12", 1, "The food was kind of off, not sure."),
    "Split bag: whole order unusable": ("C-12", 1, "Bag split open, food all over it, couldn't eat any of it."),
    "Customer with 5 refunds": ("C-40", 5, "Burger missing again."),
}


def sample_complaint(name: str) -> dict:
    cid, n, msg = SAMPLES[name]
    items = [dict(i) for i in BASE_ITEMS]
    return {"order_id": "O-551", "customer_id": cid, "items": items, "order_total": sum(i["price"] for i in items),
            "refunds_last_30_days": n, "message": msg}
