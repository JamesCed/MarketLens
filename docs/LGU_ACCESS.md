# LGU access, the upload workflow, and who runs MarketLens

This is the **administrator's guide**. It covers giving LGU staff and officials their MarketLens role, and the work each role does afterwards. Applicants read the public page at `/lgu-access-guide` (template `app/templates/shared/_lgu_access_guide_content.html`). The code is in `app/services/lgu_access_service.py` and `app/services/staging_service.py`.

---

## 1. The rule in one paragraph

LGU staff register on **`/register/lgu`**. The account is created **`pending_verification`**. While it is pending it can open:

- the LGU Dashboard
- the Diversification Plan
- the Saturation Map
- the Trend Reports

It has **no LGU role**: Gov't Data Upload / Review shows as **"Pending Request Access..."** and can't be opened. The request stays pending until the System Admin approves or rejects it in **Admin › Manage Users › LGU access requests**. It never expires on its own.

The decision is e-mailed with a link:

- **Approved Encoder:** Gov't Data Upload
- **Approved Data Steward:** Gov't Data Review
- **Rejected:** their request page, where they can correct it and send it again

Discord is **not** part of this workflow. It stays a general community space.

```
LGU staff                         System Admin (ICT / MIS)
---------                         ------------------------
1. /register/lgu  ──► pending_verification ──►  2. Manage Users › LGU access requests
   (dashboard, plan, map and trends open;           approve (choose the role) or reject (reason)
    "Pending Request Access..." in the menu)
3. E-mail with the link  ◄────────────────────────  decision
   Encoder → Gov't Data Upload · Data Steward → Gov't Data Review
```

## 2. The registration form

| Part | Fields |
|---|---|
| 1. Account details | First, middle (optional) and last name. Official work e-mail address (when *Official LGU e-mail domains* is set, it must use one of them). Password and confirmation. |
| 2. Tarlac City LGU context | **Jurisdiction scope**: City-wide or Specific barangay. **Target barangay**: one of the 76, active only for a barangay scope. **LGU department / office**: CPDO, BPLO, Assessor's, Treasurer's, Mayor's Office or Barangay Executive Office. **Official job title / designation.** |
| 3. Role selection & system scope | **Requested LGU role**: LGU Encoder or LGU Data Steward. **Brief reason**: what datasets their office will manage (10–300 characters). |

The form asks for nothing else: no birthday, address, contact number or ID. When a mail transport is configured, the work e-mail is confirmed with a 6-digit code before the account exists.

**Scope is enforced:**

- A barangay-scoped Encoder can only stage rows for that barangay.
- A barangay-scoped Data Steward only sees uploads for that barangay.

## 3. Deciding a request (System Admin checklist)

In **Manage Users**, the *LGU access requests* panel sits between the search bar and the accounts table. For each pending request:

1. **Read the request:**
   - name and work e-mail (marked *official domain* or *not official* when domains are set)
   - office, job title and jurisdiction
   - requested role and reason
2. **Check that the person works there.** Use the official e-mail domain, or call the office on its number in the LGU directory.
3. **Approve:**
   - Choose the role. You can give a different role than the one requested.
   - Optionally add a note; it goes into the e-mail.
   - Tick *I checked that this person works in the office they named*, then **Approve**.
4. **Or reject:**
   - Give a reason the applicant can act on.
   - They are e-mailed, and can send a corrected request.

Each office needs at least one Encoder and one Data Steward, and they must be **different people**. The *System health* card on the Admin Dashboard warns when either is missing.

## 4. What each role does afterwards

| Role | Page | What they can do |
|---|---|---|
| **LGU Encoder** | Gov't Data Upload | Upload a file to the staging area.<br>Until it is published:<br>• **Edit** the barangay rows (zoning, density, permits, closures)<br>• **Replace** the file for bigger corrections<br>• **Archive** (withdraw) it<br>Edited batches go back to the Data Steward. The Steward's messages show on the row and on the edit page. |
| **LGU Data Steward** | Gov't Data Review (their only data page) | Work through the queue. Compare each batch with the live figures, then:<br>• **Approve**<br>• **Ask for changes**: the message tells the Encoder what to fix or clean<br>• **Reject**<br>The page also shows recently published data. A Steward never reviews their own upload. |
| **System Admin** | Datasets (the *Publish approved government data* panel, above the tables) | **Publish** approved batches to the live dataset, or **reject** them with a reason. |

Every step notifies the next person and is written to the audit trail.

## 5. Who should be the System Admin

**Recommended: the LGU's ICT / MIS office.** That means a designated, permanent ICT officer as primary administrator, plus one other ICT staff member as backup.

| Reason | Why it matters |
|---|---|
| They don't own the data | BPLO, CPDO and barangay staff are the Encoders and Data Stewards. Approving accounts and publishing are independent checks, so they shouldn't be done by the office whose figures are published. |
| Technical skill | Accounts, passwords, settings, backups, the Render and Aiven dashboards, and incident response are ICT work. |
| Continuity | Career ICT staff stay through elections and reorganisations. |
| Existing duty | The ICT office already runs the LGU's systems, e-mail domain and security incidents. |

Who should **not** be the System Admin:

- an Encoder or Data Steward, which would break separation of duties
- the Data Protection Officer, who oversees the administrators
- the mayor or department heads, who are busy and are frequent phishing targets
- the development team after turnover
- a shared account

**During the capstone:** one designated team member is the primary admin and one teammate is the backup. Keep it to two or three administrator accounts. *System health* warns when there is only one, or more than three. The DPO reviews the audit trail at least monthly.

## 6. Settings (Admin › System Settings › LGU access verification)

| Setting | What it does | Suggested value |
|---|---|---|
| Official LGU e-mail domains | When set, LGU staff must register with one of these | `tarlaccity.gov.ph` (blank during testing with Gmail accounts) |
| LGU access contact e-mail | Shown to applicants for questions | the ICT office's official mailbox |
| Verifying office | Shown as "who approves you" | `City ICT / MIS Office` |
| Re-verify every | Days before an account shows *Review due* | `180` |

## 7. Keeping access current

- **Review due:** open *Verified LGU staff and recent decisions* in the LGU access panel. Re-check each person with their office, then press **Re-verify**.
- **Transfer or resignation:** press **Remove** the same day. The person keeps the planning pages but loses Gov't Data Upload / Review.
- **Suspected misuse:** remove access and tell the DPO (`docs/DATA_PRIVACY.md` § 5).
- **Data kept:** a decided request is deleted with the audit trail (365 days by default). A pending one stays until it is decided.

## 8. Turnover checklist (team → LGU)

1. Create administrator accounts for the LGU ICT officer and their backup.
2. Set the LGU access settings to the LGU's domain, mailbox and office.
3. Re-verify every LGU account with its office, and remove access for test accounts.
4. **Archive the development team's admin accounts.**
5. Rotate `SECRET_KEY`, `PII_ENCRYPTION_KEY` (via `PII_ENCRYPTION_PREVIOUS_KEYS`), the database password and every API key (`docs/DATA_PRIVACY.md` § 8).
6. Transfer ownership of the Render service, the Aiven database and the Discord server to the LGU ICT office.
