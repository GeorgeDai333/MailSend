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

The header row is matched case-insensitively and trimmed, so `Email`, `email`
and ` E-Mail ` all work. Rows with no address are skipped rather than failing
the whole batch.

**Test files** are in [`samples/`](samples/):

- `mail_merge_sample.csv` — five clean rows. Try this subject and body:
  ```
  {{first_name}}, quick note about {{company}}

  Hi {{first_name}},

  Good to speak. As {{role}} at {{company}}, I thought you should see
  {{next_step}}.
  ```
- `mail_merge_edge_cases.csv` — a UTF-8 BOM, padded headers, a blank line, an
  escaped quote, a comma inside a quoted name, and a row with no address.
  Four rows parse, three are valid recipients.

Both use `@example.com` addresses, which bounce by design. To test real
delivery, replace them with your own address — Gmail treats `you+ann@gmail.com`
as `you@gmail.com`, so you can send the whole merge to yourself and see each
personalised copy arrive separately.

### Attachments

Executable file types are refused: Gmail's
[published blocklist](https://support.google.com/mail/answer/6590) plus the
script types it misses (`.py`, `.sh`, `.rb`, `.pl`, `.php` and similar).
`.zip` archives are inspected one level deep, as Gmail does. Files with no
extension are also refused, since the recipient's mail client would be left
guessing how to open them.

This check has to live here: the Gmail **API** does not enforce the blocklist
that the Gmail web interface applies, so without it MailSend could deliver a
file Gmail itself would have rejected.

A rejected file is reported and skipped; the rest of the message saves normally.
The list is in [`core/attachments.py`](core/attachments.py).

### Scheduled sending

Ticking "send automatically once the send date passes" lets the
`send_due` management command deliver a message without the executive
clicking anything. Messages left unticked always wait for a human.

This needs a scheduler to run `python manage.py send_due` periodically. Render
cron jobs have no free tier, so that block is **commented out** in
`render.yaml` — uncomment it to enable (it's billed). Until you do, the
checkbox has no effect and everything is sent by clicking, which is the
behaviour the original spec describes anyway.

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

The repo contains a `render.yaml` blueprint that provisions the web service.
The database is external (see below) and the scheduler is opt-in.

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, select the repo, apply.
3. Render prompts for the values marked `sync: false`. Set:
   - `DATABASE_URL` — the Neon connection string, including `?sslmode=require`
   - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` —
     after completing the Google steps below
4. Deploy. Your URL is `https://<service-name>.onrender.com`.

### Why the database is not on Render

Render's free Postgres **expires 30 days after creation**, and only one can be
active per workspace. Both are fatal for a tool meant to keep running, so
`render.yaml` deliberately has no `databases:` block. Point `DATABASE_URL` at
any external Postgres instead — [Neon](https://neon.com)'s free plan is
permanent and ample here. `settings.py` reads `DATABASE_URL` via
`dj_database_url`, so no code changes are involved either way.

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

### 3. Configure the consent screen

This lives under **Google Auth Platform** in the console sidebar (Google moved
it out of "APIs & Services"; the old menu path no longer exists). It's split
into several pages:

**Branding** — app name, user support email, developer contact email.

**Audience** — user type **External**, unless everyone signing in is on the
same Google Workspace, in which case choose Internal and skip the test-user
step below.

**Data Access** — this is where scopes live. Add:

- `openid`
- `.../auth/userinfo.email`
- `.../auth/userinfo.profile`
- `https://www.googleapis.com/auth/gmail.send`

`gmail.send` is the one that matters — it grants send-only access. It cannot
read the mailbox, which is deliberate: MailSend never needs to.

### 4. Create the OAuth client credentials

**Google Auth Platform → Clients → Create client** (equivalently
**APIs & Services → Credentials → Create Credentials → OAuth client ID**)

- Application type: **Web application**
- **Authorised redirect URIs** — add both, exactly, including the trailing slash:

```
http://localhost:8000/google/callback/
https://<your-service>.onrender.com/google/callback/
```

A mismatch here — a missing slash, `http` instead of `https` — produces
`redirect_uri_mismatch`, which is the single most common failure.

On **Create**, a dialog shows the **Client ID** and **Client secret**.

> **Copy the secret before closing that dialog, or use Download JSON.**
> Google now shows the client secret in full *only once, at creation*.
> Afterwards the console displays just the last four characters.

The client ID looks like `1234-abc.apps.googleusercontent.com`; the secret
looks like `GOCSPX-…`.

Lost the secret? Open the client under **Google Auth Platform → Clients** and
try the **Download JSON** icon on its row. If that's unavailable, click
**Add secret** to issue a new one and revoke the old — rotating is harmless
as long as you update `GOOGLE_CLIENT_SECRET` wherever it's set.

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

### 6. Publish the app — don't leave it in Testing

An External consent screen starts in **Testing**, where refresh tokens
**expire after 7 days**. That means the executive has to sign in with Google
again every week, which is exactly the kind of friction this tool exists to
remove.

Under **Google Auth Platform → Audience**, click **Publish app** to move to
*In production*. You do **not** need to complete Google's verification review
first — you can decline the invitation to submit and stay in production,
unverified. What that gets you:

- Refresh tokens stop expiring, so the executive stays signed in.
- No test-user list to maintain.

What it does *not* change: users still see the "Google hasn't verified this
app" interstitial and click **Advanced → Go to MailSend (unsafe)** to continue.
That warning is identical in Testing, so staying in Testing buys nothing.

Unverified apps using sensitive scopes have a **100 new-user cap**; once 100
distinct accounts have authorized, Google sign-in is disabled until the app is
verified. Irrelevant for a handful of executives, but it's the reason to
eventually submit for verification if this ever grows.

**If everyone using this is on the same Google Workspace**, set the audience to
**Internal** instead. That removes the unverified warning entirely, with no cap
and no verification — strictly better than External for a single-organisation
deployment.

If you do stay in Testing for now, add every account that will sign in under
**Audience → Test users**, or they are blocked outright.

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
| "Google refused to refresh access" | Refresh token expired or was revoked. Most often the app is still in **Testing** mode, where tokens die after 7 days — publish it. Otherwise the executive signs in with Google again. |
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
