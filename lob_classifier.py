"""Route insurance agent questions to a line of business (LOB) with Microsoft-Decision-1.

Standalone: needs only the standard library (plus azure-identity if you use
Microsoft Entra ID instead of an API key). Reads AZURE_ENDPOINT, DEPLOYMENT_NAME,
and AZURE_API_KEY from the environment or a .env file next to this script.

The company routing guide in context/routing_guide.md is sent with every request:
the API is stateless, so this is how the model learns your company's rules.

    python lob_classifier.py                          # evaluate data/lob_questions.csv
    python lob_classifier.py "Client wants to add a beneficiary"   # classify one question
    python lob_classifier.py --context draft_guide.md # evaluate with another guide version
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import os
import random
import ssl
import sys
import threading
import time
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median

# --------------------------------------------------------------------------- #
# EDIT THIS SECTION to match how your company understands requests.            #
# --------------------------------------------------------------------------- #
# Company routing rules and glossary live in context/routing_guide.md.

# Each LOB and what it owns. Sent to the model as the choice options.
LOBS = {
    "Policy Servicing": (
        "Changes and requests on an individually owned policy after issuance: "
        "beneficiary or nomination changes, address and contact updates, premium "
        "payment mode or card changes, reinstatement, surrender, policy loans, ILP "
        "fund switches, duplicate documents, and claims on individual policies."
    ),
    "Corporate Insurance": (
        "Anything for a business client: group term life, group medical and "
        "employee benefits, adding or removing members, group renewals, group "
        "claims by employees, and company-owned policies such as keyman or buy-sell."
    ),
    "AIA Vitality": (
        "The AIA Vitality wellness programme: points, status tiers, rewards, "
        "device and app syncing for points, health screenings for points, "
        "membership and membership fees, and Vitality-linked premium discounts."
    ),
    "IT Helpdesk": (
        "Technical problems with systems and devices: agent portal and iPoS "
        "access or errors, VPN, email, laptops and tablets, e-signature failures, "
        "shared drive access, 2FA, and customer app login or OTP problems."
    ),
}

INSTRUCTIONS = (
    "An insurance agent sent this request. Using the company routing guide in the "
    "state, which line of business should service it? Apply the company's routing "
    "rules over general industry meaning, and choose based on the primary need."
)

# Answers below this confidence are flagged for human review.
REVIEW_THRESHOLD = 0.7

# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parent
DEFAULT_CONTEXT = ROOT / "context" / "routing_guide.md"
DEFAULT_DATASET = ROOT / "data" / "lob_questions.csv"
API_PATH = "/providers/microsoft/v1/systemone"
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
RETRY_STATUSES = {429, 500, 502, 503, 504}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_context(path: Path) -> str:
    """Read the routing guide. It is required: the classifier never runs without it."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        sys.exit(f"Routing guide not found: {path}")
    if not text:
        sys.exit(f"Routing guide is empty: {path}")
    return text


