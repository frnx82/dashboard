# GitHub OAuth Setup Guide — Pipeline Hub

## Why OAuth?

When Pipeline Hub uses a **shared service account token (PAT)** to trigger workflows, GitHub attributes every run to that service account — you lose track of **who actually triggered it**.

With **GitHub OAuth**, each user logs into Pipeline Hub with their own GitHub identity. Pipeline Hub then uses **their token** to call the GitHub API, so:

- ✅ GitHub shows the **real person** who triggered each workflow run
- ✅ Each user's **repo/workflow permissions** are automatically enforced
- ✅ No shared PAT to manage or rotate
- ✅ Full audit trail in GitHub's own activity logs

---

## Requirements

| Requirement | Difficulty | Notes |
|---|---|---|
| Create a GitHub OAuth App | 🟢 Easy (~2 min) | One-time setup in GitHub |
| LDAP / SSO changes | ❌ Not needed | OAuth is built into GitHub |
| Identity Provider changes | ❌ Not needed | No Okta/Azure AD config |
| Org admin approval (conditional) | 🟡 Maybe one-click | Only if your org restricts OAuth apps |

---

## Step 1 — Create a GitHub OAuth App

### Who can do this?

- Any **GitHub Org Admin**, or
- Any user on their personal account (for local development/testing)

### Where to create it

Navigate to:

```
GitHub → Your Org (or Profile) → Settings → Developer settings → OAuth Apps → New OAuth App
```

Direct link: `https://github.com/organizations/YOUR_ORG/settings/applications/new`

### Fill in the form

| Field | Development Value | Production Value |
|---|---|---|
| **Application name** | `Pipeline Hub (Dev)` | `Pipeline Hub` |
| **Homepage URL** | `http://localhost:9090` | `https://pipeline-hub.yourcompany.com` |
| **Authorization callback URL** | `http://localhost:9090/auth/callback` | `https://pipeline-hub.yourcompany.com/auth/callback` |

> [!IMPORTANT]
> The **callback URL must match exactly** — including the protocol (`http` vs `https`) and port. If it doesn't match, GitHub will reject the OAuth flow.

### Save the credentials

After creating the app, GitHub displays:

- **Client ID** — e.g. `Ov23liABCDEF123456` (public, safe to store in config)
- **Client Secret** — click "Generate a new client secret" (keep this secret!)

> [!CAUTION]
> Store the **Client Secret** securely. Treat it like a password. Never commit it to source control.

---

## Step 2 — Check Your Org's OAuth Policy

Navigate to:

```
GitHub Org → Settings → Third-party access → OAuth App access policy
```

| Policy Setting | What It Means | Action Needed |
|---|---|---|
| **No restrictions** | ✅ Any org member can authorize Pipeline Hub immediately | None |
| **Access restricted** | ⚠️ An admin must approve Pipeline Hub before members can use it | Ask an admin to approve the app (one-click) |

### If your org has "Access restricted" enabled

1. Go to **Org Settings → Third-party access**
2. Find **Pipeline Hub** in the list of requested apps
3. Click **Grant** or **Approve**

This is a **one-time approval**. After that, all org members can use Pipeline Hub.

---

## Step 3 — Configure Pipeline Hub

### Environment Variables

Replace the old `GITHUB_TOKEN` with these variables:

```bash
# Required — from the OAuth App you created
GITHUB_CLIENT_ID=Ov23liABCDEF123456
GITHUB_CLIENT_SECRET=your_client_secret_here

# Required — random string for signing session cookies
FLASK_SECRET_KEY=generate-a-random-string-here

# Optional — restrict to a specific GitHub org
GITHUB_ORG=your-org-name

# Optional — restrict to specific repos (comma-separated)
GITHUB_REPOS=repo1,repo2,repo3
```

### Generate a Flask Secret Key

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### Kubernetes Secret (Production)

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: pipeline-hub-oauth
  namespace: pipeline-hub
type: Opaque
stringData:
  GITHUB_CLIENT_ID: "Ov23liABCDEF123456"
  GITHUB_CLIENT_SECRET: "your_client_secret_here"
  FLASK_SECRET_KEY: "your_generated_secret_key"
```

Apply it:

```bash
kubectl apply -f oauth-secret.yaml
```

Then reference it in your Deployment:

```yaml
envFrom:
  - secretRef:
      name: pipeline-hub-oauth
