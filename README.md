# NITT Library — Backend

REST API for the NITT Library Management & Book Tracking System. Built with **FastAPI** and **SQLAlchemy**, using SQLite for local development and PostgreSQL for production.

It serves four audiences:

| Surface | Prefix | Used by |
|---------|--------|---------|
| Desk & staff API | `/api` | Library staff screens: circulation, cataloguing, members, reports, admin |
| Storefront & payments API | `/api/v1` | The member-facing website: browse, borrow, pay fines, read e-books |
| Partner API | `/api/partner/v1` (live), `/api/sandbox/v1` (sample data) | Partner institutions, authenticated with an `X-API-Key` header |
| NITT Pay sandbox | `/api/v1/sandbox/...` | A fake payment gateway for development and demos |

Interactive API docs are available at `/docs` once the server is running.

---

## Table of Contents

- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
- [Environment Variables](#environment-variables)
- [Demo Accounts](#demo-accounts)
- [The `/api/v1` API](#the-apiv1-api)
- [Payments](#payments)
- [E-books](#e-books)
- [Database Models](#database-models)
- [Roles & Permissions](#roles--permissions)
- [Library Settings](#library-settings)
- [AI Features](#ai-features)
- [Background Jobs](#background-jobs)
- [Offline Sync](#offline-sync)
- [Running Tests](#running-tests)
- [Deploying to Render](#deploying-to-render)
- [Known Limitations](#known-limitations)

---

## Tech Stack

| Layer | Library | Version |
|-------|---------|---------|
| Web framework | FastAPI | >= 0.115 |
| ASGI server | Uvicorn | >= 0.30 |
| ORM | SQLAlchemy | >= 2.0 |
| Validation | Pydantic + pydantic-settings | >= 2.7 |
| Auth | PyJWT + bcrypt | >= 2.8 / >= 4.1 |
| HTTP client | httpx | >= 0.27 |
| Fuzzy search | rapidfuzz | >= 3.9 |
| Semantic search (optional) | Pinecone | >= 5.0 |
| Scheduler | APScheduler | >= 3.10, < 4 |
| Excel/CSV | openpyxl | >= 3.1 |
| File uploads | python-multipart | >= 0.0.9 |
| Database (prod) | psycopg (PostgreSQL) | >= 3.1 |
| Tests | pytest | >= 8 |

Python 3.11+ recommended.

---

## Project Structure

This repository's root is the backend. There is no `backend/` sub-folder.

```
.
├── app/
│   ├── main.py             # App factory, middleware, lifespan, scheduler, router registration
│   ├── config.py           # Pydantic Settings — reads from .env
│   ├── database.py         # Engine, session factory, get_db, ensure_columns() for added columns
│   ├── models.py           # ORM models (members, catalogue, loans, ledger, payments, e-books, ...)
│   ├── serializers.py      # Dict serialisers for the /api routes
│   ├── security.py         # JWT, bcrypt, role-based dependencies
│   ├── apierrors.py        # ApiError, rendered in the /api/v1 error format
│   ├── slugs.py            # Public title slugs, e.g. "maritime-economics"
│   ├── timeutil.py         # UTC/WAT helpers
│   ├── seed.py             # Demo data for development
│   ├── routers/            # /api — desk, staff and partner routes
│   │   ├── auth.py         # Login, profile, notifications, password reset
│   │   ├── members.py      # Member CRUD and bulk import
│   │   ├── catalogue.py    # Titles, copies, search, labels, bulk import
│   │   ├── circulation.py  # Issue, return, renew, loans, holds, offline sync
│   │   ├── approvals.py    # Member requests (loan/return/payment) that staff approve
│   │   ├── fines.py        # Ledger, desk payments, waivers, reconciliation
│   │   ├── ebooks.py       # E-book upload and reading (numeric title ids)
│   │   ├── institutions.py # Partner institution registration and approval
│   │   ├── partner.py      # Partner API (live and sandbox keys)
│   │   ├── reports.py      # Dashboard, circulation trends, demand analysis
│   │   ├── ai.py           # AI assistant, cataloguing helper, recommendations
│   │   ├── admin.py        # Settings, audit log, system info
│   │   └── files.py        # CSV/Excel file utilities
│   ├── api_v1/             # /api/v1 — storefront and payments API
│   │   ├── library.py      # Catalogue, loans, holds
│   │   ├── checkouts.py    # Basket checkout
│   │   ├── fines.py        # Member fines, waivers
│   │   ├── payments.py     # Payments, refunds, webhook receiver
│   │   ├── ebooks.py       # E-books addressed by title slug
│   │   ├── sandbox.py      # NITT Pay sandbox: pay-page and dashboard endpoints
│   │   ├── errors.py       # Error format, pagination
│   │   ├── idempotency.py  # Idempotency-Key replay
│   │   └── views.py        # camelCase response shapes
│   └── services/
│       ├── circulation.py  # Core rules: issue, return, renew, holds, fines
│       ├── checkout.py     # Copy holds, checkout confirmation and expiry
│       ├── fines.py        # What a member owes, in kobo
│       ├── payments.py     # Payment records, references, fee calculation
│       ├── payment_flow.py # Settling payments, refunds, retries, late payments
│       ├── webhooks.py     # Signed webhook receive/process/retry
│       ├── providers/      # sandbox, paystack (stub), remita (stub)
│       ├── sandbox_cards.py, sandbox_admin.py
│       ├── ebooks.py       # Page splitting and the sample e-book
│       ├── audit.py        # Audit log with before/after snapshots and IP
│       ├── notifications.py# Deduped email and in-app notifications
│       ├── search.py       # Substring + fuzzy (or Pinecone semantic) search
│       ├── settings_store.py # Runtime-configurable library parameters
│       ├── institutions.py, grok.py, vector_store.py
├── tests/                  # Pytest suite
└── requirements.txt
```

---

## Getting Started

### 1. Create a virtual environment

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

Create a `.env` file in the project root (next to `app/`). A `.env` one folder up also works. A minimal development file:

```
APP_ENV=development
SECRET_KEY=change-me-to-a-long-random-string
DATABASE_URL=sqlite:///./library.db
CORS_ORIGINS=http://localhost:5173
BOOTSTRAP_ADMIN_PASSWORD=pick-a-strong-password
SEED_DEMO_DATA=true
```

`.env` is git-ignored. Never commit it: it holds database credentials and API keys. See [Environment Variables](#environment-variables) for everything else.

### 4. Start the server

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

The API is at `http://127.0.0.1:8000`, with Swagger UI at `http://127.0.0.1:8000/docs`.

On startup the app automatically:
- creates any missing tables and adds columns introduced since your database was created;
- creates the bootstrap admin (ID `ADMIN001`, password from `BOOTSTRAP_ADMIN_PASSWORD`, change forced at first sign-in);
- gives every title a public slug if it does not have one;
- loads demo data, including a sample e-book on three titles, if `SEED_DEMO_DATA=true`.

---

## Environment Variables

All variables and their defaults:

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_ENV` | `development` | `development` or `production` |
| `SECRET_KEY` | `dev-only-change-me` | JWT signing key. Generate one: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `DATABASE_URL` | `sqlite:///./library.db` | SQLite or PostgreSQL. `postgres://` and `postgresql://` URLs are accepted |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated allowed frontend origins, e.g. `http://localhost:5173,http://localhost:4051` |
| `APP_BASE_URL` | `http://localhost:5173` | Public URL of the website (used to build links) |
| `BOOTSTRAP_ADMIN_ID` | `ADMIN001` | ID number for the first admin |
| `BOOTSTRAP_ADMIN_PASSWORD` | `change-me-now` | Initial admin password (must be changed at first sign-in) |
| `BOOTSTRAP_ADMIN_EMAIL` | *(empty)* | Admin email |
| `SEED_DEMO_DATA` | `false` | `true` loads sample data on startup. **Never enable in production** |
| `DEMO_PASSWORD` | `Demo@1234` | Password given to every demo account |
| `RUN_SCHEDULER` | `true` | Set `false` on extra workers so background jobs run once |
| `PAYMENT_PROVIDER` | `sandbox` | `sandbox`, `paystack` or `remita` |
| `PAYMENT_PUBLIC_KEY` | `pk_test_nitt_sandbox` | Public key shown to the browser |
| `PAYMENT_SECRET_KEY` | *(empty)* | Provider secret key. Server-side only |
| `WEBHOOK_SECRET` | *(empty)* | HMAC-SHA512 key for webhook signatures. **Required in production.** In development with the sandbox, a key is derived from `SECRET_KEY` |
| `SANDBOX_DELAY` | `true` | Adds a 0.5–1.5 s pause to sandbox charges so the UI shows loading states. Tests turn it off |
| `XAI_API_KEY` | *(empty)* | AI key (Groq or xAI). Blank disables AI |
| `XAI_BASE_URL` | `https://api.x.ai/v1` | Use `https://api.groq.com/openai/v1` for Groq |
| `GROK_MODEL` | `grok-4.3` | e.g. `llama-3.3-70b-versatile` for Groq |
| `GROK_TIMEOUT_SECONDS` | `45` | AI request timeout |
| `PINECONE_API_KEY` | *(empty)* | Optional. Enables semantic catalogue search; blank uses fuzzy search |
| `PINECONE_INDEX` | `nitt-library` | Pinecone index name |
| `SMTP_HOST` | *(empty)* | Blank means emails are logged, not sent |
| `SMTP_PORT` | `587` | SMTP port |
| `SMTP_USER` / `SMTP_PASSWORD` | *(empty)* | SMTP credentials |
| `SMTP_FROM` | `library@nitt.edu.ng` | Sender address |

Unknown variables in `.env` (for example `SUPABASE_*`) are ignored.

**Using Groq instead of xAI**
```
XAI_API_KEY=gsk_your_groq_key
XAI_BASE_URL=https://api.groq.com/openai/v1
GROK_MODEL=llama-3.3-70b-versatile
```

**Using PostgreSQL**
```
DATABASE_URL=postgresql://user:password@host:5432/dbname
```
URL-encode special characters in the password (`@` → `%40`, `#` → `%23`).

---

## Demo Accounts

With `SEED_DEMO_DATA=true`, these accounts exist and all use the `DEMO_PASSWORD` (default `Demo@1234`):

| ID | Name | Role |
|----|------|------|
| `STF001` | Chidi Okeke | admin |
| `STF002` | Dr. Musa Ibrahim | head_librarian |
| `STF003` | Mrs. Hadiza Yusuf | circulation |
| `STF004` | Zainab Lawal | cataloguer |
| `STF005` | Ngozi Eze | finance |
| `STF010` | Engr. Bello Garba | lecturer |
| `NITT/HND/24/001` | Amina Sani | student |
| `NITT/HND/24/002` | Emeka Nwosu | student |
| `NITT/ND/25/014` | Fatima Abdullahi | student |
| `VIS001` | Hajiya Rakiya Danladi | visitor |

The real admin is `ADMIN001` with the password you set in `BOOTSTRAP_ADMIN_PASSWORD`.

---

## The `/api/v1` API

Used by the member-facing website. Sign in with `POST /api/auth/login`, then send `Authorization: Bearer <token>`. The server may return a refreshed token in the `X-New-Token` header (sliding sessions).

### Conventions

- **JSON in camelCase.** Timestamps are ISO 8601 UTC; the front end formats them (DD/MM/YYYY).
- **Money is integer kobo** (`…Kobo` fields), currency NGN. The ledger underneath stays in whole naira and is the single source of truth for what a member owes.
- **Public IDs are opaque strings:** titles by slug (`maritime-economics`; the numeric id is also accepted), `ln_12` loans, `hd_5` holds, `fn_9` fines, `cp_3` copies, `chk_91f2ab34` checkouts, `NITT_7Q4M2XPD` payments. **Barcodes are never returned** to members; the desk API keeps them for scanning.
- **Pagination:** `?page=1&limit=20` → `{ "data": [...], "page", "limit", "total" }`.
- **Errors** always look like:
  ```json
  { "error": { "code": "FINES_BLOCK", "message": "New loans are blocked until fines are below ₦1,000.", "fields": { "email": "Enter a valid email" } } }
  ```
  Codes: `VALIDATION_FAILED` 422, `UNAUTHENTICATED` 401, `FORBIDDEN` 403, `NOT_FOUND` 404, `CONFLICT` 409, `FINES_BLOCK` 409, `LOAN_LIMIT` 409, `NO_COPY_AVAILABLE` 409, `RATE_LIMITED` 429, `BAD_REQUEST` 400, `PAYLOAD_TOO_LARGE` 413. (`PAYMENT_NOT_SUCCESSFUL` is reserved but not currently returned.) Routes outside `/api/v1` keep FastAPI's default `{ "detail": ... }`.
- **Idempotency:** `POST /checkouts` and `POST /payments/initialize` accept an `Idempotency-Key` header; repeating a key replays the first response (`Idempotent-Replay: true`).
- **Other people's records return 404**, not 403, so existence isn't revealed.

### Catalogue, loans and holds

| Method & path | Who | Purpose |
|---------------|-----|---------|
| `GET /titles` | anyone | Search. Query: `q`, `subject`, `format`, `available=1`, `sort` (`relevance`, `title`, `year_desc`, `year_asc`, `newest`), `page`, `limit`. Each title has `availability`, `hasEbook`, `ebookPages` |
| `GET /titles/{id}` | anyone | Full detail with `copies[]` (`id`, `status`, `location`) |
| `GET /titles/{id}/related` | anyone | "More like this" / "Borrow these together" |
| `GET /me/loans` | member | Active loans with `dueAt`, `daysLeft`, `overdueDays`, `renewals`, `canRenew`, `canRead`, `readUrl` |
| `POST /loans/{id}/renew` | owner | Renew. `CONFLICT` if overdue, no renewals left, or someone is waiting |
| `POST /loans/{id}/return` | circulation, head_librarian | Return; assesses any fine (`fineKobo`) and promotes the next hold |
| `POST /holds` | member | `{ "titleId": "..." }` → `queuePosition` |
| `GET /me/holds` | member | Holds with `queuePosition`, `status`, `readyUntil` |
| `DELETE /holds/{id}` | owner | Cancel |

### Fines and checkout

| Method & path | Who | Purpose |
|---------------|-----|---------|
| `GET /me/fines` | member | `perDayKobo`, `grossKobo`, `paidKobo`, `waivedKobo`, `outstandingKobo`, `items[]`, `payments[]` |
| `POST /fines/{id}/waive` | head_librarian | Waive a fine; `reason` required; audited |
| `POST /checkouts` | member | Confirm a basket (see below) |
| `GET /checkouts/{id}` | owner | Status |
| `DELETE /checkouts/{id}` | owner | Cancel an unpaid checkout and free its copies |

`POST /checkouts` body: `{ "items": ["slug", ...], "collectionPoint": "desk", "periodDays": 14, "payFines": true, "paymentMethod": "online" }`.

1. The server reads the fines from the ledger. **Any amount sent by the browser is ignored.**
2. If fines exceed the block threshold and the member is not paying them online, it returns `FINES_BLOCK`.
3. It holds one copy per title for `checkout_hold_minutes` (default 30).
4. With no payment needed, loans are issued at once (`status: "confirmed"`, with `loans[]`).
5. With online payment, it returns `status: "awaiting_payment"` and `payment: { reference, authorizationUrl }`. Loans are issued only after the payment is verified as successful.

A member can have one open checkout at a time. An unpaid checkout expires after the hold window and its copies go back on the shelf.

### Payments

| Method & path | Who | Purpose |
|---------------|-----|---------|
| `POST /payments/initialize` | finance, admin | Manual payment link. Min ₦50. With `metadata.kind = "fine_payment"` and `metadata.member` it clears that member's fines |
| `GET /payments/{ref}` | owner or staff | Status, fee, net, event timeline |
| `POST /payments/{ref}/verify` | owner or staff | Ask the provider for the final status, then settle. Safe to repeat |
| `GET /me/payments` | member | Payment history |
| `POST /payments/{ref}/refund` | finance, admin | Successful payments only. Reopens the fines it covered |
| `POST /webhooks/payments/{provider}` | public | Signed provider webhooks |

### E-books

| Method & path | Who | Purpose |
|---------------|-----|---------|
| `GET /titles/{id}/ebook` | member | `{ available, pageCount, canRead, reason, restrictions }` |
| `GET /titles/{id}/ebook/pages/{n}` | member with the title on loan (or staff) | One page: `{ page, pageCount, text }` |
| `POST /titles/{id}/ebook` | admin, head_librarian | Upload a `.txt`/`.md` file (field `file`, max 2 MB) |
| `POST /titles/{id}/ebook/sample` | admin, head_librarian | Create a 4-page placeholder e-book |
| `DELETE /titles/{id}/ebook` | admin, head_librarian | Remove the e-book |

### NITT Pay sandbox

Enabled only when `PAYMENT_PROVIDER=sandbox`.

Pay-page endpoints (public, like a real hosted pay page: the payment reference is the only credential):

| Method & path | Purpose |
|---------------|---------|
| `GET /sandbox/transactions/{ref}` | Amount, purpose, status, transfer details |
| `POST /sandbox/transactions/{ref}/card` | `{ number, exp, cvv }` → `{ status: "success"\|"otp"\|"fail", message }` |
| `POST /sandbox/transactions/{ref}/otp` | `{ otp }`. Three wrong codes fail the payment |
| `POST /sandbox/transactions/{ref}/transfer` | Start a bank transfer (valid 30 minutes) |
| `POST /sandbox/transactions/{ref}/transfer/confirm` | Succeeds, or fails if the window has expired |
| `POST /sandbox/transactions/{ref}/transfer/expire` | Force a failure |

Dashboard endpoints (finance, admin): `GET /sandbox/admin/stats`, `GET /sandbox/admin/transactions`, `GET /sandbox/admin/webhooks`, `POST /sandbox/admin/webhooks/{id}/resend`, `GET /sandbox/admin/keys`, `POST /sandbox/admin/reset` (needs `{"confirm": true}`; disabled in production).

**Test cards** (card checks run in this order: Luhn, expiry in the future, 3–4 digit code, then the outcome):

| Card | Outcome |
|------|---------|
| `4242 4242 4242 4242` | Succeeds |
| `4000 0000 0000 3220` | Asks for an OTP (`123456`) |
| `4000 0000 0000 9995` | Fails: insufficient funds |
| `4000 0000 0000 0002` | Fails: declined by bank |
| `4000 0000 0000 0069` | Fails: expired card |
| `4000 0000 0000 0119` | Fails: processing error |
| any other valid card | Succeeds |

A typo in the card details does not fail the payment but does count towards the limit of 5 card attempts per payment reference.

---

## Payments

- **The browser never confirms a payment.** A loan is issued and a fine marked paid only after the provider verifies a successful payment: either `POST /payments/{ref}/verify` or a signed webhook, and both go through the same code (`payment_flow.apply_result`).
- **Webhooks:** the signature (HMAC-SHA512 of the raw body, header `X-NITT-Signature` for the sandbox) is checked in constant time before anything is parsed. A bad signature gets 401 and changes nothing. A repeated event id is acknowledged but not processed again. A webhook only prompts the server to ask the provider for the real outcome, so a validly signed "success" for money the provider never took changes nothing. Failed processing retries after 1, 5 and 30 minutes, then hourly for 24 hours.
- **State machine:** `pending → success → refunded`; `pending → failed → pending` (retry on the same reference); `pending → abandoned` (no activity for 24 hours).
- **Failure:** the fines stay open and the copies stay held until the checkout expires. A retry with a good card on the same reference succeeds.
- **Late payment:** if the money arrives after the checkout expired, no loans are issued; the amount is kept as account credit and the head librarian and circulation staff are notified.
- **Refund:** only `success` payments. It adds an adjustment to the ledger so exactly the fines that payment covered are owed again, notifies the member, and writes audit entries.
- **Fees** (settings, not constants): 1.5% plus ₦100 for amounts of ₦2,500 and above, capped at ₦2,000. `feeKobo` is stored on success; net settlement is amount minus fee. Confirm with the real provider.
- **Card data is never stored or logged.** Card numbers, expiry and CVV are checked in memory and discarded (not even the last four digits are kept). A test scans the database files and all log output to prove it.
- **Providers:** `PAYMENT_PROVIDER` selects the implementation behind one interface (`initialize`, `verify`, `refund`, `parse_webhook`). `sandbox` is complete. `paystack` verifies webhook signatures but its other calls return 501, and `remita` is a stub.

---

## E-books

- An admin or head librarian uploads a `.txt` or `.md` file for a title. Pages split on a line containing only `---`, or are sized automatically. The text is stored in the database; the uploaded file itself is not kept.
- A member can read the e-copy **only while they have the title on loan**. Returning the book ends access. Staff can read any e-copy.
- **Nothing is downloadable:** there is no endpoint that serves a file; pages are returned one at a time as JSON with `Cache-Control: no-store`. A test checks the OpenAPI spec so this stays true.
- Server-side controls cannot stop a screenshot or copying text on screen. The reader page should also block printing and selection (`@media print { .reader { display: none } }`, `user-select: none`, disable the context menu and Ctrl/Cmd+P) and must not cache page text in local storage.
- Demo data includes a 4-page sample e-book on *Maritime Economics*, *Logistics and Supply Chain Management* and *Introduction to Transportation Engineering*.
- The older `/api/ebooks/{numeric title id}` routes still work and share the same logic.

---

## Database Models

| Model | Purpose |
|-------|---------|
| `Member` | Students, lecturers, visitors and all staff roles |
| `Institution` | Partner schools/colleges with sandbox and live API keys |
| `Title` | Bibliographic records, with public slug, topics, course, lecturer |
| `Copy` | Physical copy of a title, with a unique barcode (staff-side) |
| `Ebook` | The readable e-copy of a title (page text) |
| `Loan` | Checkout transactions (issue → return) |
| `Hold` | Reservation queue per title |
| `LedgerEntry` | Fines, payments, waivers, replacements, adjustments (whole naira) |
| `Checkout` | A confirmed basket, with its held copies and payment reference |
| `Payment`, `PaymentEvent` | Online payments (kobo) and their timeline |
| `FinePayment` | Links a payment to the ledger rows it wrote |
| `WebhookEvent` | Inbound webhooks, de-duplicated, with retry state |
| `SandboxCharge`, `SandboxTransfer` | The sandbox gateway's own records |
| `IdempotencyKey` | Replayable responses |
| `LoanRequest`, `ReturnRequest`, `PaymentRequest` | Member requests staff approve |
| `Notification` | Email and in-app notifications with delivery tracking |
| `AuditLog` | Append-only audit trail with before/after snapshots, actor and IP |
| `SearchLog` | Search analytics, including searches with no results |
| `SyncReceipt` | Idempotency records for offline sync |
| `Setting` | Runtime library parameters (key-value) |

Tables are created on startup with `Base.metadata.create_all`, and `ensure_columns()` in `app/database.py` adds columns introduced later. There are no Alembic migrations yet; adopt [Alembic](https://alembic.sqlalchemy.org/) before further production schema changes.

---

## Roles & Permissions

| Role | Can do |
|------|--------|
| `student` / `lecturer` / visitors | Search, borrow via checkout, renew, place holds, read e-books on loan, pay fines online, use the AI assistant |
| `circulation` | Issue, return and renew at the desk, record desk payments, view member ledgers, return loans via `/api/v1` |
| `cataloguer` | Create and edit titles and copies, bulk import the catalogue |
| `head_librarian` | Waive fines, reconcile, demand reports, upload e-books, approve requests |
| `finance` | Create manual payment links, refund payments, use the gateway dashboard |
| `admin` | Everything, including settings, audit log, member management, e-book uploads, and the gateway dashboard |

---

## Library Settings

Rules live in the database (`Setting` table) with these defaults and are changed through the admin settings endpoint (`/api/admin/settings`), not by editing code. Values marked *confirm* are placeholders that library management must approve.

| Setting | Default | Notes |
|---------|---------|-------|
| `loan_days` | student 14, staff 30, visitor 14 | per member type |
| `max_items` | student 3, academic staff 10, non-academic staff 5, visitor 1 | *confirm* |
| `max_renewals` | student 1, staff 2, visitor 0 | *confirm* |
| `fine_per_day` | ₦50 | *confirm* |
| `fine_cap_default` | ₦5,000 | used when a copy has no replacement cost |
| `fine_block_threshold` | ₦1,000 | new loans blocked above this *confirm* |
| `overdue_block_days` | 14 | |
| `hold_pickup_hours` | 48 | |
| `max_holds` | 3 | |
| `checkout_hold_minutes` | 30 | how long a basket holds its copies |
| `checkout_periods` | 7, 14 | loan periods the storefront may request *confirm* |
| `payment_min_kobo` | 5000 | minimum online payment |
| `payment_fee_percent` / `_flat_kobo` / `_threshold_kobo` / `_cap_kobo` | 1.5 / 10000 / 250000 / 200000 | sandbox fee model *confirm* |
| `closing_time`, `closure_weekdays`, `closure_dates` | 17:00, Sat/Sun | due dates roll past closed days |
| `reminder_days_after_due` | -2, 0, 1, 7, 14 | |

---

## AI Features

AI features need an OpenAI-compatible API key. The system is designed for **xAI Grok** and works with **Groq** (same API format). See [Environment Variables](#environment-variables).

If `XAI_API_KEY` is not set, AI endpoints return a graceful "AI not configured" response and the rest of the system works normally. The rate limit is 30 AI calls per user per hour (configurable).

---

## Background Jobs

When `RUN_SCHEDULER=true`, APScheduler runs:

| Job | Interval | Purpose |
|-----|----------|---------|
| `daily-jobs` | every 3 hours | Accrue fines, expire holds, queue reminders |
| `email-dispatch` | every 2 minutes | Send pending emails via SMTP |
| `checkout-expiry` | every minute | Expire unpaid checkouts and free their copies |
| `webhook-retry` | every minute | Retry webhooks that failed processing |
| `payment-abandon` | every hour | Mark payments with no activity for 24 hours as abandoned |

On multi-worker deployments set `RUN_SCHEDULER=false` on all but one worker. Run the daily jobs manually with `POST /api/admin/jobs/run`.

---

## Offline Sync

Desk stations can capture transactions offline and push them in bulk:

```
POST /api/circulation/sync
```

Each operation carries a `client_id` (UUID from the client). Duplicates are ignored and the original result returned. Conflicts (e.g. a copy already returned by another station) are reported per operation so the officer can resolve them.

---

## Running Tests

```bash
# from the project root, with the virtual environment active
python -m pytest
```

The suite (about 80 tests) creates a throwaway SQLite database in a temp folder and needs no running server or external services. Demo data is seeded, the scheduler is off, AI is blank, and sandbox delays are off, so it never touches your real `library.db` or `.env` services.

---

## Deploying to Render

### Web Service settings

| Field | Value |
|-------|-------|
| Root Directory | *(leave blank — the repo root is the backend)* |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` |

### Environment variables on Render

| Variable | Value |
|----------|-------|
| `APP_ENV` | `production` |
| `SECRET_KEY` | Long random string |
| `DATABASE_URL` | PostgreSQL URL (Render, Neon or Supabase) |
| `CORS_ORIGINS` | Your frontend URL(s), comma-separated |
| `APP_BASE_URL` | Your frontend URL |
| `BOOTSTRAP_ADMIN_PASSWORD` | Secure initial admin password |
| `PAYMENT_PROVIDER` | `sandbox` until a real gateway is wired in |
| `WEBHOOK_SECRET` | Long random string (**required**: in production there is no derived default) |
| `PAYMENT_SECRET_KEY` / `PAYMENT_PUBLIC_KEY` | From your payment provider |
| `XAI_API_KEY`, `XAI_BASE_URL`, `GROK_MODEL` | Optional, for AI features |
| `RUN_SCHEDULER` | `true` on one instance |

Leave `SEED_DEMO_DATA` unset (false) in production. Do not run `POST /api/v1/sandbox/admin/reset` against a database holding real members' payments: it deletes sandbox payments and the ledger payments they created, which makes those fines owed again.

---

## Known Limitations

- **Paystack and Remita** are stubs. Only the sandbox provider is complete; real gateways need their HTTP calls wired in.
- **Not yet built:** a general per-IP rate limit (card attempts are limited per payment), a daily reconciliation job against the provider's settlement report, and a retention job for payment emails (the retention period still has to be decided).
- **Sandbox pay-page endpoints are public** by design; anyone holding a payment reference can see its amount, purpose and email.
- **E-books are text only.** PDF/EPUB upload is not supported.
- **No schema migrations framework** (see Database Models).
- **Open questions for the library:** final fine rate, grace period and cap per item; loan limits and periods per role; the fines threshold that blocks loans; which real gateway to use and who owns the merchant account; whether desk cash payments should share the online payments table; retention period for payment records and emails.
