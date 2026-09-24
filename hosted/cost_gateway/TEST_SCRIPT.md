# Try-it test script (no technical knowledge needed)

A step-by-step check of the free trial at **tryinferrail.com/try/**,
written so anyone can run it with just a web browser: no terminal, no
code. Run it after every change to the trial, or any time you want to
be sure it works.

**Time:** about 10 minutes (15 with the optional real-key part).
**You need:** a computer with Chrome, Firefox, Safari, or Edge. A phone
is useful for step 9. For the optional part, an OpenAI or Anthropic API
key (it costs a fraction of a cent).

Tick each box as you go. If a step doesn't match **what you should
see**, stop, note the step number and what you saw instead, and send it
via the page's **Report an issue** box (step 10). A screenshot helps.

---

## 1. Open the page

- [ ] Go to **https://tryinferrail.com/try/**
- **You should see:** a large heading "Try Inferrail free" and one big
  red button, **"Try free — instant gateway"**.

## 2. Start a trial

- [ ] Click **Try free — instant gateway**.
- **You should see:** the button changes to "Setting up your personal
  gateway…". If the service has been idle, it may say "Waking up the
  trial service" for up to about a minute. That's normal.
- **Then:** a white panel headed **"Your personal gateway"** appears,
  with:
  - a **Deletes in** countdown (starting near 24:00:00) and a **Demo
    mode** badge
  - a web address, labelled "Your personal base_url"
  - **Your dashboard link**, with a red warning under it to keep it
    private.

## 3. Send a test request

- [ ] Under **Send a test request**, leave **Work** as
  `support-ticket-1` and **Task** as `draft-reply`.
- [ ] Click **Send a demo request**.
- **You should see:** a small result box with a short reply, the
  provider "demo", token counts, and a latency in milliseconds.
- [ ] Scroll to **Live feed**. **You should see:** one entry with
  "demo / demo-model · support-ticket-1", "success", and a cost like
  `$0.000160`.
- [ ] Scroll to **Budget burn**. **You should see:** "Spent today" a
  little above $0.00, and "Daily cap (this trial)" of $1.00.

## 4. See what the work cost

- [ ] Scroll to **What does this work cost?**
- **You should see:** a shaded summary line like "1 unit of work ·
  $0.000160 known cost · mark an outcome below…", a table row for
  `support-ticket-1`, and under **By task** a row for `draft-reply`.
- [ ] In the `support-ticket-1` row, click **Succeeded**.
- **You should see:** within a few seconds the row shows "succeeded",
  and the summary ends with "1 succeeded → $0.000160 per successful
  outcome".
- [ ] Back up at **Send a test request**, change **Work** to
  `support-ticket-2` and click **Send a demo request** again.
- **You should see:** a second row appear. The summary now says "2
  units of work" and notes that the per-success cost includes work that
  didn't succeed.

## 5. The dashboard link

- [ ] Click **Copy** next to **Your dashboard link**.
- [ ] Open a **private/incognito window** (Ctrl+Shift+N in Chrome and
  Edge, Ctrl+Shift+P in Firefox, Cmd+Shift+N in Safari) and paste the
  link into its address bar.
- **You should see:** the same trial, with the same countdown and both
  work rows, without clicking anything.
- [ ] Look at the address bar in that window. **You should see:** the
  long part starting with `#t=` has disappeared from it.
- [ ] Close the private window.

## 6. Reload keeps your trial

- [ ] In your original window, press **F5** (or the reload button).
- **You should see:** "Reopening your trial…" briefly, then the same
  panel and data. Nothing is lost.

## 7. Download your data

- [ ] Scroll to **Keep your data** and click **Download my data
  (JSON)**.
- **You should see:** a file named `inferrail-trial-….json` download,
  and the page says "Downloaded 2 receipts."
- [ ] Optional: open the file in a text editor. **You should see:**
  receipts, work, and task totals. **You should not see** the words
  "Say hello in five words" (the test prompt). Prompts and replies are
  never stored.

## 8. Optional: your own key (costs a fraction of a cent)

Skip this section if you don't have an OpenAI or Anthropic API key.

- [ ] Under **Optional: add your own key**, paste your key into the
  OpenAI or Anthropic box and click **Add key(s)**.
- **You should see:**
  - the box empties straight away, and the page says the key is
    configured and "cleared from this page"
  - the badge changes to **Real-key mode**
  - the countdown drops to 4 hours or less (real-key trials are
    deliberately short).
- [ ] Click **Send a real request (your key)**.
- **You should see:** a result labelled "Real receipt — your OpenAI key"
  (or Anthropic), a real reply, and a new entry in the Live feed with a
  real cost.
- [ ] Click **Forget stored key(s)**. **You should see:** the badge goes
  back to **Demo mode** and the real-request button disappears.

## 9. Phone check

- [ ] Open **https://tryinferrail.com/try/** on a phone and start a
  trial.
- **You should see:** everything fits the screen with no sideways
  scrolling, and the buttons are easy to tap. Steps 3 and 4 work the
  same.

## 10. Report an issue

- [ ] Scroll to **Report an issue**, type "Test script run: all good"
  (or what went wrong), and click **Send feedback**.
- **You should see:** "Thanks — that was sent to the team." The message
  goes privately to the team; it's never posted publicly.

## 11. End the trial

- [ ] At the bottom of the panel, click **End trial now and delete
  everything**, then confirm.
- **You should see:** the panel disappears and the page says "Trial
  ended and deleted."
- [ ] Paste your dashboard link (from step 5) into a new tab.
- **You should see:** "That trial has expired or was ended — its data
  is gone." The link no longer works for anyone.

---

**All boxes ticked?** The trial works end to end. Record the date and
browser you used if you're keeping a log.

**Something didn't match?** Send the step number and what you saw
through **Report an issue**, or to whoever looks after the service. For
the person investigating: every response carries an `X-Request-ID`,
and `LAUNCH.md`'s incident runbook explains how to trace it in the logs.
