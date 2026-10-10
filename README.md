# LOB Classifier

Routes questions from insurance agents to the line of business (LOB) that should service them,
using [Microsoft-Decision-1](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision)
in Microsoft Foundry.

| LOB | Owns |
| --- | --- |
| Policy Servicing | Changes to individually owned policies: beneficiaries, addresses, payment mode, reinstatement, surrender, loans, fund switches, individual claims |
| Corporate Insurance | Business clients: group life and medical, member additions and removals, group renewals and claims, company-owned policies (keyman, buy-sell) |
| AIA Vitality | The wellness programme: points, status, rewards, device syncing, membership fees, Vitality premium discounts |
| IT Helpdesk | Systems and devices: agent portal, iPoS, VPN, email, e-signature, 2FA, customer app login and OTP |

```
$ python lob_classifier.py "How do I add three new hires to ABC Pte Ltd's group hospitalisation plan?"
LOB: Corporate Insurance  confidence=0.998
  Corporate Insurance   0.999
  AIA Vitality          0.001
  Policy Servicing      0.000
  IT Helpdesk           0.000

API round trip:     885 ms  (request sent -> decision -> response)
Connection setup:   492 ms  (TCP + TLS handshake, once per connection)
Total run time:    1386 ms
```

- **API round trip:** one request on an open connection, from sending it to receiving the decision.
  Retry waits are not included. If retries happened, the line says how many.
- **Connection setup:** opening the secure connection. It's paid once per connection, then the
  connection is reused (one per worker thread in an evaluation run).
- **Total run time:** the whole program.

### Where the time goes

The model itself is fast. Microsoft describes it as millisecond-latency, and almost all of the
measured time is network and Azure routing. The API doesn't report its own processing time, so the
model's share can't be measured exactly from outside Azure. Measured from a machine far from the
deployment:

| Measurement | Time |
| --- | --- |
| One network round trip to East US 2 | ~320 ms |
| TLS handshake (new connection) | ~250 ms (was ~1,000 ms; see below) |
| Request rejected at Azure's gateway (model never runs) | ~1,000 ms |
| Real decision on an open connection | ~900 ms |

A request that never reaches the model takes as long as a real decision, so the model's time is
lost in the noise. A `GlobalStandard` deployment can also be served from any region. In testing,
requests went to Central US and Australia East.

**TLS speedup:** the classifier offers the P-256 key exchange up front. With Python's default
settings (OpenSSL 3.6), the Azure front end needs an extra round trip to agree on a key exchange.
In testing that was ~1,000 ms per handshake vs ~250 ms with P-256. If the server ever refuses P-256,
the classifier falls back to the defaults.

To see the model's real speed, deploy in a region near your users, run the app on Azure in that
same region, and use `DataZoneStandard` where it's available.

## How it works

Microsoft-Decision-1 is not a chat model. It doesn't write answers. It takes an input (`state`) and a
typed question, and returns a decision with probabilities. This program asks one `choice` question:
*which LOB should service this request?*

Every request sends three things:

1. **The company routing guide** (`context/routing_guide.md`): your rules and glossary.
2. **The agent's question.**
3. **The LOB options** (`LOBS` in `lob_classifier.py`): each LOB with a description of what it owns.

```json
{
  "model": "<deployment name>",
  "state": {
    "company_routing_guide": "<contents of context/routing_guide.md>",
    "agent_request": "How do I add three new hires to ABC Pte Ltd's group hospitalisation plan?"
  },
  "questions": {
    "lob": {
      "type": "choice",
      "instructions": "An insurance agent sent this request. Using the company routing guide ...",
      "criteria": { "Policy Servicing": "...", "Corporate Insurance": "...", "AIA Vitality": "...", "IT Helpdesk": "..." }
    }
  }
}
```

The model returns the chosen LOB, an overall `confidence`, and a probability for each LOB.
Answers below the review threshold (default 70%) are flagged for a person to check.

### Why the routing guide matters

Your company routes some requests differently from what general insurance knowledge suggests.
For example, *"remove an employee who resigned from the group medical scheme"* sounds like policy
servicing, but your rule is that group schemes belong to Corporate Insurance. The guide teaches
the model those rules.

**The API keeps no memory between requests.** The guide is not stored anywhere on the service. It
works because the program sends it with every request, which is why the program refuses to run
when the guide file is missing or empty.

## Setup

