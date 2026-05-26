# H200 rollout-tail implications for llm-d routing

This note translates the validated H200 rollout-latency findings from `h200-rl-lab` into actionable llm-d Router/EPP and py-scheduler guidance. It is evidence-backed routing guidance, not a production benchmark and not a claim that one math RL workload generalizes to every serving workload.

## Reliable conclusions

The useful result is scheduler-oriented: config-only vLLM startup/runtime tuning has plateaued for this effort, and no more H200 launches are justified until prepared/lifecycle support exists. The serving lesson that transfers to llm-d is that **cap2048 plus request-shape-aware placement explained rollout-tail latency better than raw model speed alone**.

Four request shapes drove the observed tail:

| Shape | Observed behavior | llm-d implication |
| --- | --- | --- |
| Uncapped reasoning loops | Some Phi-4 math rollouts ran to a 4096-token generation cap and occupied decode capacity for roughly 90s. | Output caps are a workload policy knob; they bound per-request overhang but must be evaluated for quality impact. |
| Capped cap-saturated requests | With `max_completion_tokens=2048`, mean decode length was near the cap and many requests still consumed the full budget. | A cap reduces worst-case work but does not classify all capped requests as cheap. |
| Short/easy requests | Short arithmetic-style prompts completed far earlier than cap-saturated reasoning prompts. | Routing and flow-control evals need mixed cheap/expensive traffic, not only uniform synthetic prompts. |
| Backend/rank stragglers under cap | Capped requests still reached about 144-155s request wall depending on placement/load. | Per-replica decode health and replayable routing evidence matter after token length is bounded. |

The causal model is:

1. **Within-request overhang:** cap-saturated reasoning loops consume long autoregressive decode spans.
2. **Between-request interference:** cheap and expensive rollout requests share backend capacity and continuous batches.
3. **Backend/rank stragglers:** capped requests can still become tail requests when placed on a slow, full, or stale-snapshot backend/rank.

The validated H200 rows should therefore inform tail-aware scheduling and evaluation, not another H200 hill-climb on current fresh-RayJob configs.

## Fit with current llm-d surfaces

The current EPP model is already close to the right abstraction:

- The request scheduler uses a **Filter -> Score -> Pick** pipeline, so a tail-aware policy can be added as a scorer/picker/profile rather than a router rewrite.
- Existing scorers already cover token load, running requests, queue depth, KV utilization, prefix/cache locality, and latency headroom.
- Flow control can hold requests at the EPP instead of letting them become immovable local backend queue entries.
- P/D disaggregation can route prefill and decode through separate profiles, which is useful because this evidence points primarily at decode-tail placement once generation length is bounded.

The missing llm-d-facing pieces are sharper than the existing generic load signals:

- **Per-replica decode health:** route with live or recently sampled decode throughput, completed requests per second, running/waiting request counts, KV/cache pressure, and request-wall history per backend or rank.
- **Request-shape classification:** carry prompt tokens, requested output cap, predicted output length, and "likely long reasoning" hints into scheduling and flow-control decisions.
- **Tail-aware picking:** compare least-inflight with projected-remaining-decode-work and small-wave minimax-style placement. The H200 wave16/wave32 result warns that larger lookahead can overcommit from stale state under continuous batching.
- **Replayable router evidence:** record candidate metrics, selected endpoint, request shape, predicted tokens, actual tokens, request wall, queue state, and final outcome so policy choices can be audited offline.

## Actionable llm-d plan

Start with docs and replay/eval scaffolding before broad runtime changes.

