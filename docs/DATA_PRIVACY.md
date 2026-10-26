# MarketLens: Data Privacy and Protection

This document explains how MarketLens follows the **Data Privacy Act of 2012 (Republic Act No. 10173)**, its Implementing Rules and Regulations (IRR), and the National Privacy Commission (NPC) issuances that apply to it. It also gives the LGU and the development team what they need to put in place before the system is used with real records:

- a Data Protection Officer,
- a breach response procedure,
- an agreement between the LGU and the developers.

> It was written for the capstone system as built and is **not legal advice**. Before any real deployment, the LGU's Data Protection Officer and legal officer should review it and adapt it.

---

## 1. Who is responsible for what

| Party | Role under RA 10173 | For which data |
|---|---|---|
| City Government (LGU) that adopts MarketLens | **Personal Information Controller (PIC)**: it decides what is processed and why | Business permit and zoning records it uploads; the accounts of its officials; the accounts of the business owners it invites to use the system |
| MarketLens development team | **Personal Information Processor (PIP)**: it processes data only on the LGU's documented instructions (IRR Secs. 43–45) | The same data, while it builds, hosts and maintains the system |
| Hosting and service providers (sub-processors) | Process data on the developers' behalf, with the LGU's prior approval | See section 6.3 |

While MarketLens is still a capstone prototype, the development team runs it and is responsible for the test accounts. When an LGU adopts it, the roles above apply and the agreement in section 6 should be signed.

---

## 2. How each data privacy principle is met

RA 10173, Sec. 11 requires personal data to be processed with **transparency**, for a **legitimate purpose**, and with **proportionality**. It also requires the data to be accurate, kept only as long as needed, and kept secure (Sec. 20; NPC Circular 2023-06).

| Principle | What it asks | How MarketLens meets it |
|---|---|---|
| **Transparency** | People know what is collected, why, by whom, for how long, and how to exercise their rights | A privacy notice on the Gov't Data Upload page (what, kept, purpose, who, how long, contact). The full Privacy Policy is at `/privacy`, with a section 6 for LGU uploads. The Terms and Conditions are at `/terms`. Both are linked in the footer of every page, and both are agreed to with the checkbox at sign-up. The Data Protection Officer's contact details are shown on both. |
| **Legitimate purpose** | Data is used only for the declared, lawful purpose | Uploaded data is used only for market saturation scores, city-wide recommendations, trend reports and the Diversification Plan. Nothing is sold or shared outside MarketLens. |
| **Proportionality** (data minimisation) | Only the data the purpose needs | A file with owner names, contact numbers, e-mail or home addresses, TIN or other ID numbers or birth dates is **refused** (`data_import_service._refuse_personal_data`). So is a file with an e-mail address or mobile number in any cell. From a permit list, only the barangay, industry, line of business, status and permit number are read. Only **totals** are stored: one row per barangay, plus counts per industry and kind of business. No business name, permit number or file row is kept. The uploaded file is deleted as soon as it has been processed. Personal details are shown masked to administrators. |
| **Accuracy** | Data is accurate and kept up to date | Barangay names must match the 76 official names. Unreadable values are reported with their row and column. An upload is all or nothing. The newest upload per barangay is the one used. |
| **Retention** (Sec. 11(e)) | Kept no longer than necessary | Uploaded LGU data is kept **1,095 days** by default. Audit trail entries are kept **365 days** by default. An Admin can change both in System Settings (30–3,650 days). Past those periods the data is deleted automatically, once a day (`retention_service`). |
| **Security** (Sec. 20; NPC Circular 2023-06) | Organisational, physical and technical measures | <ul><li>Pages are served over HTTPS.</li><li>Each role can open only its own pages, enforced on the server.</li><li>Every form has CSRF protection.</li><li>Invisible human verification (Cloudflare Turnstile) guards sign-in, sign-up, password reset and the AI / Google Places routes.</li><li>Failed sign-ins and heavy requests are rate limited.</li><li>Passwords are stored only as hashes.</li><li>Gender, birthday, contact number and address are encrypted at rest (Fernet), and the key can be rotated.</li><li>The audit trail records who did what, when and from where.</li><li>An upload's contents are checked, not only its file name: renamed programs, PDFs and web pages, macro workbooks and zip bombs are refused.</li><li>Secrets live only in environment variables.</li></ul> |
| **Rights of data subjects** (Sec. 16) | To be informed, to access, to correct, to object, to erasure or blocking, to damages, to file a complaint | Users can correct most details themselves in Settings. Other requests go to the Data Protection Officer named in the notices. |

