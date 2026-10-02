"""Policy engine tests using the offline extractor (no API keys required)."""
import pytest
import pipeline as P

CFG = P.Config(extractor="heuristic", writer="template")

def run(name):
    return P.run_pipeline(CFG, P.sample_complaint(name))

def test_base_burger_cash_fries_credit():
    r = run("Base example: missing burger + cold fries")
    d = r["decision"]
    assert d["cash_total"] == 9.00 and d["credit_total"] == 3.50 and d["route"] == "auto"
    assert "$14.50" not in r["reply"]          # does not promise a "full refund"

def test_pizza_not_in_order_no_refund():
    d = run("Pizza that never arrived (not in the order)")["decision"]
    assert d["cash_total"] == 0 and d["credit_total"] == 0 and d["declined"]

def test_wrong_item_coke_cash():
    d = run("Ordered Coke, got Sprite")["decision"]
    assert d["cash_total"] == 2.00 and d["credit_total"] == 0

def test_vague_goes_to_human():
    assert run("Vague: 'the food was kind of off'")["decision"]["route"] == "human"

def test_bag_split_credit_all_plus_goodwill():
    d = run("Split bag: whole order unusable")["decision"]
    assert d["cash_total"] == 0 and d["credit_total"] == pytest.approx(14.50 + 5.00)

def test_five_refunds_flag_no_money():
    r = run("Customer with 5 refunds")
    assert r["decision"]["route"] == "flag" and r["decision"]["cash_total"] == 0
    step2 = [s for s in r["trace"] if s["step"].startswith("2.")]
    assert step2 and step2[0]["runner"] == "Skipped" and step2[0]["cost_usd"] == 0   # extraction LLM skipped, and says so

def test_boundary_three_refunds_is_not_flagged():
    c = P.sample_complaint("Base example: missing burger + cold fries"); c["refunds_last_30_days"] = 3
    assert P.run_pipeline(CFG, c)["decision"]["route"] == "auto"

def test_validator_rejects_overpromise():
    d = {"cash_total": 9.0, "credit_total": 3.5, "actions": [{"amount": 9.0}, {"amount": 3.5}]}
    assert not P.validate_reply("We refunded $14.50 in full.", d)["ok"]
    assert not P.validate_reply("You get a full refund!", d)["ok"]
    assert P.validate_reply("Refunded $9.00 and $3.50 in store credit.", d)["ok"]

def test_validator_no_cash_no_cash_talk():
    d = {"cash_total": 0.0, "credit_total": 3.5, "actions": [{"amount": 3.5}]}
    assert not P.validate_reply("We refunded you $3.50", d)["ok"]

def test_hallucinated_item_name_is_declined():
    c = P.sample_complaint("Pizza that never arrived (not in the order)")
    ex = {"claims": [{"item": "Pizza", "mentioned": "pizza", "issue": "missing"}], "whole_order_unusable": False, "unclear": False, "confidence": 0.9}
    assert P.decide(c, ex)["cash_total"] == 0

def test_low_confidence_goes_to_human():
    c = P.sample_complaint("Base example: missing burger + cold fries")
    ex = {"claims": [{"item": "Fries", "mentioned": "fries", "issue": "missing"}], "whole_order_unusable": False, "unclear": False, "confidence": 0.4}
    assert P.decide(c, ex)["route"] == "human"

def test_appeal_never_increases_money():
    c = P.sample_complaint("Base example: missing burger + cold fries")
    orig = P.run_pipeline(CFG, c)
    a = P.handle_appeal(CFG, c, orig, "My coke was also missing!")
    assert a["outcome"] == "escalated"
    a2 = P.handle_appeal(CFG, c, orig, "Please reconsider.")
    assert a2["outcome"] in ("upheld", "escalated")


def test_jev_request_shape_and_key_routing(monkeypatch):
    """Jev goes to OpenRouter with the OpenRouter key; every noul question carries true/false criteria."""
    import os
    seen = {}

    class R:
        ok = True; status_code = 200; text = ""
        def json(self):
            return {"model": "typesafe/jev-1.13-20260917", "answers": {
                "item_0": {"type": "choice", "choice": "missing", "confidence": 0.95, "probabilities": {}},
                "item_1": {"type": "choice", "choice": "quality", "confidence": 0.9, "probabilities": {}},
                "item_2": {"type": "choice", "choice": "none", "confidence": 0.99, "probabilities": {}},
                "unlisted_item": {"type": "noul", "noul": 0.02}, "whole_order_unusable": {"type": "noul", "noul": 0.01},
                "unclear": {"type": "noul", "noul": 0.03}}, "usage": {"input_tokens": 700, "output_tokens": 60, "cost": 2.94e-05}}

    def fake_post(url, headers=None, json=None, timeout=0):
        seen.update(url=url, auth=headers["Authorization"], body=json); return R()

    monkeypatch.setattr(P.requests, "post", fake_post)
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "or-key"); monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key")
    cfg = P.Config(extractor="jev", writer="template")
    r = P.run_pipeline(cfg, P.sample_complaint("Base example: missing burger + cold fries"))
    assert seen["url"] == "https://openrouter.ai/api/alpha/decisions" and seen["auth"] == "Bearer or-key"
    assert seen["body"]["model"] == "typesafe/jev-1.13"
    for k, q in seen["body"]["questions"].items():
        if q["type"] == "noul":
            assert set(q["criteria"]) == {"true", "false"}, k
    assert r["decision"]["cash_total"] == 9.0 and r["decision"]["credit_total"] == 3.5
    assert abs(r["cost_usd"] - 2.94e-05) < 1e-9          # uses the cost reported by OpenRouter


def test_typesafe_endpoint_never_gets_openrouter_key(monkeypatch):
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "or-key"); monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    cfg = P.Config(extractor="jev", jev_url="https://api.typesafe.ai/v1/systemone")
    with pytest.raises(RuntimeError, match="TypeSafe key"):
        P.extract_jev(cfg, P.sample_complaint("Base example: missing burger + cold fries"), P.Trace())


def test_every_step_explains_what_and_why():
    for name in P.SAMPLES:
        r = P.run_pipeline(CFG, P.sample_complaint(name))
        for s in r["trace"]:
            assert len(s["what"]) > 20 and len(s["why"]) > 20, (name, s["step"])


def test_action_logs_are_not_shared_between_runs():
    a = P.run_pipeline(CFG, P.sample_complaint("Base example: missing burger + cold fries"))
    b = P.run_pipeline(CFG, P.sample_complaint("Customer with 5 refunds"))
    assert [e["action"] for e in a["executed"]] == ["CASH_REFUND", "STORE_CREDIT"]
    assert [e["action"] for e in b["executed"]] == ["FLAG_ACCOUNT"]      # nothing leaked from run a
    assert not hasattr(P, "ACTION_LOG")
