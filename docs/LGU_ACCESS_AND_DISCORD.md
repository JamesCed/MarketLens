# LGU access, the Discord community, and who runs MarketLens

This is the **administrator's guide** to giving LGU staff and officials their MarketLens role safely. The public, applicant-facing guide is the page at `/lgu-access-guide` (template `app/templates/shared/_lgu_access_guide_content.html`). The code is in `app/services/lgu_access_service.py`.

---

## 1. The rule in one paragraph

Choosing "LGU Staff / Official" on the sign-up form grants **nothing**. Anyone can choose it. The account is created **waiting for verification** and gets a reference code (`LGU-XXXXXX`). It gets an LGU role only after the System Admin has done three things:

- confirmed an endorsement from the applicant's head of office, sent from an official LGU address;
- confirmed it by calling the office;
- approved the request in **Admin › LGU Access**.

Discord is where applicants get help and where verified staff talk. **A Discord role never grants MarketLens access.** Discord roles are given *after* MarketLens approval and taken away when MarketLens access is removed.

```
Applicant                     Head of office              System Admin (ICT / MIS)
---------                     --------------              ------------------------
1. Joins Discord (optional),
   picks "LGU staff – applicant"
2. Signs up as LGU  ──────►  gets reference code
                              3. E-mails the endorsement
                                 from the official address ─►  4. Checks the From address,
                                                                  calls the office's directory number,
                                                                  approves in Admin › LGU Access
5. LGU pages open  ◄───────────────────────────────────────────  (role: Encoder or Data Steward)
   Discord role given  ◄─────────────────────────────────────────  6. Gives the matching Discord role
```

---

## 2. Who should be the System Admin

**Recommended: the LGU's ICT / MIS office.** That means a designated, permanent ICT officer as the primary administrator, plus one other ICT staff member as backup.

Why them:

| Reason | Why it matters |
|---|---|
| They don't own the data | BPLO and CPDO staff are the Encoders and Data Stewards. Publishing is the final, independent check, so it should not be done by the office whose figures are being published. |
| Technical skill | Accounts, passwords, settings, backups, the Render and Aiven dashboards, and incident response are ICT work. |
| Continuity | Career ICT staff stay through elections and reorganisations. Appointees and consultants do not. |
| Existing duty | The ICT office already handles the LGU's systems, e-mail domain and security incidents. |

Who should **not** be the System Admin:

- **An Encoder or Data Steward.** One person would then upload, approve *and* publish, which defeats separation of duties.
- **The Data Protection Officer.** The DPO has to oversee the administrators independently, including reviewing the audit trail. They should not also be the person being checked.
- **The mayor or department heads.** They are busy, and they are the people most often targeted by phishing. They endorse staff; they don't operate the system.
- **The development team, after turnover.**
- **A shared account.** Every administrator signs in as themselves, so the audit trail names a real person.

**During the capstone and the pilot:** one designated team member (for example the project lead) is the primary admin and one teammate is the backup. At turnover, follow the checklist in section 7.

Rules for every administrator:

- Keep it to two or three administrator accounts. Admin › Dashboard › System health warns when there is only one, or more than three.
- Use a strong, unique password, and turn on 2FA on Discord, Render, Aiven and the mailbox.
- The DPO reviews the audit trail (Admin › Audit Trail, filter "Accounts" and "Data") at least monthly.

---

## 3. Settings in MarketLens

Go to **Admin › System Settings › LGU access verification**:

| Setting | What it does | Suggested value |
|---|---|---|
| Official LGU e-mail domains | When set, LGU sign-ups and their endorsers must use one of these domains | `tarlaccity.gov.ph` (blank during user testing with Gmail accounts) |
| Endorsement e-mail | Where heads of office send endorsements. Shown in the guide and on the applicant's page | the ICT office's official mailbox |
| Verifying office | Shown to applicants as "who verifies you" | `City ICT / MIS Office` |
| Re-verify every | Days before Admin › LGU Access flags an account as "Review due" | `180` |

---

## 4. Setting up the Discord server for LGU roles

### 4.1 Server safety settings

