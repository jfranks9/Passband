"""Unit 5 — curation resilience: retry with backoff, then skip the cluster.

The property under test is an availability one, not a formatting one: a homelab
LiteLLM proxy returns 429 (model cooldown), 504 (gateway timeout), or simply
drops the connection often enough that a *single* bad cluster must never cost
the whole edition. ``curate_section`` retries each call ``MAX_ATTEMPTS`` times
with exponential backoff and, if it never succeeds, drops that one cluster and
carries on. ``test_one_dead_cluster_does_not_sink_the_edition`` is the
load-bearing case; everything else pins a detail that case depends on.

Two harness notes:

* ``_complete`` sleeps between attempts. Every test patches ``time.sleep`` with
  a recorder, which keeps the suite instant *and* makes the backoff schedule
  assertable — a silently-removed ``sleep`` would otherwise still pass.
* The backoff assertions are derived from ``BACKOFF_BASE``/``MAX_ATTEMPTS``
  rather than hardcoded as ``[4, 8]``. Retuning the constants is a deliberate
  act that should not have to touch this file; deleting the doubling is not.

No network: the fake client below is the only "transport", and the dry-run test
pins that the real one is never even constructed.
"""
from __future__ import annotations

import types

import pytest

from passband import curate as curate_mod
from passband.cluster import Cluster
from passband.curate import BACKOFF_BASE, MAX_ATTEMPTS, curate_section
from passband.models import Item

# The proxy failures this retry loop exists for. Spelled out as real exception
# types because the loop catches bare Exception — if it is ever narrowed, these
# are the shapes that must keep working.
TRANSPORT_ERRORS = [
    RuntimeError("429 model cooldown"),
    TimeoutError("504 gateway timeout"),
    ConnectionError("connection reset by peer"),
]


def failures(n: int) -> list[BaseException]:
    """Exactly ``n`` transport errors, cycling the shapes above.

    Exact length matters: a script longer than the attempts a cluster is
    allowed leaks its tail into the *next* cluster's calls, which silently
    turns a per-cluster test into a whole-run one.
    """
    return [TRANSPORT_ERRORS[i % len(TRANSPORT_ERRORS)] for i in range(n)]


# --------------------------------------------------------------------------
# Fakes


class _Response:
    """The attribute hops curate makes into an OpenAI-shaped response."""

    def __init__(self, content, finish_reason="stop"):
        message = types.SimpleNamespace(content=content)
        self.choices = [types.SimpleNamespace(message=message,
                                              finish_reason=finish_reason)]


class Truncated:
    """Script element: a response that ran out of budget mid-reasoning.

    A reasoning model spends max_tokens on reasoning_content first, so the
    caller gets finish_reason='length' with content None or "".
    """

    def __init__(self, content=None):
        self.content = content


class FakeClient:
    """An OpenAI-shaped client driven by a scripted outcome list.

    Each element of ``script`` is either an exception instance (raised) or a
    string (returned as the completion body). The script is consumed across
    the whole client, so a test can make cluster 1 fail and cluster 2 succeed.
    Running past the end returns a default body rather than raising, so a test
    that under-specifies fails on its own assertion instead of on StopIteration.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.script.pop(0) if self.script else "fallback body"
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, Truncated):
            return _Response(outcome.content, finish_reason="length")
        return _Response(outcome)


def make_cluster(title: str, summary: str = "some context") -> Cluster:
    """A one-item cluster. Real Item/Cluster, not a stub: ``representative``
    and ``snippet`` are the two seams curate_section leans on."""
    return Cluster(items=[
        Item(
            title=title,
            url=f"https://example.invalid/{title.replace(' ', '-').lower()}",
            summary=summary,
            section="news",
            source_title="Wire",
        )
    ])


# --------------------------------------------------------------------------
# Fixtures


@pytest.fixture
def sleeps(monkeypatch):
    """Record every backoff sleep instead of performing it."""
    recorded: list[float] = []
    monkeypatch.setattr(curate_mod.time, "sleep", recorded.append)
    return recorded


@pytest.fixture
def llm_config(config_dir):
    """Minimal llm.yaml so curate reads known defaults, not a developer's."""
    config_dir.write("llm.yaml", {
        "defaults": {"temperature": 0.1, "max_tokens": 500,
                     "cluster_snippet_chars": 600},
        "task_models": {"summarize": "test-model"},
    })
    return config_dir


@pytest.fixture
def client_factory(monkeypatch):
    """Install a FakeClient as the one curate_section will build."""

    def install(script) -> FakeClient:
        client = FakeClient(script)
        monkeypatch.setattr(curate_mod, "_client", lambda: client)
        return client

    return install


# --------------------------------------------------------------------------
# _complete: the retry primitive


def test_succeeds_without_retrying_or_sleeping(sleeps):
    """The happy path costs exactly one call and no wall-clock delay."""
    client = FakeClient(["  a brief  "])
    assert curate_mod._complete(client, "story", model="m") == "a brief"
    assert len(client.calls) == 1
    assert sleeps == []


