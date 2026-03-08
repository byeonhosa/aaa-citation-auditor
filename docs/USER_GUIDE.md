# AAA Citation Auditor — User Guide

This guide is written for attorneys, paralegals, and legal staff. No technical background is needed.

---

## Table of Contents

1. [Getting Started](#1-getting-started)
2. [Running Your First Audit](#2-running-your-first-audit)
3. [Understanding Results](#3-understanding-results)
4. [Resolving Ambiguous Citations](#4-resolving-ambiguous-citations)
5. [AI Risk Memo](#5-ai-risk-memo)
6. [Configuring Settings](#6-configuring-settings)
7. [Exporting Results](#7-exporting-results)
8. [Audit History](#8-audit-history)
9. [Troubleshooting](#9-troubleshooting)
10. [FAQ](#10-faq)

---

## 1. Getting Started

### What you need

- A web browser (Chrome, Firefox, Safari, or Edge)
- The AAA Citation Auditor app running on your computer or network

If your firm's IT person has already set up the app using Docker, you only need the browser. Open the address they provided — typically **http://localhost:8000** if you are on the same computer, or a local network address if it is running on a server.

If you need to set up the app yourself, follow the [Quick Start instructions in the README](../README.md).

### Opening the app

Navigate to the app address in your browser. You will see the **Audit Dashboard** — the main screen where you start every audit.

---

## 2. Running Your First Audit

### Step 1 — Provide your document

You have two options:

**Option A — Paste text directly**
Copy and paste the relevant text from your brief, memo, or filing into the large text box labeled "Pasted text."

**Option B — Upload a file**
Click the file upload area (or drag and drop a file onto it). The app accepts:
- **Word documents** (.docx)
- **PDF files** (.pdf)

You can upload multiple files at once (up to 10 by default). Each file will produce a separate set of results.

> **Note:** If you paste text and also upload files, the app will use the pasted text and ignore the uploaded files.

### Step 2 — Click Audit

Click the **Audit** button. A spinner will appear while the app extracts and verifies citations — this typically takes a few seconds, or up to a minute for large documents with many citations.

### Step 3 — Review the results

Once the audit finishes, you will see:

- A **summary grid** showing how many citations fell into each status category
- A **list of every citation found**, with its status, the surrounding text for context, and any detail about why the status was assigned
- An **AI Risk Memo** if your settings include an AI provider (see [Section 5](#5-ai-risk-memo))

---

## 3. Understanding Results

Each citation receives one of the following status labels:

### ✅ VERIFIED
The citation was found in CourtListener's database and matches a published opinion. This is the best outcome — it means the citation appears to be real and correctly formatted.

> *Example detail: "CourtListener matched citation."*

### ❌ NOT_FOUND
The citation was not found in CourtListener. This may mean:
- The case name, reporter, or page number is misspelled or incorrect
- The citation uses an unusual abbreviation CourtListener does not recognize
- The opinion exists but has not been added to CourtListener's database

**Action:** Check the citation manually against Westlaw, Lexis, or the original source.

### ⚠️ AMBIGUOUS
CourtListener found more than one case that could match the citation. The app will attempt to resolve this automatically using the surrounding text (see [Section 4](#4-resolving-ambiguous-citations)). If automatic resolution does not succeed, you will be offered a list of candidates to choose from.

### 🔗 DERIVED
This citation is a shortened reference — typically *Id.*, *Id. at [page]*, or a similar back-reference — that refers to the immediately preceding full citation. The app links it to that prior citation rather than verifying it independently.

> *Example detail: "Derived from prior citation (Brown v. Board of Educ., 347 U.S. 483 (1954))."*

If the reference could not be linked (for example, *Id.* appeared at the very start of the document), it will be shown as unresolved.

### 📋 STATUTE_DETECTED
The citation refers to a statutory provision (a law passed by Congress or a legislature) rather than a court opinion. Examples: *42 U.S.C. § 1983*, *28 U.S.C. § 1331*. The app does not verify statutes against CourtListener (which focuses on case law) and labels them for your awareness.

### 🔴 ERROR
Something prevented the citation from being verified — for example, CourtListener was temporarily unreachable, the request timed out, or the citation text was too short to look up. The app will show a specific message explaining what happened.

**Action:** Re-run the audit after a few minutes, or check the [Troubleshooting section](#9-troubleshooting).

### 🔒 UNVERIFIED_NO_TOKEN
No CourtListener API token has been configured. The citation was not checked. See [Section 6](#6-configuring-settings) for how to add a token.

---

## 4. Resolving Ambiguous Citations

When CourtListener finds multiple cases that match a citation, the app tries to automatically pick the correct one by comparing the citation text and surrounding context against the candidate cases. If it succeeds, the status is changed from AMBIGUOUS to VERIFIED and the chosen case is noted.

If automatic resolution does not find a clear winner, the citation remains AMBIGUOUS and you are offered a list of candidate cases. For each candidate you will see:

- The case name
- The court that decided it
- The date it was filed

Click **Select** next to the case you believe is correct. The citation's status will update to VERIFIED and the choice will be remembered — future audits containing the same citation will be resolved instantly without querying CourtListener again.

You can change a previous selection at any time from the Audit History page.

---

## 5. AI Risk Memo

After each audit, the app can generate a short advisory memo that summarizes the overall citation risk in plain language. The memo includes:

- A **risk level** (Low, Moderate, or High)
- A **brief summary** of citation quality
- A list of the **top issues** found
- **Recommended actions**

> **Important:** The AI memo is advisory only. The verification statuses (VERIFIED, NOT_FOUND, etc.) are the authoritative result. The memo is a starting point for your review, not a substitute for professional judgment.

### Enabling the AI memo

Go to **Settings → AI Risk Memo** and choose a provider:

**OpenAI** (requires a paid OpenAI account)
1. Enter your OpenAI API key.
2. Choose a model — `gpt-4o-mini` is fast and inexpensive for most documents.
3. Save settings.

**Ollama** (free, runs on your own computer — no API key needed)
1. Ollama must be running (see Docker setup in the README for how to enable it).
2. Select Ollama, enter the base URL (`http://localhost:11434` by default, or `http://ollama:11434` in Docker Compose), and the model name (e.g., `llama3.2`).
3. Save settings.

Once enabled, the memo will appear automatically after every audit.

---

## 6. Configuring Settings

Go to **Settings** using the navigation bar. Changes take effect on the next audit — no restart is needed.

### CourtListener Integration

| Setting | What it does |
|---------|-------------|
| API Token | Your CourtListener token. Without it, citations are marked UNVERIFIED_NO_TOKEN. |
| Verification Base URL | The CourtListener API endpoint. Leave this as-is unless your IT person says otherwise. |
| Request Timeout | How long (in seconds) to wait for CourtListener before giving up. Increase this on slow connections. |

### AI Risk Memo

| Setting | What it does |
|---------|-------------|
| AI Provider | Choose None, OpenAI, or Ollama. |
| OpenAI API Key | Your OpenAI key (starts with `sk-`). |
| Model | OpenAI model name. `gpt-4o-mini` is recommended for cost and speed. |
| Ollama Base URL | Address of your Ollama server. |
| Ollama Model | Name of the Ollama model you have downloaded. |
| AI Request Timeout | How long to wait for an AI response before giving up. |
| Include citation content | When checked, sends the full citation text to the AI. Produces more specific memos but uses more tokens (higher cost for OpenAI). |

### Guardrails

These limits protect against accidentally uploading extremely large files or documents with thousands of citations.

| Setting | Default | What it does |
|---------|---------|-------------|
| Max File Size | 50 MB | Larger files are rejected. |
| Max Files per Batch | 10 | Limits how many files can be submitted at once. |
| Max Citations per Run | 500 | Extra citations beyond this limit are skipped with a warning. Consider splitting very long documents. |

### Resolution Cache

The app stores your disambiguation choices (both automatic and manual) in a local cache. When you audit a document containing the same citation again, it resolves instantly without querying CourtListener.

Use **Clear resolution cache** to start fresh — for example, if a disambiguation choice was incorrect.

---

## 7. Exporting Results

After an audit (or from the History page), you can export results in three formats:

### Markdown
A plain-text format that is readable as-is and renders cleanly in most documentation tools, GitHub, and Notion. Good for pasting into internal memos or keeping alongside case files.

### CSV
A spreadsheet-compatible format. Open in Excel or Google Sheets to sort, filter, or share results with others. Each row is one citation.

### Printable HTML
A clean, formatted page optimized for printing or saving as a PDF. Use your browser's Print function (Ctrl+P / Cmd+P) and choose "Save as PDF." Good for attaching to client files or court submissions.

---

## 8. Audit History

Every completed audit is saved to the **History** page. From there you can:

- Browse all past audits with a summary of their results
- Click any audit to see the full detail page, including individual citations
- Resolve any remaining AMBIGUOUS citations
- Export results in any format

Audits are stored indefinitely until you clear the database.

---

## 9. Troubleshooting

### "No citations were detected"

The app uses standard legal citation formats (e.g., *Brown v. Board of Educ., 347 U.S. 483 (1954)*). If your document uses non-standard abbreviations, very informal shorthand, or citations formatted differently from the Bluebook, the detector may miss them.

**Try:** Paste a short excerpt that contains a citation in standard format and run a test audit to confirm the app is working.

### All citations show UNVERIFIED_NO_TOKEN

No CourtListener API token is configured. Go to **Settings → CourtListener Integration** and add your token. Creating a CourtListener account is free.

### All citations show ERROR — "CourtListener may be unreachable"

The app cannot connect to CourtListener. Possible causes:

- Your computer or network is not connected to the internet
- CourtListener is temporarily down (check [status.courtlistener.com](https://www.courtlistener.com/))
- A firewall is blocking outbound requests to courtlistener.com

**Try:** Wait a few minutes and re-run the audit.

### AI memo shows "AI memo unavailable"

- **OpenAI:** Check that your API key is correct and your OpenAI account has available credits.
- **Ollama:** Make sure Ollama is running and you have pulled the model (`ollama pull llama3.2`). In Docker Compose, confirm the `ollama` service is uncommented and running.

### The app does not open in the browser

If you are using Docker:
1. Make sure Docker Desktop is running.
2. Run `docker compose up -d` from the project folder.
3. Wait 10–15 seconds and refresh the browser.

If you see a "connection refused" error, the app may still be starting. Wait a moment and try again.

---

## 10. FAQ

**Is my data private?**

Yes. AAA Citation Auditor runs entirely on your own computer or local network. The only outbound connections it makes are:
- To **CourtListener** to verify citations (citation text only — not your document)
- To **OpenAI** if you enable the OpenAI AI provider (citation metadata only, if "Include citation content" is enabled)

If you use Ollama, all AI processing also happens locally with no data leaving your network.

**What does AMBIGUOUS mean?**

It means CourtListener found more than one case that could match the citation. This usually happens with common party names or citations that omit enough information to uniquely identify the case. See [Section 4](#4-resolving-ambiguous-citations) for how to resolve it.

**Do I need an internet connection?**

You need an internet connection to verify citations against CourtListener. The app itself (the interface, citation extraction, and all other features) works offline. If you are offline, citations will show as ERROR.

If you use Ollama for AI memos, the AI also works without internet access.

**How much does it cost to run?**

- The app itself is free and open-source.
- **CourtListener** is free (create an account at courtlistener.com).
- **OpenAI** charges per token used. For a typical legal brief with 50–100 citations, a `gpt-4o-mini` memo costs well under $0.01.
- **Ollama** is completely free and runs on your own hardware.

**Can I audit multiple files at once?**

Yes. You can upload up to 10 files per submission (this limit can be changed in Settings). Each file gets its own set of results, and all are saved to the same audit history entry.

**How do I know if a citation is truly correct?**

A VERIFIED status means the citation was found in CourtListener's database and matched a published opinion. It does not verify that the legal proposition you are citing actually appears in the case, or that the case supports your argument. Always review the cited authority before filing.

**Can I change a disambiguation choice I made earlier?**

Yes. Open the audit from the History page and click the citation. If it was resolved heuristically, you will see a "Change selection" option. If it was resolved manually, you can also re-select from the candidate list.

**How do I update the app?**

With Docker, update by pulling the latest image and restarting:

```bash
git pull
docker compose up -d --build
```

Your data (stored in the `aaa-data` volume) is not affected.