1. **Define evidence fields for router replay.** Start with the decision record contract below, then add or extend the EPP/py-scheduler trace format with request shape, selected endpoint, candidate endpoint metrics, predicted/actual output tokens, request wall, queue state, and scheduling policy/version.
2. **Add a mixed-workload routing eval.** Build a CPU/local replay trace shaped like the H200 evidence: short ~700-token completions, capped 2048-token reasoning completions, uncapped/cap-equivalent 4096-token reasoning loops, and backend/rank straggler cases.
3. **Compare policy baselines.** Evaluate least-inflight, least-projected-decode-work, and conservative small-wave tail-aware assignment against the same replay trace.
4. **Promote only with tail and throughput evidence.** Require p95/p99 or max request-wall improvement without unacceptable completed-RPS, decode-TPS, fairness, or quality regression.
5. **Keep caps separate from routing.** Treat per-workload output caps as a workload policy owned by the caller/operator; do not hide backend/rank straggler risk behind caps.

## Router decision record contract

A first replay/eval implementation should emit one record per scheduling attempt. Keep the contract narrow enough to implement in EPP or py-scheduler, but complete enough that an offline replay can compare least-inflight, projected-decode-work, and tail-aware policies against the same candidate state.

| Group | Required fields | Why it matters |
| --- | --- | --- |
| Request | `request_id`, arrival timestamp, model, prompt tokens, requested output cap, predicted output-token bucket, request-shape hint, optional SLO/deadline class. | Separates short/easy traffic from capped-long and uncapped-long traffic before scoring. |
| Policy | Policy name/version, scorer weights, plugin/profile names, fallback policy, fallback reason when tail-aware inputs are missing. | Prevents a replay from mixing policy revisions or presenting a baseline fallback as a tail-aware decision. |
| Candidates | Endpoint/rank ID, metric timestamp and age, running requests, waiting requests, KV/cache pressure, decode TPS, completed RPS, recent request-wall summary, projected remaining decode work, candidate score, filtered reason. | Captures whether a backend looked healthy, stale, or degraded before dispatch. |
| Decision | Selected endpoint/rank, selected score, baseline least-inflight endpoint, rejected near-tie candidates, human-readable reason. | Makes it possible to explain why the policy did not simply choose the fewest in-flight requests. |
| Outcome | Actual output tokens, cap-saturation flag, queue time, decode time, request wall time, success/error, quality or truncation signal when available. | Ties the dispatch decision to realized tail, throughput, and cap/quality tradeoffs. |

Minimum synthetic fixture rows, represented in [`h200-rollout-tail-routing-fixture.json`](./h200-rollout-tail-routing-fixture.json) and validated by [`check-h200-rollout-tail-routing-fixture.py`](./check-h200-rollout-tail-routing-fixture.py):

```bash
make validate-rollout-tail-replay
```

| Case ID | Shape | Candidate setup | Replay assertion |
| --- | --- | --- | --- |
| `h200-long-4096` | Predicted-long uncapped reasoning loop. | Two endpoints have similar in-flight counts, but one has lower decode TPS and higher projected remaining decode work. | Tail-aware policy chooses the healthier endpoint; least-inflight is recorded as an explicit baseline. |
| `h200-capped-2048` | Capped request with actual output near cap. | A low-queue endpoint is already carrying long decode work while a slightly busier endpoint has better decode TPS. | Cap is treated as bounded maximum work, not proof the request is cheap. |
| `h200-short-700` | Short/easy contrast request. | Healthy endpoints include one currently reserved or preferred for long work. | Short-request p95 does not regress materially while long-tail placement improves. |
| `h200-straggler-2048` | Capped-long backend/rank straggler. | The selected endpoint has stale or degraded per-replica metrics that should be visible in the record. | Replay can attribute the straggler to degraded state, stale metrics, or missing evidence; otherwise the policy fails closed to a named baseline. |

## Router replay acceptance checklist

A first llm-d/epp or py-scheduler replay harness should be accepted only when it can explain every decision using request shape plus per-replica state. The fixture can be synthetic or trace-derived, but it must keep the four H200 shapes distinct so policy changes cannot pass by optimizing only the easy cases.