def test_recovers_from_transient_failure(sleeps):
    """A call that fails then succeeds still returns a body."""
    client = FakeClient([TRANSPORT_ERRORS[0], "recovered body"])
    assert curate_mod._complete(client, "story", model="m") == "recovered body"
    assert len(client.calls) == 2


@pytest.mark.parametrize("error", TRANSPORT_ERRORS, ids=lambda e: type(e).__name__)
def test_every_transport_error_is_retryable(error, sleeps):
    """429, 504 and a dropped connection are all handled the same way."""
    client = FakeClient([error, "body"])
    assert curate_mod._complete(client, "story", model="m") == "body"
    assert len(client.calls) == 2


def test_gives_up_after_max_attempts(sleeps):
    """Permanent failure returns None and is bounded — it does not spin."""
    client = FakeClient(failures(MAX_ATTEMPTS))
    assert curate_mod._complete(client, "story", model="m") is None
    assert len(client.calls) == MAX_ATTEMPTS


def test_backoff_doubles_between_attempts(sleeps):
    """Waits are BACKOFF_BASE, then double each time, with no sleep after the
    final attempt — retrying a 429 immediately just hits the same cooldown."""
    curate_mod._complete(FakeClient(failures(MAX_ATTEMPTS)), "story", model="m")
    expected = [BACKOFF_BASE * (2 ** i) for i in range(MAX_ATTEMPTS - 1)]
    assert sleeps == expected
    assert len(sleeps) == MAX_ATTEMPTS - 1


def test_failure_log_names_the_cluster(sleeps, capsys):
    """A skip must say *which* story vanished, or the gap is undiagnosable.
    Diagnostics go to stderr; stdout carries the pipeline's progress lines."""
    curate_mod._complete(FakeClient(failures(MAX_ATTEMPTS)), "Cobalt supply squeeze",
                         model="m")
    lines = capsys.readouterr().err.splitlines()
    # Asserted per-line, not against the whole stream: the retry lines also name
    # the story, so a substring check over all of stderr passes even when the
    # skip line — the only record that a story left the edition — loses it.
    skip = [ln for ln in lines if "skipping" in ln]
    retry = [ln for ln in lines if "attempt" in ln and "failed" in ln]
    assert len(skip) == 1 and "Cobalt supply squeeze" in skip[0]
    assert "RuntimeError" in retry[0]  # the type, so the proxy fault is identifiable
    assert all("Cobalt supply squeeze" in ln for ln in retry)


# --------------------------------------------------------------------------
# curate_section: the property that matters


def test_one_dead_cluster_does_not_sink_the_edition(llm_config, client_factory,
                                                    sleeps):
    """The load-bearing case: cluster 1 never succeeds, cluster 2 does, and the
    edition ships with cluster 2 rather than raising."""
    client = client_factory(failures(MAX_ATTEMPTS) + ["good body"])
    briefs = curate_section(
        [make_cluster("Doomed story"), make_cluster("Healthy story")],
        "summarize", max_clusters=10,
    )
    assert [b.title for b in briefs] == ["Healthy story"]
    assert briefs[0].body == "good body"
    # MAX_ATTEMPTS burned on the dead cluster, then one clean call.
    assert len(client.calls) == MAX_ATTEMPTS + 1


def test_retry_budget_is_per_cluster(llm_config, client_factory, sleeps):
    """One cluster exhausting its retries must not leave the next with fewer:
    a bad first cluster would otherwise silently truncate the whole edition."""
    client = client_factory(
        failures(MAX_ATTEMPTS)                  # cluster 1: never succeeds
        + [TRANSPORT_ERRORS[0], "second body"]  # cluster 2: fails once, recovers
    )
    briefs = curate_section(
        [make_cluster("Doomed story"), make_cluster("Flaky story")],
        "summarize", max_clusters=10,
    )
    assert [b.title for b in briefs] == ["Flaky story"]
    assert len(client.calls) == MAX_ATTEMPTS + 2


def test_all_clusters_failing_yields_no_briefs(llm_config, client_factory, sleeps):
    """Total proxy outage degrades to an empty section, not an exception — the
    caller decides whether an empty edition is worth sending."""
    client_factory(failures(MAX_ATTEMPTS * 2))
    assert curate_section([make_cluster("A"), make_cluster("B")],
                          "summarize", max_clusters=10) == []


def test_brief_carries_cluster_identity_not_model_output(llm_config,
                                                         client_factory, sleeps):
    """Title/url/sources come from the cluster; only the body is the model's.
    Keeps a hallucinated headline out of the newsletter."""
    client_factory(["model body"])
    cluster = make_cluster("Real headline")
    brief = curate_section([cluster], "summarize", max_clusters=10)[0]
    assert brief.title == "Real headline"
    assert brief.url == cluster.representative.url
    assert brief.sources == ["Wire"]
    assert brief.body == "model body"