- **Server Settings › Safety Setup**:
  - Verification level **High** (members must have a verified e-mail and wait 10 minutes).
  - Turn on **Require 2FA for moderator actions**.
  - Explicit image filter: **all members**.
- **Server Settings › Overview**: default notifications **Only @mentions**.
- Ask every LGU member to turn on 2FA (*User Settings › My Account*). Also ask them to turn off *Allow direct messages from server members* (*Privacy & Safety*), which stops fake-admin DMs.
- Make the server invite link **never expire** and paste it into Admin › System Settings › Community.

### 4.2 Roles (highest first)

| Role | Who gets it | How | Key permissions |
|---|---|---|---|
| **MarketLens Admin** | The System Admins only (2–3 people) | Server owner | Manage Roles, Manage Threads, Manage Messages. Display separately, distinct colour. |
| Moderator *(optional)* | Trusted team members | MarketLens Admin | Manage Messages, Timeout Members. **Not** Manage Roles. |
| **LGU Data Steward** | Verified in MarketLens as Data Steward | MarketLens Admin, after approval | Sees `#lgu-stewards`, `#lgu-staff` |
| **LGU Encoder** | Verified in MarketLens as Encoder | MarketLens Admin, after approval | Sees `#lgu-encoders`, `#lgu-staff` |
| LGU Applicant | Anyone who says they are LGU staff | Self-assigned in Onboarding | Sees `#lgu-access-guide`, `#lgu-help`, can open a private thread in `#lgu-verification` |
| Community | Business owners and everyone else | Self-assigned in Onboarding | Public channels |

Never put *LGU Encoder*, *LGU Data Steward* or *MarketLens Admin* in Onboarding or in any self-assign menu. Keep the MarketLens Admin role above every role it manages.

### 4.3 Channels

| Channel | Visible to | Purpose |
|---|---|---|
| `#start-here` (read-only) | everyone | Welcome, rules, link to `/lgu-access-guide` |
| `#lgu-access-guide` (read-only) | LGU Applicant and above | The pinned guide below |
| `#lgu-help` | LGU Applicant and above | General questions. No personal details. |
| `#lgu-verification` | LGU Applicant and above, who can **create private threads** but not send messages in the channel | Ticket-style verification. The applicant opens a private thread named after their code. Only they and the MarketLens Admins (Manage Threads) can see it. |
| `#lgu-staff` | LGU Encoder, LGU Data Steward, MarketLens Admin | Announcements and coordination for verified staff |
| `#lgu-encoders`, `#lgu-stewards` | that role + MarketLens Admin | Role-specific help |

Permissions for `#lgu-verification`:

- @everyone: *View Channel* off.
- LGU Applicant: *View Channel* on, *Send Messages* off, *Create Private Threads* on, *Send Messages in Threads* on.
- MarketLens Admin: *Manage Threads* on.

If you prefer a bot, a ticket bot (for example Ticket Tool) does the same job. It is optional.

### 4.4 Onboarding (Server Settings › Onboarding)

Question: **"Who are you?"** (required, one answer)

- "Business owner / entrepreneur" gives **Community**.
- "LGU staff – applicant (not yet verified)" gives **LGU Applicant**.

### 4.5 AutoMod (Server Settings › AutoMod)

- **Block mention spam** and **block suspected spam**.
- Custom keyword rule **"Personal details"**:
  - Applies to all channels **except** `#lgu-verification` threads.
  - Action: block the message and alert `#mod-log`.
  - Regex patterns:
    - Philippine mobile numbers: `(\+?63|0)9\d{2}[\s-]?\d{3}[\s-]?\d{4}`
    - E-mail addresses: `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}`
    - TIN-like numbers: `\b\d{3}-\d{3}-\d{3}(-\d{3})?\b`
- Custom keyword rule **"Credential bait"**:
  - Action: alert only.
  - Keywords: `send your password`, `your otp`, `verification code`, `send your id`.

### 4.6 Paste-ready messages

**`#start-here`**