---

## 3. What personal data the system holds

| Data | Collected from | Stored where | Who can see it | Kept for |
|---|---|---|---|---|
| Name and e-mail of every account | Sign-up form | `user` table | The user. Administrators (e-mail masked in lists). | While the account exists. Archived, not deleted. |
| Gender, birthday, contact number, address | Sign-up and Settings | `user` table, **encrypted** (`*_enc` columns) | The user. Administrators see it masked. | While the account exists. |
| Business plans | SME users | `sme_profile`, `forecast_result` | The plan's owner | While the account exists. Trash is emptied after 30 days. |
| Audit trail: action, time, IP address, browser | Every significant action | `audit_logs` | Administrators | `audit_log_retention_days` (default 365) |
| Uploaded zoning figures and permit counts | LGU uploads | `lgu_data`, `market_data` (source DTI/PSA), `subcategory_market_data` | LGU accounts and administrators. Business owners see only the resulting scores and counts. | `upload_retention_days` (default 1,095) |
| The uploaded file | LGU uploads | Temporary file on the server | Nobody | Deleted right after processing |

The permit and zoning data that is stored holds **no personal information**. It is made up of totals per barangay and counts per industry. Business names and permit numbers are read only while the file is processed.

---

## 4. Data Protection Officer

**NPC Advisory No. 2017-01** requires every LGU to designate a Data Protection Officer (DPO). A component city may instead designate a Compliance Officer for Privacy (COP) who works under the DPO of its province.

Fill in and keep current:

| | |
|---|---|
| Name of DPO / COP | ______________________________ |
| Office | ______________________________ |
| E-mail | ______________________________ |
| Telephone | ______________________________ |
| Developer contact for privacy matters | ______________________________ |
| System administrator | ______________________________ |

To show these on the privacy notices, set the `DPO_NAME`, `DPO_OFFICE`, `DPO_EMAIL` and `DPO_PHONE` environment variables on Render. Until they are set, the notices direct requests to `SUPPORT_EMAIL`.

The DPO's functions under the advisory include:

- monitoring compliance with the Act, the IRR and NPC issuances;
- ensuring privacy impact assessments are done;
- advising on data subject requests and complaints;
- overseeing breach and security incident management, including reports to the NPC;
- serving as the contact point for data subjects and the NPC.

Check whether the LGU's data processing systems and DPO must be registered with the NPC under **NPC Circular 2022-04**.

---

## 5. Personal data breach response

This procedure follows **NPC Circular No. 16-03** (Personal Data Breach Management). **NPC Advisory No. 2026-02** covers submissions through the NPC's Data Breach Notification Management System (DBNMS). As the processor, the development team reports to the LGU; the LGU, as controller, notifies the NPC and the people affected.

**1. Report it immediately.** Anyone who notices a possible breach tells the DPO and the developer contact at once. Examples include:

- a leaked API key or database password;
- logins nobody recognises;
- a lost laptop that holds a database dump or `.env`;
- personal data visible where it should not be.

**2. Contain it (first hours).**

- Rotate every exposed secret (section 8).
- Suspend affected accounts from Admin > Users.
- Take the service offline from the Render dashboard if data is still exposed.
- **Preserve evidence**: export the audit trail (Admin > Audit Trail > export) and set `DATA_RETENTION_ENABLED=false` so the daily retention job does not delete entries the investigation needs.

**3. Assess it.**

- What data was involved?
- How many people are affected?
- Was sensitive personal information or data enabling identity fraud involved?
- Was it, or is it likely to have been, acquired by an unauthorised person?
- Does it create a real risk of serious harm?

**4. Notify, within 72 hours of knowing.** When those conditions are met, the LGU notifies:

- **the NPC**, through the DBNMS;
- **the affected data subjects**.

Notify within **72 hours** of knowing of the breach, using the information available then, without waiting for the investigation to finish. The notification describes:

- the nature of the breach;
- the personal data involved;
- the measures taken to address it and reduce harm;
- the contact person;
- the assistance available to those affected.

A **full breach report** follows within **five days**, unless the NPC allows more time.

**5. Recover and learn.**

- Fix the cause.
- Restore service.
- Record the incident in the LGU's security incident register.
- Include it in the annual security incident report the NPC requires.
- Review this procedure.

| Step | Who | When |
|---|---|---|
| Internal report | Whoever notices | Immediately |
| Containment and key rotation | Developers and system administrator | Within hours |
| Assessment | DPO with developers | Within 24 hours |
| NPC and data subject notification | LGU (PIC), through the DPO | Within 72 hours of knowledge |
| Full breach report | LGU (PIC) | Within 5 days |

---

## 6. Agreement between the LGU and the developers

When an LGU adopts MarketLens, the developers process its data on its behalf. **IRR Sections 43–45** require a written outsourcing (data processing) agreement. At minimum it must contain the following.

### 6.1 Required contents (IRR Sec. 44(a))

1. The subject matter and duration of the processing: hosting, maintaining and supporting MarketLens for the LGU, from __________ to __________.
2. The nature and purpose of the processing: computing market saturation scores, recommendations, trend reports and the Diversification Plan from uploaded permit and zoning records, and providing user accounts.
3. The types of personal data:
   - account data: name, e-mail, gender, birthday, contact number, address;
   - the audit trail.

   The categories of data subjects are LGU officials, business owners and administrators.
4. The rights and obligations of the LGU as controller.
5. The geographic location of the processing:
   - Render (web application), region __________;
   - Aiven (MySQL database), region __________.

### 6.2 What the developers must do (IRR Sec. 44(b))

1. Process the data only on the LGU's documented instructions, including any transfer outside the Philippines.
2. Bind everyone with access to confidentiality.
3. Keep the security measures in section 2 and comply with the Act, the IRR and NPC issuances.
4. Engage no other processor without the LGU's prior instruction, and bind each one to the same obligations (section 6.3).
5. Help the LGU answer data subject requests.
6. Help the LGU comply with the Act, including breach notification. Tell the LGU **without delay, and within 24 hours at most**, of any breach or suspected breach.
7. At the end of the service, return or delete all personal data, as the LGU chooses, including copies and backups, unless the law requires keeping it.
8. Give the LGU the information it needs to show compliance, and allow audits and inspections.
9. Tell the LGU at once if an instruction would break the Act, the IRR or NPC issuances.

### 6.3 Sub-processors (to be approved by the LGU)

| Provider | Purpose | Personal data it receives |
|---|---|---|
| Render | Web hosting | All data passes through it in transit |
| Aiven | MySQL database hosting | All stored data |
| Brevo / Gmail | Sending verification, password reset and alert e-mails | Recipient e-mail address and name |
| Google (Places API, Maps) | Competitor counts and map tiles | No personal data |
| Google (Gemini) | Writing the plain-language forecast explanation | Plan figures. No personal details, as stated in the Terms. |

### 6.4 Also agree on

- the retention periods (section 7);
- who may hold administrator accounts;
- how and where database backups are kept;
- the support and incident contacts (section 4);
- the governing law and how disputes are settled.

---

## 7. Retention schedule

| Data | Period | How it ends | Where it is set |
|---|---|---|---|
| Uploaded zoning rows and permit counts | 1,095 days after upload (default) | Deleted by the daily retention job. Rows a saved forecast still uses are kept until that forecast is gone; they hold totals only. | Admin > System Settings > Data retention |
| Audit trail entries | 365 days (default) | Deleted by the daily retention job | Admin > System Settings > Data retention |
| Uploaded files | Minutes | Deleted right after processing | Fixed |
| Plans in Trash | 30 days | Deleted | Fixed |
| User accounts and plans | While the account exists | Archived by an administrator. Erasure is done on request through the DPO. | Admin > Users |