class LOBClassifier:
    def __init__(self, context: str, timeout: float = 60, max_retries: int = 6):
        endpoint = os.environ["AZURE_ENDPOINT"].rstrip("/").removesuffix("/models")
        url = urllib.parse.urlsplit(endpoint + API_PATH)
        self.host, self.port, self.path = url.hostname, url.port, url.path
        # One persistent connection per thread, so the TCP + TLS handshake
        # (over a second from far-away regions) is paid once, not per request.
        self._local = threading.local()
        # Offer the P-256 key share up front. OpenSSL's default first offer isn't
        # accepted by the Azure front end, which costs an extra round trip
        # (measured ~1,000 ms vs ~250 ms per handshake). Falls back to defaults
        # if the server ever refuses P-256.
        self._tls = ssl.create_default_context()
        self._tls.set_ecdh_curve("prime256v1")
        self.deployment = os.environ["DEPLOYMENT_NAME"]
        self.api_key = os.environ.get("AZURE_API_KEY") or None
        self.context = context
        self.timeout = timeout
        self.max_retries = max_retries
        self.credential = None
        if self.api_key is None:
            from azure.identity import DefaultAzureCredential

            self.credential = DefaultAzureCredential()

    def classify(self, question: str) -> dict:
        """Return {"lob", "confidence", "probabilities", "round_trip_ms", "connect_ms", "retries"}."""
        state = {"company_routing_guide": self.context, "agent_request": question}
        body = json.dumps(
            {
                "model": self.deployment,
                "state": state,
                "questions": {
                    "lob": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": LOBS}
                },
            }
        ).encode("utf-8")

        payload, round_trip_ms, connect_ms, retries = self._post(body)

        answer = (payload.get("answers") or {}).get("lob") or {}
        lob = answer.get("choice")
        if lob not in LOBS:
            raise RuntimeError(f"Unexpected answer: {json.dumps(payload)[:300]}")
        return {
            "lob": lob,
            "confidence": answer.get("confidence"),
            "probabilities": answer.get("probabilities", {}),
            "round_trip_ms": round_trip_ms,
            "connect_ms": connect_ms,
            "retries": retries,
        }

    def _connection(self) -> tuple[http.client.HTTPSConnection, float]:
        """Return this thread's open connection and the ms spent opening it (0 if reused)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None and conn.sock is not None:
            return conn, 0.0
        start = time.perf_counter()
        try:
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self._tls)
            conn.connect()
        except ssl.SSLError:
            self._tls = ssl.create_default_context()
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self._tls)
            conn.connect()
        self._local.conn = conn
        return conn, (time.perf_counter() - start) * 1000

    def _drop_connection(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _post(self, body: bytes) -> tuple[dict, float, float, int]:
        """Return (payload, round_trip_ms, connect_ms, retries).

        round_trip_ms times only the successful request on an open connection:
        request sent, decision made, response received. It excludes connection
        setup (reported as connect_ms) and retry waits. The API doesn't report
        server-side processing time, so this is the closest client-side measure.
        """
        connect_ms = 0.0
        for attempt in range(self.max_retries + 1):
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if self.api_key:
                headers["api-key"] = self.api_key
            else:
                headers["Authorization"] = f"Bearer {self.credential.get_token(TOKEN_SCOPE).token}"
            reused = False
            try:
                conn, opened_ms = self._connection()
                reused = opened_ms == 0.0
                connect_ms += opened_ms
                start = time.perf_counter()
                conn.request("POST", self.path, body, headers)
                response = conn.getresponse()
                raw = response.read()
                round_trip_ms = (time.perf_counter() - start) * 1000
            except (http.client.HTTPException, OSError) as exc:
                self._drop_connection()
                if attempt < self.max_retries:
                    # A reused connection was likely closed by the server while idle:
                    # reconnect right away. Otherwise back off before retrying.
                    if not reused:
                        time.sleep(retry_delay({}, attempt))
                    continue
                raise RuntimeError(f"Couldn't reach the API: {exc}") from exc

            if response.status in RETRY_STATUSES and attempt < self.max_retries:
                time.sleep(retry_delay(response.headers, attempt))
                continue
            if not 200 <= response.status < 300:
                detail = raw.decode("utf-8", errors="replace")[:500]
                raise RuntimeError(f"HTTP {response.status}: {detail}")
            return json.loads(raw), round_trip_ms, connect_ms, attempt
        raise AssertionError("unreachable")


def retry_delay(headers, attempt: int) -> float:
    """Honor Azure's retry hints, else exponential backoff with jitter."""
    for name, scale in (("retry-after-ms", 0.001), ("x-ms-retry-after-ms", 0.001), ("retry-after", 1.0)):
        value = headers.get(name)
        if value:
            try:
                return float(value) * scale + random.uniform(0, 0.5)
            except ValueError:
                pass
    return min(2**attempt, 30) + random.uniform(0, 1)