> **Welcome to the MarketLens community!**
> **Business owners:** ask questions, share feedback, and read announcements here.
> **LGU staff and officials:** read **#lgu-access-guide** first. Your LGU role in MarketLens is given only after the System Admin verifies you with your head of office. **A Discord role does not give MarketLens access.**
> **Safety:** never post passwords, verification codes, government IDs or personal numbers. MarketLens staff will never ask for them. Trust only members with the **MarketLens Admin** role.
> Full guide: https://marketlens-w1ov.onrender.com/lgu-access-guide

**`#lgu-access-guide` (pin it)**

> **How to get your LGU role**
> **1.** Sign up at MarketLens and choose **LGU Staff / Official**. Fill in your office, position, the role you need (**LGU Encoder** = uploads data, **LGU Data Steward** = reviews uploads) and your head of office's official e-mail. You get a code like `LGU-7K3QPM`.
> **2.** Your **head of office e-mails** the System Admin from their official address, quoting your code. The template is in the guide.
> **3.** Optional: open a **private thread** in #lgu-verification named after your code, with **only** your code and name.
> **4.** The System Admin checks the e-mail and **calls your office on its directory number**, then approves you in MarketLens.
> **5.** You get a notification and e-mail, your LGU pages open, and you get your LGU role here.
> **Never** post IDs, passwords, codes or personal numbers. Encoder and Data Steward are always **two different people**.
> Guide: https://marketlens-w1ov.onrender.com/lgu-access-guide

**What the applicant posts in their private thread**

> Reference code: LGU-XXXXXX
> Name: Juan Dela Cruz
> Office: BPLO
> Role requested: LGU Encoder

**Admin replies**

> *Received:* Thanks! We have your request **LGU-XXXXXX**. We'll verify it with your head of office and call your office. You'll be notified in MarketLens when it's done.
>
> *Approved:* You're verified as **LGU Encoder** in MarketLens and now have the matching role here. Welcome! This thread will be archived.
>
> *Declined:* We couldn't verify request **LGU-XXXXXX**: *[reason]*. You can send a corrected request from your MarketLens LGU Access page.

---

## 5. Verifying a request (System Admin checklist)

Open **Admin › LGU Access** and, for each pending request:

1. **Check the e-mail domains.** The page marks the applicant's and endorser's addresses green or red when official domains are set.
2. **Find the endorsement e-mail** with the request's reference code.
   - The **From** address must be the head of office's official address, and must match the endorser on the request.
   - Reply to the address on file. Never act on a forwarded copy, or on an endorsement posted in Discord.
3. **Call the office** on the number in the official LGU directory or website, not a number from the e-mail or chat, and confirm the person works there in that position.
4. **Choose the role.**
   - Encoders upload; Data Stewards review.
   - Keep them different people. An office needs at least one of each, and the System health card warns when either is missing.
5. **Tick "I verified the endorsement through an official channel"** and approve. The applicant is notified and e-mailed, and the audit trail records you.
6. **On Discord:** give the matching role to the username shown on the request, post the *Approved* reply in their thread, then archive the thread.
7. **To decline:** give a reason the applicant can act on. They can send a corrected request.

Requests nobody verifies expire after 60 days.

---

## 6. Keeping access current

- The **"Review due"** badge marks accounts not re-checked within the re-verification period. Ask each head of office to confirm their list, then press **Re-verify**, or **Remove access** with a reason.
- **Transfer or resignation:** remove access the same day the head of office tells you. Remove the Discord role at the same time.
- **Suspected misuse:** remove access, remove the Discord role, and tell the DPO (see `docs/DATA_PRIVACY.md` § 5, breach response).

---

## 7. Turnover checklist (team → LGU)

1. Create administrator accounts for the LGU ICT officer and their backup. Have them sign in and change their passwords.
2. Set **Admin › System Settings › LGU access verification** to the LGU's domain, mailbox and office.
3. Re-verify every LGU account with its head of office. Remove access for test accounts.
4. **Archive the development team's admin accounts.**
5. Rotate `SECRET_KEY`, `PII_ENCRYPTION_KEY` (via `PII_ENCRYPTION_PREVIOUS_KEYS`), the database password and every API key (see `docs/DATA_PRIVACY.md` § 8).
6. Transfer ownership of the Discord server, the Render service and the Aiven database to the LGU ICT office. Remove team members from the MarketLens Admin role.
