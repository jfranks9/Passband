"""LLM curation over pre-clustered story groups via LiteLLM (OpenAI-compatible).

The model only ever sees cluster title + bounded snippet + source list — never
raw article bodies. One call per cluster. --dry-run skips the network entirely
and returns a deterministic stub so the whole pipeline can be tested offline.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass

from .cluster import Cluster
from .config import config

# A homelab proxy in front of LiteLLM will occasionally 429 (model cooldown),
# 504 (gateway timeout), or drop the connection, and a local model can simply be
# slow. A single bad call must never sink the whole edition: each per-cluster
# request is retried with backoff, and the cluster is skipped if it never
# succeeds — we ship the briefs that worked.
REQUEST_TIMEOUT = 180  # seconds per call; generous for slow local generations
MAX_ATTEMPTS = 3
BACKOFF_BASE = 4       # seconds between attempts: 4, 8, ...


class EmptyCompletion(RuntimeError):
    """The call succeeded at the transport layer but carried no usable text.

    Raised inside the retry loop so an empty body takes the same retry/backoff
    path as a 429 — a proxy that returns a null body under load usually
    recovers. The one case that is NOT retried is finish_reason == "length"
    (see _complete): that is a budget problem, and the identical request will
    reproduce it.
    """

SYSTEM = (
    "You are a news analyst writing a concise newsletter brief. "
    "For each story, write 1-2 tight sentences capturing what happened and why "
    "it matters. No preamble, no hedging, no markdown headers. Factual and dry."
)
RISK_SYSTEM = (
    "You are a risk analyst. For each event, write 1-2 sentences: what it "
    "is, its severity/scope, and the concrete second-order risk (supply chain, "
    "shortages, unrest, infrastructure). Factual, no alarmism, no markdown."
)


@dataclass
class Brief:
    title: str
    body: str
    url: str
    sources: list[str]


def _model_for(task: str) -> str:
    return config()["llm"].get("task_models", {}).get(task, "summarize")


def _client():
    from openai import OpenAI
    env = config()["env"]
    # Explicit timeout for slow local generations; disable the SDK's own retry
    # loop since we do our own retry/skip per cluster (see _complete).
    return OpenAI(
        base_url=env["litellm_base_url"],
        api_key=env["litellm_api_key"],
        timeout=REQUEST_TIMEOUT,
        max_retries=0,
    )


def _complete(client, label: str, **kwargs) -> str | None:
    """One LLM call with bounded retries and backoff.

    Returns the body text, or None if the call never succeeded — the caller
    skips that cluster. Backoff matters for the 429 case in particular: an
    immediate retry hits the same model cooldown that just rejected us.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = client.chat.completions.create(**kwargs)
            choice = resp.choices[0]
            # `content` is not guaranteed to be a non-empty string. A reasoning
            # model spends max_tokens on reasoning_content first and can return
            # None (-> AttributeError, previously logged as if it were a code
            # bug) or "" (-> previously accepted, rendering a headline with a
            # blank body). Both are failures; neither may reach a Brief.
            body = (choice.message.content or "").strip()
            if body:
                return body
            reason = getattr(choice, "finish_reason", None)
            if reason == "length":
                print(
                    f"curate: '{label}' hit the token budget before writing any "
                    f"text (finish_reason=length) — raise llm.defaults.max_tokens "
                    f"in config/llm.yaml; not retrying",
                    file=sys.stderr,
                )
                return None
            raise EmptyCompletion(f"empty completion (finish_reason={reason})")
        except Exception as e:  # noqa: BLE001 — proxy errors are varied (429/504/timeout)
            msg = str(e).splitlines()[0][:140]
            if attempt == MAX_ATTEMPTS:
                print(
                    f"curate: skipping '{label}' after {attempt} attempts "
                    f"({type(e).__name__}: {msg})",
                    file=sys.stderr,
                )
                return None
            wait = BACKOFF_BASE * (2 ** (attempt - 1))
            print(
                f"curate: attempt {attempt} failed for '{label}' "
                f"({type(e).__name__}: {msg}); retrying in {wait}s",
                file=sys.stderr,
            )
            time.sleep(wait)
    return None


def curate_section(
    clusters: list[Cluster],
    llm_task: str,
    max_clusters: int,
    dry_run: bool = False,
) -> list[Brief]:
    cfg = config()
    defaults = cfg["llm"].get("defaults", {})
    snippet_chars = defaults.get("cluster_snippet_chars", 600)
    system = RISK_SYSTEM if llm_task == "risk_synthesis" else SYSTEM

    clusters = clusters[:max_clusters]
    briefs: list[Brief] = []
    client = None if dry_run else _client()
    model = _model_for(llm_task)

    for c in clusters:
        rep = c.representative
        snippet = c.snippet(snippet_chars)
        if dry_run:
            body = (snippet[:200] + "…") if len(snippet) > 200 else snippet
            briefs.append(Brief(rep.title, body or rep.title, rep.url, c.sources))
            continue

        prompt = (
            f"Headline: {rep.title}\n"
            f"Sources: {', '.join(c.sources) or 'n/a'}\n"
            f"Context: {snippet}\n\n"
            "Write the brief now."
        )
        body = _complete(
            client,
            rep.title[:60],
            model=model,
            temperature=defaults.get("temperature", 0.2),
            max_tokens=defaults.get("max_tokens", 900),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        if body is None:  # never succeeded — drop this cluster, keep the edition
            continue
        briefs.append(Brief(rep.title, body, rep.url, c.sources))

    return briefs