Needs Python 3.10+. The command-line classifier needs nothing else when you use an API key.
The API server needs `pip install -r requirements.txt` (already installed in `.venv`).

1. Create a `.env` file in the project folder:

   ```
   AZURE_ENDPOINT=https://<resource>.services.ai.azure.com
   DEPLOYMENT_NAME=<your Microsoft-Decision-1 deployment name>
   AZURE_API_KEY=<key from the deployment details page>
   ```

   - The endpoint can be the resource root or the portal's `.../models` URL; both work.
   - `DEPLOYMENT_NAME` is the name you gave the deployment, not the model name.
   - Variables already set in your shell override `.env`.

2. To use Microsoft Entra ID instead of a key, leave out `AZURE_API_KEY`, run `az login`, and
   install `azure-identity`. It's already in `.venv`:

   ```bash
   source .venv/bin/activate
   ```

## Usage

```bash
# Classify one question
python lob_classifier.py "Customer can't receive the OTP to log into the app"

# Evaluate the whole test dataset
python lob_classifier.py

# Evaluate a draft guide before replacing the live one
python lob_classifier.py --context context/routing_guide_v2.md
```

| Option | Default | Purpose |
| --- | --- | --- |
| `question` (positional) | none | Classify a single question instead of the dataset |
| `--context PATH` | `context/routing_guide.md` | Routing guide sent with every request |
| `--dataset PATH` | `data/lob_questions.csv` | Labeled questions to evaluate |
| `--output PATH` | `data/lob_predictions.csv` | Where per-question results are written |
| `--workers N` | `4` | Parallel requests. Lower this if you hit rate limits |

### Evaluation output

```
Routing guide: .../context/routing_guide.md
Accuracy:                 100.0%  (40 questions)
  needs company context:  100.0%  (11)
  plain questions:        100.0%  (29)
Flagged for review (<70% confidence): 0
Median API round trip: 874 ms
Connections opened: 4 (median setup 1175 ms each)

Confusion matrix (rows = expected, columns = predicted)
                         Policy  Corporat       AIA        IT
Policy Servicing             10         0         0         0
Corporate Insurance           0        10         0         0
AIA Vitality                  0         0        10         0
IT Helpdesk                   0         0         0        10
```

Misrouted questions are listed under the matrix. `data/lob_predictions.csv` holds every question with
`predicted_lob`, `confidence`, `correct`, `needs_review`, and `round_trip_ms`.

## API server

`api.py` serves the same classifier over HTTP with FastAPI. It opens one pooled HTTPS connection
to Azure when it starts and keeps it warm, so each request pays only the API round trip, with no
connection setup.

```bash
source .venv/bin/activate
uvicorn api:app --port 8765
```

```bash
curl -X POST localhost:8765/classify -H 'Content-Type: application/json' \
     -d '{"question": "How do I add three new hires to ABC Pte Ltd'"'"'s group hospitalisation plan?"}'
```

```json
{
  "lob": "Corporate Insurance",
  "confidence": 0.998,
  "needs_review": false,
  "probabilities": {"Corporate Insurance": 0.9985, "AIA Vitality": 0.0008, "Policy Servicing": 0.0005, "IT Helpdesk": 0.0002},
  "retries": 0,
  "timing": {"api_round_trip_ms": 343.1, "connection_setup_ms": 0.0, "total_ms": 344.8}
}
```

| Endpoint | Purpose |
| --- | --- |
| `POST /classify` | Body `{"question": "..."}` (1–4,000 characters). Returns the LOB, confidence, review flag, probabilities and timing |
| `GET /health` | Deployment name and LOB list |
| `GET /docs` | Interactive API docs (FastAPI) |

**Timing fields:** `api_round_trip_ms` covers request sent → decision → response received.
`connection_setup_ms` is TCP + TLS time, and is 0 when a warm connection was reused. `total_ms` is
the time spent inside the endpoint.

**How it stays fast:**
- **Opens the connection at startup.** The first user request doesn't pay setup; the log shows
  `Warm connection to Azure opened in ~800 ms`.
- **Keep-warm ping.** It touches the resource root (not the model) every `KEEPALIVE_SECONDS`, so
  Azure doesn't drop the idle connection.
- **Faster TLS and a shared pool.** Same P-256 fix as the CLI, with a pool of up to 10 connections
  shared across concurrent requests.

In testing, four requests on a warm connection all had `connection_setup_ms: 0`, with round trips of
343–1,328 ms. The spread comes from Azure routing to different regions.

