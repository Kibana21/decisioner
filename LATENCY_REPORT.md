# Latency Report: LOB Classifier

Measured on 2026-10-10 from the developer's machine, calling a `GlobalStandard` Microsoft-Decision-1
deployment in East US 2 with an API key. All figures come from runs already made, listed under Sources.

## Summary

One request from the command line takes about **1.4 s** end to end. Nearly all of it is network:
connecting to Azure (~0.5 s) and the API round trip (~0.9 s). The program itself (Python startup,
reading files) takes about 50 ms. The model's own processing time can't be isolated from outside
Azure. A request rejected at Azure's gateway, before any model runs, takes as long as a real decision.

## Time per step: one request

```
python lob_classifier.py "How do I add three new hires to ABC Pte Ltd's group hospitalisation plan?"
```

| # | Step | Typical time | Share | Notes |
| --- | --- | --- | --- | --- |
| 1 | Python startup + import | ~50 ms | 4% | Median of 5 runs (50–61 ms) |
| 2 | Read `.env` + routing guide, build request | <1 ms | 0% | Request body is 2.5 KB |
| 3 | DNS lookup | ~5 ms | 0% | Median of 10 (2–35 ms); usually cached |
| 4 | TCP connect | ~250 ms | 18% | One network round trip. Lowest seen 233 ms, but spikes up to 1.3 s |
| 5 | TLS handshake | ~250 ms | 18% | With the P-256 fix. Was ~980 ms before |
| 6 | API round trip | ~870 ms | 61% | Request sent → Azure routing → model → response received |
| 7 | Parse + print | <1 ms | 0% | |
| | **Total** | **~1.4 s** | | Three runs: 1,503 / 1,435 / 1,386 ms |

The script prints steps 3–5 together as "Connection setup" (492–624 ms across the three runs) and step 6
as "API round trip" (841–885 ms).

## Inside the API round trip (step 6)

Measured on an already-open connection, 10 requests each:

| Request | Median | Range |
| --- | --- | --- |
| Real decision | 971 ms | 366–1,368 ms |
| Rejected at the gateway (made-up deployment name, model never runs) | 946 ms | 881–984 ms |

- **The model's share is lost in the noise.** A request that never reaches the model takes as long as
  one that does. That's consistent with Microsoft's millisecond-latency claim, but it can't be proven
  from outside Azure.
- **Requests are served from many regions.** The deployment is in East US 2, but `GlobalStandard`
  sent the 10 real requests to Central US (7), Sweden Central (2) and Australia East (1). Distance to
  whichever region serves a request is the main reason the round trip varies.
- **The fastest decision took 366 ms.** That's close to one network round trip (~250 ms), so the
  model plus Azure's routing can add little more than 100 ms when the route is short.

## What has been optimized

| Change | Step affected | Before | After |
| --- | --- | --- | --- |
| Offer P-256 key exchange in TLS | TLS handshake | ~980 ms | ~250 ms |
| Reuse one connection per worker thread | Connection setup, per request after the first | ~1.2 s every request | 0 ms after the first |

**Effect:** one request went from ~2.1 s to ~1.4 s. The 40-question evaluation went from about 20 s
(estimated without reuse) to 11.8 s with reuse, then 10.7 s with the TLS fix.

## What's left

| Remaining time | Where it goes | How to reduce it |
| --- | --- | --- |
| ~0.9 s API round trip | Distance to East US 2, plus Azure routing to other regions | Deploy in a region near you, using `DataZoneStandard` where available |
| ~0.5 s connection setup | Two network round trips (TCP + TLS) | Run the classifier as a long-lived service so the connection stays open |
| ~50 ms Python startup | Interpreter start and import | Not worth optimizing |

## Sources

All runs on 2026-10-10:

- Three single-question runs of `lob_classifier.py` after the TLS fix (step totals and printed timings).
- Benchmark of 10 samples per step: DNS, TCP, TLS (default vs P-256), real decision vs gateway-only
  request, Python startup (5 samples).
- Earlier TLS comparison: 16 handshakes each, median 995 ms default vs 245 ms with P-256.
- Full evaluation runs: 11.8 s (connection reuse), 10.7 s (connection reuse + TLS fix).
