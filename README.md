# MailSend

An email client that lets an assistant draft, assemble and schedule email on
behalf of an executive — without ever being able to send it. The executive
signs in with Google, reviews the queue, and sends from their own Gmail account
with one click.

The assistant never receives the executive's password and has no send button
anywhere in the interface.

---

## How it works

**Two roles, two very different sign-ins.**

| | Executive | Assistant ("worker") |
|---|---|---|
| Signs in with | Google OAuth | Username + password issued by the executive |
| Can draft & edit | Yes | Yes |
| Can delete | Yes | Yes |
| **Can send** | **Yes** | **No — enforced server-side** |
| Sees | All mail for their mailbox | Only drafts they created |

Mail is sent through the Gmail API using the executive's own OAuth token, so it
comes from their real address, threads normally, and lands in the recipient's
inbox rather than a bulk-mail folder.

### The core object

An email carries To, CC, BCC, a subject, a body, attachments, and a **send
date**. Drafts can be dated arbitrarily far into the future — the assistant can
queue a message for the start of every quarter, and the executive just clears
the queue whenever they log in.

### The executive's three edit views

Straight from the spec — these are date filters over the same sequential editor:

- **Edit current** — everything dated today or earlier
- **Edit future** — everything dated after today
- **Edit all** — no filter

Each shows every matching message stacked one after another with all fields
editable, saved in a single submit. These pages edit only; sending happens from
the dashboard, either per-message or via **Send current messages**, which sends
everything dated up to and including today.

### Mail merge

Tick "This is a mail merge" and upload a CSV with an `email` column. Any other
column can be dropped into the subject or body as `{{column_name}}`. Each
recipient receives their own individually-addressed message — nobody sees anyone
else's address. Unknown placeholders render as empty rather than leaking
`{{first_name}}` into a real email. Preview before sending.

### Scheduled sending

Ticking "send automatically once the send date passes" lets the
`send_due` management command deliver a message without the executive
clicking anything. That command runs every 10 minutes as a Render cron job.
Messages left unticked always wait for a human.

---

## Running it locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then fill in the Google credentials
python manage.py migrate
python manage.py runserver
```

Open http://localhost:8000. Local development uses SQLite with no setup.

Run the tests with:

```bash
python manage.py test core
```

The suite covers the permission boundaries (an assistant genuinely cannot
send), the current/future/all date filters, mail-merge rendering, signature and
attachment handling in the outgoing MIME payload, and the scheduler.

---

## Deploying to Render

The repo contains a `render.yaml` blueprint that provisions the web service, a
Postgres database and the scheduler cron job.

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, select the repo, apply.
3. Render prompts for `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and
   `GOOGLE_REDIRECT_URI`. Set these after completing the Google steps below.
4. Deploy. Your URL is `https://<service-name>.onrender.com`.

**The URL is stable.** It's derived from the service name and does not change
between deploys, restarts or redeploys — so it's safe to hard-code as the Google
OAuth redirect URI. It only changes if you rename the service. You can attach a
custom domain later in Render → Settings → Custom Domains; do that before you
show it to anyone if you want a branded address, since changing it means
updating the redirect URI in Google.

### Two things to know about Render's free tier

- **Free web services sleep after ~15 minutes idle.** The next visit takes
  roughly 50 seconds to wake. If your boss is going to click the link cold, hit
  it yourself a minute beforehand, or upgrade that service to Starter.
- **The filesystem is wiped on every deploy.** Attachments and merge CSVs are
  written to disk, so on the free plan an attachment uploaded before a deploy is
  gone after it. Database records survive (Postgres is separate); only the files
  are lost. To fix properly, upgrade the web service and attach a persistent
  disk mounted at `/var/data`, then set `DJANGO_MEDIA_ROOT=/var/data/media`.
  The code already reads that variable — no code change needed. Everything else
  works fine on the free tier.

---

## Connecting Google OAuth

This is what makes sending work. Do it once.

### 1. Create a Google Cloud project

Go to <https://console.cloud.google.com>, create a project (call it MailSend).

### 2. Enable the Gmail API

**APIs & Services → Library** → search "Gmail API" → **Enable**.

