# QuickBite Refund Desk (Build Lab)

    pip install -r requirements.txt
    streamlit run app.py          # reads keys from the `env` / `.env` file (ANTHROPIC_API_KEY, OPEN_ROUTER_API_KEY)
    pip install -r requirements-dev.txt && pytest -q    # policy-engine tests, no API needed

Files: `pipeline.py` (all logic), `app.py` (UI), `test_pipeline.py`.
Changing a policy rule = editing `POLICY` / `CASH_ISSUES` / `CREDIT_ISSUES` in `pipeline.py`.
Plan B without internet: in the sidebar choose "Offline heuristic" + "Template only".
NEVER commit the `env` file to git.

Deployment: see `DEPLOY.md` (Streamlit Community Cloud for the demo, Azure Container Apps for team use).