def evaluate(classifier: LOBClassifier, context_path: Path, dataset: Path, output: Path, workers: int) -> None:
    with dataset.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    unknown = {r["expected_lob"] for r in rows} - LOBS.keys()
    if unknown:
        sys.exit(f"Dataset uses LOBs not defined in LOBS: {sorted(unknown)}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda r: classifier.classify(r["question"]), rows))

    lobs = list(LOBS)
    confusion = Counter()
    wrong, review = [], []
    for row, res in zip(rows, results):
        row.update(
            predicted_lob=res["lob"],
            confidence=f"{res['confidence']:.3f}" if res["confidence"] is not None else "",
            correct=str(res["lob"] == row["expected_lob"]).lower(),
            needs_review=str((res["confidence"] or 0) < REVIEW_THRESHOLD).lower(),
            round_trip_ms=f"{res['round_trip_ms']:.0f}",
        )
        confusion[row["expected_lob"], res["lob"]] += 1
        if row["correct"] == "false":
            wrong.append(row)
        if row["needs_review"] == "true":
            review.append(row)

    def accuracy(subset):
        return sum(r["correct"] == "true" for r in subset) / len(subset) if subset else float("nan")

    ctx_rows = [r for r in rows if r.get("requires_context") == "yes"]
    plain_rows = [r for r in rows if r.get("requires_context") != "yes"]

    print(f"\nRouting guide: {context_path}")
    print(f"Accuracy:                 {accuracy(rows):.1%}  ({len(rows)} questions)")
    print(f"  needs company context:  {accuracy(ctx_rows):.1%}  ({len(ctx_rows)})")
    print(f"  plain questions:        {accuracy(plain_rows):.1%}  ({len(plain_rows)})")
    print(f"Flagged for review (<{REVIEW_THRESHOLD:.0%} confidence): {len(review)}")
    print(f"Median API round trip: {median(r['round_trip_ms'] for r in results):.0f} ms")
    opened = [r["connect_ms"] for r in results if r["connect_ms"]]
    if opened:
        print(f"Connections opened: {len(opened)} (median setup {median(opened):.0f} ms each)")

    width = max(len(l) for l in lobs)
    short = [l.split()[0][:8] for l in lobs]
    print(f"\nConfusion matrix (rows = expected, columns = predicted)")
    print(" " * (width + 2) + "".join(f"{s:>10}" for s in short))
    for exp in lobs:
        print(f"{exp:<{width + 2}}" + "".join(f"{confusion[exp, pred]:>10}" for pred in lobs))

    if wrong:
        print("\nMisrouted:")
        for r in wrong:
            print(f"  #{r['id']} [{r['expected_lob']} -> {r['predicted_lob']}, {r['confidence']}] {r['question']}")

    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nPredictions written to {output}")


def main() -> None:
    run_start = time.perf_counter()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question", nargs="?", help="classify a single question instead of the dataset")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "lob_predictions.csv")
    parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT, help="routing guide sent with every request")
    parser.add_argument("--workers", type=int, default=4, help="parallel requests (lower this if you hit rate limits)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    classifier = LOBClassifier(load_context(args.context))

    if args.question:
        res = classifier.classify(args.question)
        flag = "  (needs review)" if (res["confidence"] or 0) < REVIEW_THRESHOLD else ""
        print(f"LOB: {res['lob']}  confidence={res['confidence']:.3f}{flag}")
        for lob, p in sorted(res["probabilities"].items(), key=lambda kv: -kv[1]):
            print(f"  {lob:<22}{p:.3f}")
        retried = f" (after {res['retries']} rate-limit retries)" if res["retries"] else ""
        print(f"\nAPI round trip:   {res['round_trip_ms']:>5.0f} ms  (request sent -> decision -> response){retried}")
        print(f"Connection setup: {res['connect_ms']:>5.0f} ms  (TCP + TLS handshake, once per connection)")
        print(f"Total run time:   {(time.perf_counter() - run_start) * 1000:>5.0f} ms")
    else:
        evaluate(classifier, args.context, args.dataset, args.output, args.workers)


if __name__ == "__main__":
    main()