```

---

## Step 4 — How the Login Flow Works

```
┌──────────────┐                    ┌──────────────┐                    ┌──────────┐
│   Browser    │                    │ Pipeline Hub  │                    │  GitHub   │
│   (User)     │                    │  (Flask App)  │                    │   API     │
└──────┬───────┘                    └──────┬───────┘                    └─────┬────┘
       │                                   │                                  │
       │  1. Click "Login with GitHub"     │                                  │
       │ ────────────────────────────────> │                                  │
       │                                   │                                  │
       │  2. Redirect to GitHub authorize  │                                  │
       │ <──────────────────────────────── │                                  │
       │                                   │                                  │
       │  3. User approves on GitHub       │                                  │
       │ ─────────────────────────────────────────────────────────────────>   │
       │                                   │                                  │
       │  4. GitHub redirects back         │                                  │
       │     with temporary code           │                                  │
       │ ────────────────────────────────> │                                  │
       │                                   │                                  │
       │                                   │  5. Exchange code for token      │
       │                                   │ ──────────────────────────────>  │
       │                                   │                                  │
       │                                   │  6. Return OAuth token           │
       │                                   │ <──────────────────────────────  │
       │                                   │                                  │
       │  7. Store token in session        │                                  │
       │ <──────────────────────────────── │                                  │
       │                                   │                                  │
       │  8. All API calls now use         │                                  │
       │     THIS user's token ✅          │                                  │
       │                                   │ ──────────────────────────────>  │
```

### OAuth Scopes Requested

| Scope | Purpose |
|---|---|
| `repo` | Read repository list, workflow files, and run history |
| `workflow` | Trigger workflow dispatch events |

Users grant these permissions **once** during the first login. They can revoke access anytime via GitHub Settings → Applications.

---

## Step 5 — Verify It's Working

1. Start Pipeline Hub:
   ```bash
   GITHUB_CLIENT_ID=xxx GITHUB_CLIENT_SECRET=yyy FLASK_SECRET_KEY=zzz python app.py
   ```

2. Open `http://localhost:9090`

3. You should see a **"Login with GitHub"** button

4. Click it → authorize on GitHub → you're redirected back

5. Trigger a workflow → check the run in GitHub → it should show **your name**, not a service account

---

## FAQ

### Do I need to change anything in LDAP or our SSO provider?

**No.** GitHub OAuth is completely independent of your corporate LDAP/SSO. It uses GitHub's own identity system. If your users can log into GitHub, they can use Pipeline Hub.

### What if a user doesn't have access to a repo?

GitHub enforces permissions automatically. If a user doesn't have `write` access to a repo, they won't be able to trigger workflows. Pipeline Hub doesn't need to handle this — the GitHub API will return a `403 Forbidden`.

### Can I still use a service account PAT instead?

Yes. The `GITHUB_TOKEN` environment variable is still supported as a fallback. If set, Pipeline Hub will use it for all API calls (no login flow). This is useful for CI/CD environments or automated triggers.

### What happens if a user's session expires?

They'll be redirected to the login page. GitHub OAuth tokens don't expire by default, but session cookies have a configurable timeout (default: 24 hours).

### Is this secure?

- OAuth tokens are stored **server-side in the session** (not in the browser)
- The Client Secret is never exposed to the browser
- All communication with GitHub uses HTTPS
- Users can revoke access anytime from their GitHub settings

### Do I need a separate OAuth App for dev and production?

**Recommended yes.** Create two OAuth Apps:
- One with callback URL `http://localhost:9090/auth/callback` (dev)
- One with callback URL `https://pipeline-hub.yourcompany.com/auth/callback` (prod)

This avoids callback URL mismatch errors.

---

## Quick Reference

```bash
# Development
export GITHUB_CLIENT_ID="your-dev-client-id"
export GITHUB_CLIENT_SECRET="your-dev-client-secret"
export FLASK_SECRET_KEY="any-random-string-for-dev"
python app.py

# Production (Docker)
docker run -d \
  -e GITHUB_CLIENT_ID="your-prod-client-id" \
  -e GITHUB_CLIENT_SECRET="your-prod-client-secret" \
  -e FLASK_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
  -e GITHUB_ORG="your-org" \
  -p 9090:9090 \
  pipeline-hub:latest
```
