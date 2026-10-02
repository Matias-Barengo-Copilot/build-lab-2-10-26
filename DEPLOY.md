# Deploying the Refund Desk

## Which platform, and why

What the code needs: a single Python process running Streamlit, outbound HTTPS to the model APIs, a few secrets,
and nothing else. There is no database and no file storage, and every visitor's state lives in their own session.
What it must protect: the API keys, and the credits those keys spend every time someone presses *Process*.

| Option | Use it for | Why | Watch out for |
|---|---|---|---|
| **Streamlit Community Cloud** | The Build Lab demo | Free, deploys straight from a GitHub repo, secrets pasted into a settings box, no Docker. About 10 minutes. | The free tier allows **one private app at a time**. A public repo makes a public app, so use a private repo and invite viewers by email. |
| **Azure Container Apps** (recommended for anything after the lab) | Team or client use | Runs the included Dockerfile. Built-in Microsoft Entra ID sign-in can require login for the whole app with no code changes. Secrets live in the platform, not in the image. | Needs an Azure subscription, a container registry and about 30 to 45 minutes the first time. |
| Hugging Face Spaces, Render, Railway | Alternatives | Also run a Streamlit app or a Docker image. | Not evaluated here. Check their access control before putting paid keys on them. |

**Recommendation:** use Streamlit Community Cloud for the live session, because the activity is a 90-minute internal exercise.
Move to Azure Container Apps with Entra sign-in if the team keeps using it. The same code runs on both.

## What was changed in the code for deployment

- Keys are read from the platform's secrets or environment variables, and from the local `env` file only when running locally.
- `APP_PASSWORD` (optional) puts a password screen in front of the app. Set it on any deployment that is reachable from the internet.
- `MAX_RUNS_PER_SESSION` (default 40) caps how many complaints one browser session can process.
- The action log is now per visitor. Before, it was a module-level list that every visitor would have shared.
- `Dockerfile`, `.dockerignore` and `.streamlit/config.toml` (headless, no usage stats, XSRF protection) were added.
  The `env` file and the tests are excluded from the image.

## A. Streamlit Community Cloud

1. Create a **private** GitHub repository and push this folder. Check that `env` is not included: `.gitignore` excludes it.
2. Go to share.streamlit.io, choose *New app*, select the repository, branch `main`, main file `app.py`.
3. Open *Advanced settings* and paste the contents of `.streamlit/secrets.toml.example` with your real values.
   Set `APP_PASSWORD` to a long shared password.
4. Deploy. After it is live, use *Share* to invite the viewers' email addresses.
5. Open the app and run sample 1. In the sidebar the key indicators should show a check mark.

## B. Azure Container Apps

    # one-time
    az group create -n rg-refunddesk -l eastus
    az acr create -n <uniqueRegistryName> -g rg-refunddesk --sku Basic
    az containerapp env create -n env-refunddesk -g rg-refunddesk -l eastus

    # build in the cloud and deploy (run inside the refund_desk folder)
    az acr build -r <uniqueRegistryName> -t refunddesk:1 .
    az containerapp create -n refunddesk -g rg-refunddesk --environment env-refunddesk \
      --image <uniqueRegistryName>.azurecr.io/refunddesk:1 --registry-server <uniqueRegistryName>.azurecr.io \
      --target-port 8501 --ingress external \
      --secrets anthropic=<ANTHROPIC_KEY> openrouter=<OPENROUTER_KEY> apppw=<PASSWORD> \
      --env-vars ANTHROPIC_API_KEY=secretref:anthropic OPEN_ROUTER_API_KEY=secretref:openrouter APP_PASSWORD=secretref:apppw

    # require Microsoft sign-in for the whole app (no code changes)
    az containerapp auth microsoft update -n refunddesk -g rg-refunddesk \
      --client-id <APP_ID> --client-secret <CLIENT_SECRET> --tenant-id <TENANT_ID> --yes
    az containerapp auth update -n refunddesk -g rg-refunddesk --unauthenticated-client-action RedirectToLoginPage

Check the commands against the current Azure CLI before running them. This sandbox has no Azure or Docker access,
so the image has not been built here.

## Before sharing the link

- Rotate any key that was ever pasted into a chat or committed by mistake.
- Set a spending limit on the Anthropic and OpenRouter keys.
- Keep a local copy running as plan B ("Offline heuristic" + "Template only" needs no internet).
- Jev runs on OpenRouter's alpha Decisions API, which can change. Test it on the deployed URL before the session.