Nothing can send mail until this is switched on.

### 3. Configure the OAuth consent screen

**APIs & Services → OAuth consent screen**

- User type: **External** (unless everyone using it is on the same Google
  Workspace, in which case choose Internal and skip the test-user step below).
- Fill in app name, your support email and developer contact email.
- On the **Scopes** step, add:
  - `openid`
  - `.../auth/userinfo.email`
  - `.../auth/userinfo.profile`
  - `https://www.googleapis.com/auth/gmail.send`

`gmail.send` is the one that matters — it grants send-only access. It cannot
read the mailbox, which is deliberate: MailSend never needs to.

### 4. Create the OAuth client credentials

**APIs & Services → Credentials → Create Credentials → OAuth client ID**

- Application type: **Web application**
- **Authorised redirect URIs** — add both, exactly, including the trailing slash:

```
http://localhost:8000/google/callback/
https://<your-service>.onrender.com/google/callback/
```

A mismatch here — a missing slash, `http` instead of `https` — produces
`redirect_uri_mismatch`, which is the single most common failure.

Copy the **Client ID** and **Client secret**.

### 5. Set the environment variables

Locally in `.env`, and on Render under **Environment**:

```
GOOGLE_CLIENT_ID=<client id>
GOOGLE_CLIENT_SECRET=<client secret>
GOOGLE_REDIRECT_URI=https://<your-service>.onrender.com/google/callback/
```

`GOOGLE_REDIRECT_URI` must match the environment it's running in — the
localhost value locally, the Render value on Render.

Optionally lock down who can create an executive account:

```
GOOGLE_ALLOWED_EMAILS=boss@company.com,you@company.com
```

Leave it unset and anyone with a Google account can sign up.

### 6. Add test users while the app is unverified

An External consent screen starts in **Testing** mode. Under
**Audience → Test users**, add every Google account that will sign in —
including your boss's. Without this they get "app has not completed
verification" and are blocked.

In Testing mode users see an "unverified app" interstitial: they click
**Advanced → Go to MailSend (unsafe)** to continue. That warning is expected and
is what the original documentation refers to. Refresh tokens in Testing mode
expire after 7 days, so the executive will need to sign in with Google again
about weekly.

To remove both the warning and the weekly re-auth, submit the app for
verification (**Publish app**). Because `gmail.send` is a sensitive scope,
Google requires a review that typically takes a few weeks. Fine to defer — but
plan for it before this is relied on day to day.

### 7. Sign in

Visit the site, click **Sign in with Google**, choose the executive's account,
and grant access. That creates the executive account and stores the refresh
token used for sending.

---

## First run checklist

1. Executive signs in with Google.
2. Executive goes to **Workers** and creates a worker account, then gives that
   username and password to the assistant.
3. Assistant signs in at `/login/` and drafts messages in the Outbox.
4. Executive reviews via **Edit current / future / all** and sends from the
   dashboard.

Set a **Signature** under the Signature tab — it's appended to every outgoing
message.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `redirect_uri_mismatch` | The URI in Google Cloud doesn't exactly match `GOOGLE_REDIRECT_URI`. Check the trailing slash and http vs https. |
| "has not granted MailSend permission to send Gmail" | Gmail API not enabled, or the executive consented before `gmail.send` was added to the scope list. Sign in with Google again. |
| "Google refused to refresh access" | Refresh token expired (7-day limit in Testing mode) or was revoked. The executive signs in with Google again. |
| CSRF failures on every form | `DJANGO_CSRF_TRUSTED_ORIGINS` missing the site's `https://` origin. Render's own hostname is added automatically. |
| Attachments vanished | Free-tier deploy wiped the disk. See the persistent disk note above. |

---

## Layout

```
mailsend/settings.py      configuration, Google scopes, Render detection
core/models.py            User (executive/assistant), Email, Attachment, SendLog
core/views.py             role-gated views; send paths are executive-only
core/google_oauth.py      OAuth flow and token refresh
core/gmail.py             MIME assembly and Gmail API delivery
core/tests.py             25 tests, including the permission boundaries
render.yaml               Render blueprint: web + Postgres + cron
```