| Replay case | Required replay inputs | Required scheduler signals | Expected decision evidence | Stop/go criterion |
| --- | --- | --- | --- | --- |
| Uncapped 4096-token reasoning loop | Prompt tokens, requested `max_tokens=4096`, predicted output length bucket, actual output tokens, request wall. | Per-endpoint running/waiting requests, projected remaining decode work, decode TPS, request-wall history. | The chosen endpoint is not justified by least-inflight alone; the record shows why the selected replica has lower projected tail risk than candidates with similar queue depth. | Go if the policy avoids stacking multiple predicted-long requests on the same degraded replica; stop if the decision record cannot distinguish one 4096-token request from many short requests. |
| Capped 2048-token cap-saturated request | Prompt tokens, requested `max_tokens=2048`, actual output tokens near cap, truncation/cap-saturation flag, request wall. | Decode TPS, completed RPS, queue depth, KV/cache pressure, projected remaining decode work. | The record treats the cap as a bounded maximum, not as proof the request is cheap; candidate scoring shows placement pressure for already-long or near-cap work. | Go if capped-long traffic is spread away from low-throughput or backed-up replicas; stop if all capped requests collapse into the same score as short capped requests. |
| Short/easy contrast request | Prompt tokens, requested cap, actual output tokens around the short-request bucket, request wall. | Queue/running counts, completed RPS, optional SLO/deadline, prefix/cache signal when available. | The policy can route short work without over-penalizing healthy replicas reserved for long work; the evidence shows whether short requests are delayed behind long reasoning loops. | Go if mixed workload p95/p99 improves without starving short requests; stop if tail-aware placement improves long-request max while materially increasing short-request latency. |
| Backend/rank straggler under cap | Capped-long request, selected endpoint/rank, candidate endpoint/rank metrics snapshot, actual request wall around the straggler bucket. | Per-replica decode TPS, completed RPS deficit, running/waiting requests, request-wall history, metric timestamp/staleness. | The replay explains whether the selected replica looked degraded before dispatch and whether staleness or missing health signals caused the bad placement. | Go if the policy demotes stale/degraded replicas or records insufficient evidence explicitly; stop if a 144-155s-style straggler cannot be attributed to candidate state, stale metrics, or missing inputs. |

Minimum acceptance for promoting a tail-aware policy:

- Compare against least-inflight on the same replay trace.
- Report request-wall p95, p99, max, completed RPS, decode TPS, and per-replica fairness/skew.
- Include policy version, scorer weights, candidate scores, selected endpoint, and enough raw inputs to replay the decision offline.
- Fail closed when required decode-health inputs are absent: either fall back to a named baseline policy or mark the evidence as insufficient, rather than presenting the decision as tail-aware.
- Keep quality and cap policy separate from routing metrics; a lower tail is not acceptable if it only comes from silently truncating useful completions.

## Proxyguard8 to llm-d two-lane plan

The latest Prime-RL `proxyguard8` result sharpens the llm-d direction: the useful mechanism is not "always route with guardrails." It is **two-lane dispatch** where normal/short traffic preserves high-throughput routing while predicted-long or cap-saturated traffic gets tail-spreading across distinct decode targets.

Proxyguard8 mechanics to translate:

| Prime-RL proxyguard8 behavior | llm-d/epp or py-scheduler equivalent |
| --- | --- |
| Treats client identity as `(api_base_url, X-data-parallel-rank)`, so ranks behind one router are separate placement targets. | Treat the routing target for decode-tail policy as an endpoint plus rank/replica identity when that identity is observable. If rank is hidden behind a backend service, replay evidence must mark the policy as unable to prove rank spreading. |
| Sorts refill groups by largest predicted completion first. | Classify predicted-long or cap-saturated requests before scoring, then evaluate the heaviest requests first in replay or batch-aware scheduling tests. |
| Picks the client with minimum projected score: projected load plus predicted load plus throughput and proxy/guardrail penalties. | Score candidates by projected remaining decode work plus per-replica decode health penalties, not only running request count or queue depth. |
| Updates projected load and assigned counts after each assignment. | Replay must model sequential assignment effects so multiple long requests do not all see the same stale snapshot and select the same rank. |
| Penalizes fewer completed rollouts than the best client, slower request wall, slower rollout-group wall, and longer completions; scales penalty by predicted completion load. | Candidate evidence should include completed RPS deficit, request-wall history, group/session-wall history when available, recent completion length, and a penalty that matters more for predicted-long requests than for short requests. |