| Setting (environment or `.env`) | Default | Purpose |
| --- | --- | --- |
| `ROUTING_GUIDE` | `context/routing_guide.md` | Routing guide loaded once at startup (restart to pick up edits) |
| `KEEPALIVE_SECONDS` | `60` | Keep-warm interval; `0` disables it |

**Errors:**
- **429:** the decision API's rate limit was still exceeded after 3 retries. The deployment
  currently allows 50 requests per minute.
- **502:** the decision API failed or returned something unexpected.

## Project layout

```
.env                       endpoint, deployment, API key (keep out of version control)
lob_classifier.py          the command-line program
api.py                     FastAPI server (same LOBs, guide, and instructions)
requirements.txt           packages for the API server
context/routing_guide.md   company routing rules and glossary, sent with every request
data/lob_questions.csv     labeled test questions
data/lob_predictions.csv   results of the last evaluation run
```

## Customizing

| To change | Edit |
| --- | --- |
| Company routing rules, terminology, glossary | `context/routing_guide.md`. No code changes needed |
| What each LOB owns, or add or rename an LOB | `LOBS` at the top of `lob_classifier.py` |
| How the question is phrased to the model | `INSTRUCTIONS` in `lob_classifier.py` |
| When to flag an answer for review | `REVIEW_THRESHOLD` in `lob_classifier.py` |
| Test questions | `data/lob_questions.csv` |

**Dataset format:** a CSV with `question` and `expected_lob` columns. The optional columns are
`id` and `requires_context` (`yes` for questions that depend on company rules, which are reported
separately). Every `expected_lob` must match a key in `LOBS`.

**After any change to the guide, LOBs, or instructions, re-run the evaluation.** The model is
sensitive to wording: rephrasing a rule or an option can shift its decisions.

## Results so far

Measured on the 40-question dataset (10 per LOB, 11 that depend on company rules):

| Setup | Accuracy | Company-rule questions |
| --- | --- | --- |
| Routing guide + LOB descriptions (current) | 40/40 | 11/11 |
| Bare LOB names, no guide | 37/40 | 8/11 |

Without the company rules, the model sent group-scheme actions (adding new hires, removing a leaver,
changing the HR contact) to Policy Servicing.

| Per request | |
| --- | --- |
| API round trip (open connection) | ~0.9 s, mostly network and routing |
| Connection setup | ~0.5 s, once per connection |
| Single question, end to end | ~1.4 s |
| 40-question evaluation, 4 workers | ~11 s total |
| Input tokens with guide | ~520 |
| Input tokens without guide | ~276 |

The guide is billed on every request, so a longer guide costs proportionally more.

## Limitations

- **The dataset is small and clean.** 40 hand-written questions don't represent real agent traffic.
  Replace them with real, labeled tickets before relying on the accuracy numbers.
- **There's no "none of these" option.** The model must pick one of the four LOBs. If agents send
  requests outside them (e.g. commission disputes), add an `Other / unclear` option to `LOBS`.
- **No explanations.** The model returns probabilities, not reasons. Use the confidence and the
  review flag to decide what happens automatically.
- **Guide size.** The service doesn't document a maximum size for `state`. If the guide grows large,
  check that accuracy holds, and consider sending only the rules relevant to each question.
- **The LOB descriptions repeat the guide's rules.** Keep the two consistent when you edit either one.

## Troubleshooting

| Error | Likely cause | Fix |
| --- | --- | --- |
| `Routing guide not found` / `is empty` | Wrong `--context` path or empty file | Check the path and file contents |
| `KeyError: 'AZURE_ENDPOINT'` | `.env` missing or incomplete | Create `.env` as in Setup |
| `HTTP 401` | Wrong or expired key or token | Check `AZURE_API_KEY`, or re-run `az login` |
| `HTTP 404` | Wrong endpoint | Use the resource endpoint from the deployment details page |
| `HTTP 422` | Wrong deployment name | Set `DEPLOYMENT_NAME` to the deployment name, not the model name |
| `HTTP 429` after retries | Deployment rate limit (currently 50 requests per minute) | Use `--workers 2`, or request more quota |
| `Dataset uses LOBs not defined in LOBS` | Label in the CSV doesn't match `LOBS` | Fix the spelling, or add the LOB |

Rate-limit (429) and server errors (5xx) are retried automatically, up to 6 times with backoff, waiting
as long as Azure's retry hints ask.
