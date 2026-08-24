#!/usr/bin/env python3
"""Unit tests for scripts/populate_models.py model selection + status banner.

Pure/offline: every test feeds the selectors and the /jobs classifier fixture
payloads captured from the LIVE ALCF gateway on 2026-08-24, so no Globus token
and no network are needed.

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)

import populate_models as pm  # noqa: E402


# --- Fixtures: real /models entries, trimmed to the fields we read -----------

SOPHIA_MODELS = [
    {"id": "google/gemma-4-31B-it", "framework": "vllm",
     "max_model_len": 128000, "reasoning_parser": "gemma4"},
    {"id": "openai/gpt-oss-120b", "framework": "vllm", "max_model_len": 65536},
    # below the 64k floor -> excluded, and recorded for the banner
    {"id": "meta-llama/Llama-4-Maverick-17B-128E-Instruct", "framework": "vllm",
     "max_model_len": 16384},
    # no max_model_len but allowlisted -> kept
    {"id": "openai/gpt-oss-20b", "framework": "vllm"},
    # non-chat framework + embedding/science ids -> excluded
    {"id": "sam3", "framework": "sam3service"},
    {"id": "google/embeddinggemma-300m", "framework": "vllm"},
    {"id": "genslm-test/genslm-esmc-300M-codon", "framework": "vllm"},
]

METIS_MODELS = [
    {"id": "gemma-4-31B-it", "framework": "api"},
    {"id": "gpt-oss-120b", "framework": "api"},
    # verified 8192 on Metis -> below floor, excluded
    {"id": "Mistral-Large-3-675B-Instruct-2512", "framework": "api"},
]

MINERVA_MODELS = [
    {"id": "gpt-oss-120b", "framework": "api", "capabilities": {
        "context_window_tokens": 131072, "reasoning": {"supported": True},
        "api_protocols": ["chat_completions", "responses"]}},
    {"id": "inkling-bf16", "framework": "api", "capabilities": {
        "context_window_tokens": 262144, "reasoning": {"supported": True},
        "api_protocols": ["chat_completions", "responses"]}},
    {"id": "nemotron-3-ultra", "framework": "api", "capabilities": {
        "context_window_tokens": 262144, "reasoning": {"supported": True},
        "api_protocols": ["chat_completions", "responses"]}},
]

# Sophia was fully down at capture time: nothing running, two queued jobs whose
# estimated start was ~31 hours out.
SOPHIA_JOBS = {
    "running": [],
    "queued": [
        {"Models": "meta-llama/Llama-4-Maverick-17B-128E-Instruct",
         "Estimated Start Time": "Tue Aug 25 00:30:00 2026 (Chicago time)",
         "Job Comments": "Not Running: No available resources on nodes"},
        {"Models": "arcee-ai/Trinity-Large-Thinking-W4A16",
         "Estimated Start Time": "Tue Aug 25 00:30:00 2026 (Chicago time)",
         "Job Comments": "Not Running: No available resources on nodes"},
    ],
}

METIS_JOBS = {
    "running": [{"Models": "gemma-4-31B-it,gpt-oss-120b,"
                           "Mistral-Large-3-675B-Instruct-2512",
                 "Model Status": "running"}],
    "queued": [],
}


class TestSelectors(unittest.TestCase):
    def setUp(self):
        pm.EXCLUDED_BY_FLOOR.clear()

    def test_sophia_filters_and_windows(self):
        sel = pm._select_sophia(SOPHIA_MODELS)
        self.assertEqual(sel["google/gemma-4-31B-it"], (128000, True))
        self.assertEqual(sel["openai/gpt-oss-120b"], (65536, True))
        # allowlisted add-back keeps a model with no max_model_len
        self.assertEqual(sel["openai/gpt-oss-20b"][0], 128000)
        # non-chat frameworks, embeddings and science models are dropped
        for mid in ("sam3", "google/embeddinggemma-300m",
                    "genslm-test/genslm-esmc-300M-codon"):
            self.assertNotIn(mid, sel)

    def test_sub_floor_model_excluded_and_recorded(self):
        sel = pm._select_sophia(SOPHIA_MODELS)
        mid = "meta-llama/Llama-4-Maverick-17B-128E-Instruct"
        self.assertNotIn(mid, sel)
        self.assertEqual(pm.EXCLUDED_BY_FLOOR["sophia"][mid], 16384)

    def test_metis_uses_verified_windows_and_drops_mistral_large(self):
        sel = pm._select_metis(METIS_MODELS)
        self.assertEqual(sel["gemma-4-31B-it"], (131072, True))
        self.assertEqual(sel["gpt-oss-120b"], (131072, True))
        self.assertNotIn("Mistral-Large-3-675B-Instruct-2512", sel)
        self.assertEqual(
            pm.EXCLUDED_BY_FLOOR["metis"]["Mistral-Large-3-675B-Instruct-2512"], 8192)

    def test_minerva_prefers_capabilities_over_id_heuristic(self):
        sel = pm._select_minerva(MINERVA_MODELS)
        self.assertEqual(sel["gpt-oss-120b"], (131072, True))
        # REGRESSION GUARD: neither id matches REASONING_ID_PATTERNS, so without
        # reading capabilities.reasoning.supported these would be classed as
        # plain chat, land in the 2048-token baseline provider, and return empty
        # content (their reasoning channel eats the whole budget).
        self.assertFalse(pm._is_reasoning_id("inkling-bf16"))
        self.assertFalse(pm._is_reasoning_id("nemotron-3-ultra"))
        self.assertEqual(sel["inkling-bf16"], (262144, True))
        self.assertEqual(sel["nemotron-3-ultra"], (262144, True))

    def test_minerva_falls_back_to_heuristic_without_capabilities(self):
        sel = pm._select_minerva([
            {"id": "some-thinking-model", "framework": "api"},
            {"id": "plain-chat-model", "framework": "api"},
        ])
        self.assertEqual(sel["some-thinking-model"],
                         (pm.MINERVA_DEFAULT_CONTEXT, True))
        self.assertEqual(sel["plain-chat-model"],
                         (pm.MINERVA_DEFAULT_CONTEXT, False))


class TestJobClassification(unittest.TestCase):
    """_fetch_job_states parsing, exercised through the pure helpers."""

    def _states(self, doc):
        live = set()
        for job in doc.get("running", []) or []:
            if str(job.get("Model Status", "")).lower() in ("", "running"):
                live.update(pm._split_models_field(job))
        return live

    def test_comma_joined_models_field_is_split(self):
        live = self._states(METIS_JOBS)
        self.assertEqual(live, {"gemma-4-31B-it", "gpt-oss-120b",
                                "Mistral-Large-3-675B-Instruct-2512"})

    def test_offline_cluster_yields_no_live_models(self):
        # REGRESSION GUARD for the bug this change fixes: Sophia had zero running
        # models, so the OLD code labelled every offered model "cold (~10-15m)"
        # when the queue's estimated start was ~31 hours away.
        self.assertEqual(self._states(SOPHIA_JOBS), set())


class TestFormatting(unittest.TestCase):
    def test_context_window_rendered_per_model(self):
        mapping = {"a": (131072, True), "b": (128000, False), "c": (262144, True)}
        self.assertEqual(pm._fmt_model("a", mapping), "a (131k ctx)")
        self.assertEqual(pm._fmt_model("b", mapping), "b (128k ctx)")
        self.assertEqual(pm._fmt_model("c", mapping), "c (262k ctx)")

    def test_unknown_window_is_labelled_not_faked(self):
        self.assertEqual(pm._fmt_model("missing", {}), "missing (ctx unknown)")

    def test_extra_detail_appended(self):
        out = pm._fmt_model("a", {"a": (131072, True)}, "starts ~Tue Aug 25")
        self.assertEqual(out, "a (131k ctx; starts ~Tue Aug 25)")


class TestClusterRegistry(unittest.TestCase):
    def test_default_registry_has_three_serving_clusters(self):
        names = [c[0] for c in pm._clusters()]
        self.assertEqual(names, ["sophia", "metis", "minerva"])

    def test_toggles_drop_clusters(self):
        self.assertEqual([c[0] for c in pm._clusters(include_minerva=False)],
                         ["sophia", "metis"])
        self.assertEqual([c[0] for c in pm._clusters(include_metis=False)],
                         ["sophia", "minerva"])

    def test_provisioning_cluster_has_no_provider(self):
        # tara is registered upstream but serves nothing yet; it must never
        # produce a provider block (its models list is empty).
        names = [c[0] for c in pm._clusters()]
        for cluster in pm.PROVISIONING_CLUSTERS:
            self.assertNotIn(cluster, names)

    def test_every_cluster_has_a_nonempty_fallback(self):
        for cluster, provider, base, _sel, fallback in pm._clusters():
            self.assertTrue(fallback, f"{cluster} has an empty fallback")
            self.assertTrue(base.startswith(pm.INFER_HOST))
            self.assertTrue(provider.startswith("alcf-"))
            for mid, (ctx, _is_reasoning) in fallback.items():
                self.assertGreaterEqual(
                    ctx, pm.MIN_CONTEXT,
                    f"{cluster} fallback {mid} is below the context floor")


class TestEmit(unittest.TestCase):
    def test_reasoning_split_gives_each_class_its_own_max_tokens(self):
        mapping = {"chatty": (128000, False), "thinky": (262144, True)}
        block = "\n".join(pm._emit_cluster("alcf-x", "https://h/v1", mapping))
        self.assertIn("  - name: alcf-x\n", block + "\n")
        self.assertIn("- name: alcf-x-reasoning", block)
        self.assertIn(f"max_tokens: {pm.BASELINE_MAX_TOKENS}", block)
        self.assertIn(f"max_tokens: {pm.REASONING_MAX_TOKENS}", block)
        self.assertIn("context_length: 262144", block)

    def test_single_class_emits_one_provider(self):
        block = "\n".join(pm._emit_cluster(
            "alcf-x", "https://h/v1", {"thinky": (262144, True)}))
        self.assertIn("- name: alcf-x-reasoning", block)
        self.assertNotIn("- name: alcf-x\n", block + "\n")


if __name__ == "__main__":
    unittest.main()