Two-lane acceptance criteria:

| Lane | Traffic admitted | Preferred policy | Required proof |
| --- | --- | --- | --- |
| Short/normal lane | Short predicted output, non-saturated cap, or latency-sensitive requests where long-tail spreading is not expected to help. | Existing high-throughput routing such as latency/queue/running/token-load scoring, with prefix/cache policy when useful. | Completed RPS and decode TPS do not regress materially versus the baseline; short-request p95 stays within the configured tolerance. |
| Predicted-long lane | Large predicted output, requested cap near the workload maximum, cap-saturation history, or request/session history that resembles long reasoning loops. | Tail-spreading policy that distinguishes endpoint/rank targets and penalizes low decode TPS, low completed RPS, slow request wall, slow group wall, high projected decode load, and stale metrics. | Max and p99 request wall improve versus least-inflight or slack-style throughput routing, while throughput regression remains within the promotion budget. |

Replay stop/go checks for this lane split:

- **Go** if the replay shows predicted-long requests spread across endpoint/rank identities and records sequential projected-load updates after each long assignment.
- **Go** if short/normal traffic keeps using the throughput-oriented lane and its p95/completed-RPS/decode-TPS stay within the documented tolerance.
- **Stop** if the policy improves long-tail max only by routing all traffic through the guarded lane and materially hurts completed RPS or decode TPS.
- **Stop** if endpoint identity cannot distinguish DP ranks but the evidence claims rank-level spreading.
- **Stop** if penalty inputs are missing or stale and the decision record does not show a named fallback policy.

The observed proxyguard8 tradeoff should be preserved as an evaluation requirement: it improved tail max substantially versus slack-style routing, but hurt raw serving throughput/completed RPS. llm-d should therefore promote this only as a replay-guided lane for predicted-long traffic unless a mixed-workload evaluation proves the guarded policy is also safe as a default.

The fixture's `two_lane_acceptance` block makes this executable enough for future tests: predicted-long assignments must use endpoint/rank identities, consume projected long-load budget sequentially, spread across at least two rank targets, and stay within explicit tail/runtime/throughput/short-latency promotion budgets. Short/normal assignments must remain in the high-throughput lane and must not consume guarded projected long-load budget.

Likely code surfaces, if this moves beyond docs:

| Surface | Change direction |
| --- | --- |
| EPP request parsing / internal request model | Preserve requested output cap and request-shape hints for scorers. |
| EPP data layer / metrics ingestion | Surface per-endpoint decode health and request-wall history. |
| EPP scheduling plugins | Add projected decode work and/or tail-aware scorer/picker variants using existing Filter -> Score -> Pick hooks. |
| Flow control ordering policy | Optionally order queued requests by shape/SLO when pool saturation is active. |
| Router evidence tooling | Emit replayable decision records and add a local replay comparison harness. |

## Non-goals and cautions

- Do not treat the H200 rows as a full production benchmark.
- Do not overfit llm-d defaults to Phi-4 math RL rollouts.
- Do not launch more H200 or Kubernetes jobs for this handoff.
- Do not implement a broad router rewrite before the replay/eval surface can prove policy value.
- Do not compare scheduler replay p99 from scheduler rows directly against no-scheduler rows that lack replay data; use no-scheduler as a throughput/wall denominator and scheduler rows for scheduler-vs-scheduler tail comparisons.

## Source evidence

- `h200-rl-lab/docs/rollout-latency-scheduler.md`
- `h200-rl-lab/docs/llm-d-rollout-latency.html`
- `h200-rl-lab/configs/experiments/phi4-next-phase.json`
- `h200-rl-lab/scripts/check-rollout-latency-memo.py --self-test`
- `docs/research/h200-rollout-tail-routing-fixture.json`
- `make validate-rollout-tail-replay`