Each retention run that deletes something writes a "Deleted data past its retention period" entry in the audit trail, with the counts. The last run is also shown on the settings page.

---

## 8. Secrets and keys

- Real secrets are kept **only** in:
  - `.env` on a developer's own computer (git-ignored);
  - the **Environment** settings of the Render service.
- `.env.example` and `.env.sample` hold **placeholders only**. Do not put real keys in them, in zip files, in chats or in the repository.
- `PII_ENCRYPTION_KEY` gets its own random value, never a copy of `SECRET_KEY`. Set it only in Render, and keep a copy in a password manager.

### Rotating a key that may have been exposed

| Secret | Where to rotate | Effect |
|---|---|---|
| `GOOGLE_PLACES_API_KEY`, `GOOGLE_GEOCODING_API_KEY`, `GOOGLE_MAPS_JS_API_KEY` | Google Cloud console > APIs & Services > Credentials: create a new key with the same restrictions, then delete the old one | None after updating Render |
| `GEMINI_API_KEY` | Google AI Studio > API keys: create new, delete old | None |
| `BREVO_API_KEY` | Brevo > SMTP & API > API keys: create new, delete old | None |
| `GMAIL_APP_PASSWORD` | Google Account > Security > App passwords: revoke and create new | None |
| `DATABASE_URL` / `DB_PASSWORD` | Aiven console > service > Users: reset the password | Update Render at the same time |
| `SECRET_KEY` | Generate a new value | Everyone is signed out once. If `PII_ENCRYPTION_KEY` was blank, first follow the PII steps below, putting the old `SECRET_KEY` in `PII_ENCRYPTION_PREVIOUS_KEYS`. |
| `PII_ENCRYPTION_KEY` | See below | Stored personal details are re-encrypted |

### Rotating `PII_ENCRYPTION_KEY`

1. Generate a new key: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
2. On Render, set `PII_ENCRYPTION_PREVIOUS_KEYS` to the old key. If details were written before `PII_ENCRYPTION_KEY` existed and `SECRET_KEY` is also being changed, add the old `SECRET_KEY`, comma-separated.
3. On Render, set `PII_ENCRYPTION_KEY` to the new key, then deploy.
4. In the deploy log, look for `Re-encrypted N personal detail(s) under the current PII_ENCRYPTION_KEY`.
5. Delete `PII_ENCRYPTION_PREVIOUS_KEYS` and deploy again.

If the log says some details "could not be decrypted with any configured key", add the key they were written under to `PII_ENCRYPTION_PREVIOUS_KEYS` and deploy again. Nothing is deleted.

---

## 9. Before going live with an LGU

- [ ] DPO or COP designated, and `DPO_*` variables set on Render
- [ ] Outsourcing agreement (section 6) signed, and sub-processors approved
- [ ] Privacy impact assessment done by the LGU
- [ ] Retention periods confirmed in Admin > System Settings
- [ ] Every exposed or shared secret rotated (section 8), and `.env.example` holding placeholders only
- [ ] `PII_ENCRYPTION_KEY` set on its own, and a copy kept safely
- [ ] LGU staff told to remove personal fields from registers before upload
- [ ] Breach response contacts (section 5) known to everyone involved
- [ ] NPC registration requirement checked (NPC Circular 2022-04)

### Sources

- Republic Act No. 10173, Data Privacy Act of 2012, and its IRR (as amended): <https://privacy.gov.ph/wp-content/uploads/2023/06/IRR_RA-10173-as-amended.pdf>
- NPC Circular 2023-06, Security of Personal Data in the Government and the Private Sector (effective 30 March 2024; repealed Circular 16-01)
- NPC Circular No. 16-03, Personal Data Breach Management
- NPC Advisory No. 2026-02, submission of breach notifications through the DBNMS
- NPC Advisory No. 2017-01, Designation of Data Protection Officers: <https://privacy.gov.ph/?p=1507>
- NPC Circular 2022-04, Registration of Personal Data Processing Systems: <https://privacy.gov.ph/wp-content/uploads/2023/05/Circular-2022-04.pdf>