def test_dry_run_never_constructs_a_client(llm_config, monkeypatch):
    """--dry-run is the offline path: no client, therefore no network, even if
    LITELLM_BASE_URL points somewhere real."""

    def explode():
        raise AssertionError("dry_run must not construct an LLM client")

    monkeypatch.setattr(curate_mod, "_client", explode)
    briefs = curate_section([make_cluster("Offline story", "stub context")],
                            "summarize", max_clusters=10, dry_run=True)
    assert [b.title for b in briefs] == ["Offline story"]
    assert briefs[0].body == "stub context"


def test_max_clusters_bounds_the_call_count(llm_config, client_factory, sleeps):
    """Cost control: clusters beyond max_clusters cost zero LLM calls."""
    client = client_factory(["one", "two", "three"])
    briefs = curate_section([make_cluster(f"Story {i}") for i in range(5)],
                            "summarize", max_clusters=2)
    assert len(briefs) == 2
    assert len(client.calls) == 2


def test_configured_model_and_sampling_reach_the_call(llm_config, client_factory,
                                                      sleeps):
    """The llm.yaml knobs are wired through rather than silently defaulted."""
    client = client_factory(["body"])
    curate_section([make_cluster("Story")], "summarize", max_clusters=10)
    call = client.calls[0]
    assert call["model"] == "test-model"
    assert call["temperature"] == 0.1
    assert call["max_tokens"] == 500


def test_prompt_carries_no_raw_article_body(llm_config, client_factory, sleeps):
    """The cost/privacy contract: the model sees title, sources and a bounded
    snippet — never the full item text."""
    client = client_factory(["body"])
    cluster = make_cluster("Story", summary="x" * 5000)
    curate_section([cluster], "summarize", max_clusters=10)
    user_msg = client.calls[0]["messages"][1]["content"]
    assert "Story" in user_msg and "Wire" in user_msg
    assert len(user_msg) < 1200  # snippet is capped, not the whole 5k summary


# --------------------------------------------------------------------------
# Empty completions: the body is not guaranteed to be a non-empty string


@pytest.mark.parametrize("empty", [None, "", "   ", "\n\t "],
                         ids=["none", "empty", "spaces", "whitespace"])
def test_empty_body_is_never_returned_as_success(empty, sleeps):
    """The bug this guards: `""` used to pass `.strip()` and render as a
    headline with a blank body, and `None` used to raise AttributeError —
    logged as if curate had a code bug rather than the model returning nothing."""
    client = FakeClient([empty] * MAX_ATTEMPTS)
    assert curate_mod._complete(client, "story", model="m") is None
    assert len(client.calls) == MAX_ATTEMPTS


def test_empty_body_is_retried_and_can_recover(sleeps):
    """A null body from a proxy under load is transient, so it takes the same
    retry path as a 429 rather than failing the cluster outright."""
    client = FakeClient([None, "", "real body"])
    assert curate_mod._complete(client, "story", model="m") == "real body"
    assert len(client.calls) == 3


def test_empty_body_is_reported_as_such_not_as_a_type_error(sleeps, capsys):
    """The log must name the real fault. `AttributeError: 'NoneType' object has
    no attribute 'strip'` sends you reading curate.py instead of the model."""
    curate_mod._complete(FakeClient([None] * MAX_ATTEMPTS), "story", model="m")
    err = capsys.readouterr().err
    assert "EmptyCompletion" in err
    assert "AttributeError" not in err


def test_truncation_fails_fast_without_retrying(sleeps, capsys):
    """finish_reason='length' is a budget problem, not a transient one: the
    identical request reproduces it, so burning MAX_ATTEMPTS and 12s of backoff
    on it is pure waste."""
    client = FakeClient([Truncated(None), "would-be body"])
    assert curate_mod._complete(client, "story", model="m") is None
    assert len(client.calls) == 1
    assert sleeps == []
    err = capsys.readouterr().err
    assert "max_tokens" in err  # the message must say what to actually change


def test_truncated_empty_string_also_fails_fast(sleeps):
    """Truncation surfaces as either None or "" depending on the provider."""
    client = FakeClient([Truncated("")])
    assert curate_mod._complete(client, "story", model="m") is None
    assert len(client.calls) == 1


def test_no_brief_is_ever_rendered_with_a_blank_body(llm_config, client_factory,
                                                     sleeps):
    """End to end: a cluster whose model output is empty is dropped, not shipped
    as a headline with nothing under it."""
    client_factory([None] * MAX_ATTEMPTS + ["good body"])
    briefs = curate_section(
        [make_cluster("Silent story"), make_cluster("Healthy story")],
        "summarize", max_clusters=10,
    )
    assert [b.title for b in briefs] == ["Healthy story"]
    assert all(b.body.strip() for b in briefs)
