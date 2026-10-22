#!/usr/bin/env python3
"""Tests for Network Jobs helpers (prefs shortlist, ranker, later layers)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from network_jobs.inventory import MARKER_START  # noqa: E402
from network_jobs.prefs import match_listings, score_job  # noqa: E402
from network_jobs.rank import rank_corpus  # noqa: E402
from network_jobs.cli import main as helper_main  # noqa: E402


FIXTURES = ROOT / "fixtures"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


class MatchPrefsTests(unittest.TestCase):
    def setUp(self):
        self.prefs = _load("preferences.json")
        self.listings = _load("listings-prefs.json")

    def test_shortlist_counts_and_histogram(self):
        result = match_listings(self.listings, self.prefs, company="Example")
        titles = {j["title"] for j in result["jobs"]}
        self.assertIn("Senior Product Manager, Foundations", titles)
        self.assertIn("Staff Product Manager, Growth", titles)
        # Intern, engineering SF, sales, deal-breaker crypto, London onsite
        self.assertNotIn("Product Manager Intern", titles)
        self.assertNotIn("Senior Backend Engineer", titles)
        self.assertNotIn("Account Executive", titles)
        self.assertNotIn("Senior Product Manager, Crypto Trading", titles)
        self.assertNotIn("Senior Product Manager", titles)  # London
        self.assertGreaterEqual(result["nMatches"], 2)
        self.assertEqual(result["nListings"], 8)
        self.assertIn("Product", result["departments"])
        self.assertEqual(result["showing"], f"{result['nMatches']} of 8 match prefs")

    def test_shortlist_reports_composition_not_just_counts(self):
        # #8: a bare "N of M" reads as success even when one unintended
        # family dominates. Composition must be visible in the result.
        result = match_listings(self.listings, self.prefs, company="Example")
        for key in ("matchDepartments", "matchCategories", "reasonCounts",
                    "warnings"):
            self.assertIn(key, result)
        self.assertEqual(sum(result["matchDepartments"].values()),
                         result["nMatches"])
        self.assertEqual(sum(result["matchCategories"].values()),
                         result["nMatches"])
        self.assertGreaterEqual(
            sum(result["reasonCounts"].values()), result["nMatches"])
        # Fixture prefs carry dealBreakers, so no unconfirmed-filter warning.
        self.assertEqual(result["warnings"], [])

    def test_unconfirmed_empty_dealbreakers_warns(self):
        prefs = dict(self.prefs)
        prefs["dealBreakers"] = []
        result = match_listings(self.listings, prefs, company="Example")
        self.assertTrue(any("dealBreakers" in w for w in result["warnings"]))
        prefs["dealBreakersConfirmed"] = True
        result = match_listings(self.listings, prefs, company="Example")
        self.assertEqual(result["warnings"], [])

    def test_mismatch_reasons_recorded_on_hard_fail(self):
        prefs = dict(self.prefs)
        prefs["categories"] = ["legal"]
        result = match_listings(self.listings, prefs, company="Example")
        reasons = [r for j in result["allScored"] for r in j["matchReasons"]]
        self.assertIn("category-mismatch", reasons)


class ReviewMatchesTests(unittest.TestCase):
    def setUp(self):
        self.prefs = _load("preferences.json")
        self.listings = _load("listings-prefs.json")

    def test_title_stem_keeps_domain_qualifier(self):
        # #13: a bare headword collapses whole boards — retain the first
        # qualifier so families stay vetoable.
        from network_jobs.review import title_stem

        self.assertEqual(title_stem("Staff Product Manager, Payments"),
                         "product/payments")
        self.assertEqual(title_stem("Product Manager, Safeguards (Generalist)"),
                         "product/safeguards")
        self.assertEqual(title_stem("Research Product Manager, Labs"),
                         "research/product")
        self.assertEqual(title_stem("Senior Product Manager"), "product")
        self.assertEqual(title_stem("Staff PM"), "product")

    def test_cluster_groups_families(self):
        from network_jobs.review import cluster_matches

        jobs = [
            {"title": "Senior Product Manager, Payments", "category": "product",
             "location": "NYC", "matchScore": 12},
            {"title": "Staff PM, Payments", "category": "product",
             "location": "Remote", "matchScore": 10},
            {"title": "Data Center Architect", "category": "operations",
             "location": "Austin", "matchScore": 4},
        ]
        fams = cluster_matches(jobs, min_cluster=2)
        by_stem = {f["stem"]: f for f in fams}
        self.assertIn("product/payments", by_stem)
        self.assertEqual(by_stem["product/payments"]["count"], 2)
        self.assertEqual(by_stem["product/payments"]["medianScore"], 11.0)
        self.assertEqual(by_stem["product/payments"]["label"], "product/payments")
        self.assertEqual(by_stem["product/payments"]["suggestedVeto"], "payments")
        # Singleton folds into the mixed bucket at min_cluster=2.
        mixed = [f for f in fams if f["stem"] == "(smaller families)"]
        self.assertEqual(len(mixed), 1)
        self.assertEqual(mixed[0]["count"], 1)

    def test_explain_leaks_groups_vetoes_and_hard_fails(self):
        from network_jobs.review import explain_leaks

        scored = [
            {"matched": True, "matchReasons": ["category"]},
            {"matched": False, "veto": True,
             "matchReasons": ["dealBreaker:data center"]},
            {"matched": False, "veto": True,
             "matchReasons": ["dealBreaker:data center"]},
            {"matched": False, "veto": False,
             "matchReasons": ["category-mismatch"]},
        ]
        leaks = explain_leaks(scored)
        self.assertEqual(leaks["vetoed"], 2)
        self.assertEqual(leaks["vetoPhrases"], {"data center": 2})
        self.assertEqual(leaks["hardFailed"], 1)
        self.assertEqual(leaks["hardFailReasons"], {"category-mismatch": 1})

    def test_veto_rewrites_prefs_and_rematches(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-rev-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        triage = tmp / "triage" / "careers-example-2026-09-16"
        (triage / "index").mkdir(parents=True)
        shutil.copy(FIXTURES / "listings-prefs.json",
                    triage / "index" / "listings.json")
        data = tmp / "data"
        data.mkdir()
        shutil.copy(FIXTURES / "preferences.json", data / "preferences.json")
        rc = helper_main([
            "review-matches",
            "--triage-dir", str(triage),
            "--data", str(data),
            "--company", "Example",
            "--veto", "product manager",
            "--json",
        ])
        self.assertEqual(rc, 0)
        prefs = json.loads((data / "preferences.json").read_text())
        self.assertIn("product manager", prefs["dealBreakers"])
        self.assertTrue(prefs["dealBreakersConfirmed"])
        matches = json.loads((triage / "index" / "matches.json").read_text())
        self.assertEqual(matches["nMatches"], 0)

    def test_hybrid_nyc_or_remote_matches_remote_and_onsite_prefs(self):
        hybrid = next(j for j in self.listings if "or Remote" in j["location"])
        scored = score_job(hybrid, self.prefs, company="Example")
        self.assertTrue(scored["matched"])
        self.assertIn("nyc", scored["locationBuckets"])
        self.assertIn("remote", scored["locationBuckets"])

    def test_remote_only_does_not_take_london_onsite(self):
        prefs = dict(self.prefs)
        prefs["workModes"] = ["remote"]
        prefs["locationBuckets"] = ["remote"]
        prefs["onsiteLocations"] = []
        london = next(j for j in self.listings if "London" in j["location"])
        scored = score_job(london, prefs, company="Example")
        self.assertFalse(scored["matched"])

    def test_helper_writes_matches_and_inventory(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-match-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        triage = tmp / "triage" / "careers-example-2026-09-16"
        (triage / "index").mkdir(parents=True)
        shutil.copy(FIXTURES / "listings-prefs.json", triage / "index" / "listings.json")
        (triage / "INVENTORY.md").write_text("# Careers triage — Example\n\n- **Slug:** example\n")
        data = tmp / "data"
        (data / "resume").mkdir(parents=True)
        shutil.copy(FIXTURES / "preferences.json", data / "preferences.json")
        shutil.copy(FIXTURES / "resume-keywords.md", data / "resume" / "text.md")
        rc = helper_main([
            "match-prefs",
            "--triage-dir", str(triage),
            "--data", str(data),
            "--company", "Example",
        ])
        self.assertEqual(rc, 0)
        matches = json.loads((triage / "index" / "matches.json").read_text())
        self.assertEqual(matches["nListings"], 8)
        self.assertGreaterEqual(matches["nMatches"], 2)
        self.assertEqual(matches["ingestDefault"], "matches")
        inv = (triage / "INVENTORY.md").read_text()
        self.assertIn(MARKER_START, inv)
        self.assertIn("match prefs", inv)
        self.assertIn("Departments:", inv)


class RankerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nj-rank-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        corpus = self.tmp / "corpus"
        corpus.mkdir()
        jobs = [
            {
                "id": "ex-spm-1",
                "title": "Senior Product Manager, Foundations",
                "company": "Stripe",
                "department": "Product",
                "category": "product",
                "location": "New York, NY",
                "locationBucket": "nyc",
                "seniority": "senior",
                "url": "https://example.com/jobs/pm-1",
                "lastSeen": "2026-09-16",
            },
            {
                "id": "ex-eng-1",
                "title": "Senior Backend Engineer",
                "company": "Stripe",
                "department": "Infrastructure",
                "category": "engineering",
                "location": "San Francisco, CA",
                "locationBucket": "sf",
                "seniority": "senior",
                "url": "https://example.com/jobs/eng-1",
                "lastSeen": "2026-09-16",
            },
            {
                "id": "ex-pm-mid",
                "title": "Product Manager",
                "company": "Figma",
                "department": "Product",
                "category": "product",
                "location": "Remote",
                "locationBucket": "remote",
                "seniority": "mid",
                "url": "https://example.com/jobs/pm-mid",
                "lastSeen": "2026-09-16",
            },
        ]
        (corpus / "jobs-all.json").write_text(json.dumps(jobs, indent=2) + "\n")
        (corpus / "product-nyc-senior.json").write_text(json.dumps([jobs[0]], indent=2) + "\n")
        (corpus / "product-remote-mid.json").write_text(json.dumps([jobs[2]], indent=2) + "\n")
        (corpus / "engineering-sf-senior.json").write_text(json.dumps([jobs[1]], indent=2) + "\n")
        (corpus / "manifest.json").write_text(json.dumps({
            "lastUpdated": "2026-09-16T00:00:00Z",
            "totalJobs": 3,
            "categories": {
                "product": {
                    "count": 2,
                    "file": "product.json",
                    "byLocation": {
                        "nyc": {"senior": {"count": 1, "file": "product-nyc-senior.json"}},
                        "remote": {"mid": {"count": 1, "file": "product-remote-mid.json"}},
                    },
                },
                "engineering": {
                    "count": 1,
                    "file": "engineering.json",
                    "byLocation": {
                        "sf": {"senior": {"count": 1, "file": "engineering-sf-senior.json"}},
                    },
                },
            },
        }, indent=2) + "\n")
        shutil.copy(FIXTURES / "preferences.json", self.tmp / "preferences.json")
        (self.tmp / "resume").mkdir()
        shutil.copy(FIXTURES / "resume-keywords.md", self.tmp / "resume" / "text.md")

    def test_ranker_top_k_not_whole_dump(self):
        result = rank_corpus(data_dir=self.tmp, k=1, today="2026-09-16")
        self.assertEqual(result["k"], 1)
        self.assertGreaterEqual(result["n"], 1)
        self.assertEqual(result["showing"], f"1 of {result['n']}")
        self.assertEqual(len(result["jobs"]), 1)
        self.assertEqual(result["jobs"][0]["category"], "product")

    def test_helper_writes_ranked_file(self):
        rc = helper_main(["rank", "--data", str(self.tmp), "-k", "2"])
        self.assertEqual(rc, 0)
        ranked = json.loads((self.tmp / "search" / "ranked.json").read_text())
        self.assertIn("showing", ranked)
        self.assertLessEqual(ranked["k"], ranked["n"])
        self.assertLessEqual(len(ranked["jobs"]), 2)

    def test_tie_break_prefers_most_recently_seen(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-tie-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        corpus = tmp / "corpus"
        corpus.mkdir()
        jobs = [
            {
                "id": "old", "title": "Senior Product Manager", "company": "Stripe",
                "department": "Product", "category": "product", "location": "Remote",
                "locationBucket": "remote", "seniority": "senior",
                "url": "https://example.com/old", "lastSeen": "2026-09-10",
            },
            {
                "id": "new", "title": "Senior Product Manager", "company": "Figma",
                "department": "Product", "category": "product", "location": "Remote",
                "locationBucket": "remote", "seniority": "senior",
                "url": "https://example.com/new", "lastSeen": "2026-09-16",
            },
        ]
        (corpus / "product-remote-senior.json").write_text(json.dumps(jobs, indent=2) + "\n")
        (corpus / "manifest.json").write_text(json.dumps({
            "lastUpdated": "2026-09-16T00:00:00Z",
            "totalJobs": 2,
            "categories": {
                "product": {
                    "count": 2,
                    "file": "product.json",
                    "byLocation": {
                        "remote": {"senior": {"count": 2, "file": "product-remote-senior.json"}},
                    },
                },
            },
        }, indent=2) + "\n")
        shutil.copy(FIXTURES / "preferences.json", tmp / "preferences.json")
        (tmp / "resume").mkdir()
        shutil.copy(FIXTURES / "resume-keywords.md", tmp / "resume" / "text.md")
        result = rank_corpus(data_dir=tmp, k=2, today="2026-09-16")
        self.assertEqual(result["n"], 2)
        scores = [j["matchScore"] for j in result["jobs"]]
        self.assertEqual(scores[0], scores[1])
        self.assertEqual(result["jobs"][0]["id"], "new")
        self.assertEqual(result["jobs"][1]["id"], "old")


class PaginationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from urllib.parse import parse_qs, urlparse

        board = json.loads((FIXTURES / "fat-board" / "jobs.json").read_text())
        cls.jobs = board["jobs"]

        class Handler(BaseHTTPRequestHandler):
            jobs = cls.jobs

            def log_message(self, fmt, *args):
                return

            def _send(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                q = parse_qs(parsed.query)
                limit = int((q.get("limit") or ["10"])[0])
                if parsed.path.endswith("/offset"):
                    offset = int((q.get("offset") or ["0"])[0])
                    chunk = self.jobs[offset:offset + limit]
                    self._send({"jobs": chunk, "total": len(self.jobs)})
                    return
                if parsed.path.endswith("/cursor"):
                    cursor = int((q.get("cursor") or ["0"])[0])
                    chunk = self.jobs[cursor:cursor + limit]
                    nxt = cursor + limit if cursor + limit < len(self.jobs) else None
                    self._send({"results": chunk, "nextCursor": nxt})
                    return
                page = int((q.get("page") or ["1"])[0])
                start = (page - 1) * limit
                chunk = self.jobs[start:start + limit]
                payload = {"jobs": chunk, "total": len(self.jobs)}
                if start + limit < len(self.jobs):
                    payload["next"] = f"/jobs?page={page + 1}&limit={limit}"
                self._send(payload)

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _triage(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-page-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        triage = tmp / "triage"
        triage.mkdir()
        return triage

    def test_page_link_into_listings(self):
        from network_jobs.pagination import paginate, write_pagination

        triage = self._triage()
        url = f"http://127.0.0.1:{self.port}/jobs?page=1&limit=10"
        result = paginate(url, max_pages=10, page_size=10)
        self.assertGreaterEqual(result["pagination"]["pages"], 4)
        self.assertTrue(result["pagination"]["complete"])
        self.assertFalse(result["pagination"]["truncated"])
        # Fixture holds 38 rows but 3 are the same Senior PM posting (36 unique).
        self.assertEqual(len(result["listings"]), 36)
        quiet = write_pagination(triage, result, company="FatBoard")
        self.assertTrue((triage / "index" / "pagination.json").is_file())
        inv = (triage / "INVENTORY.md").read_text()
        self.assertIn("complete=true", inv)
        self.assertEqual(quiet["pages"], result["pagination"]["pages"])

    def test_max_pages_marks_incomplete(self):
        from network_jobs.pagination import paginate

        url = f"http://127.0.0.1:{self.port}/jobs?page=1&limit=10"
        result = paginate(url, max_pages=2, page_size=10)
        self.assertEqual(result["pagination"]["pages"], 2)
        self.assertFalse(result["pagination"]["complete"])
        self.assertTrue(result["pagination"]["truncated"])

    def test_offset_and_cursor_schemes(self):
        from network_jobs.pagination import paginate

        off = paginate(f"http://127.0.0.1:{self.port}/offset?offset=0&limit=10", max_pages=10, page_size=10)
        self.assertEqual(len(off["listings"]), 36)  # 38 rows, 36 unique
        self.assertIn(off["pagination"]["scheme"], {"offset", "link"})
        self.assertTrue(off["pagination"]["complete"])
        cur = paginate(f"http://127.0.0.1:{self.port}/cursor?cursor=0&limit=10", max_pages=10, page_size=10)
        self.assertEqual(len(cur["listings"]), 36)  # 38 rows, 36 unique
        self.assertEqual(cur["pagination"]["scheme"], "cursor")
        self.assertTrue(cur["pagination"]["complete"])

    def test_locations_array_preserved(self):
        from network_jobs.pagination import paginate

        url = f"http://127.0.0.1:{self.port}/jobs?page=1&limit=10"
        result = paginate(url, max_pages=10, page_size=10)
        hybrid = [j for j in result["listings"] if j.get("locations")]
        self.assertTrue(hybrid)
        self.assertIn("Remote", hybrid[0]["locations"])

    def test_repeated_page_stops_and_dedupes(self):
        from network_jobs.pagination import paginate
        from network_jobs.fingerprint import fingerprint

        # Server ignores invented page params and returns the same full page.
        jobs = [
            {"title": f"Engineer {i}", "url": f"https://x.io/jobs/{i}", "location": "New York, NY"}
            for i in range(60)
        ]
        payload = {"jobs": jobs}

        def fake_fetch(url):
            import json as _json
            return payload, 200, _json.dumps(payload).encode()

        result = paginate("https://x.io/api/jobs", fetch=fake_fetch, max_pages=5, page_size=50)
        pag = result["pagination"]
        self.assertLess(pag["pages"], 5)
        self.assertTrue(pag["complete"])
        self.assertFalse(pag["truncated"])
        fps = [fingerprint(j) for j in result["listings"]]
        self.assertEqual(len(fps), len(set(fps)))
        self.assertEqual(len(result["listings"]), 60)


class FingerprintExpiryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nj-fp-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.corpus = self.tmp / "corpus"
        self.corpus.mkdir()
        os.environ["NETWORK_JOBS_HOME"] = str(self.tmp)

    def tearDown(self):
        os.environ.pop("NETWORK_JOBS_HOME", None)

    def _job(self, **kw):
        base = {
            "title": "Senior Product Manager",
            "company": "FatBoard",
            "category": "product",
            "location": "New York, NY",
            "locationBucket": "nyc",
            "seniority": "senior",
            "url": "https://fatboard.example/jobs/1",
            "status": "open",
        }
        base.update(kw)
        return base

    def test_dedupe_by_ats_id_not_url(self):
        from network_jobs.corpus import merge_jobs, job_fingerprint

        a = self._job(externalId="1", url="https://fatboard.example/jobs/1")
        b = self._job(externalId="1", url="https://fatboard.example/jobs/1?src=dup")
        merged, _ = merge_jobs([], [a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(job_fingerprint(a), job_fingerprint(b))

    def test_dedupe_by_company_title_location(self):
        from network_jobs.corpus import merge_jobs

        a = self._job(url="https://fatboard.example/jobs/a")
        b = self._job(url="https://careers.fatboard.example/spm")
        merged, _ = merge_jobs([], [a, b])
        self.assertEqual(len(merged), 1)

    def test_merge_reconciles_externalid_across_fingerprint_drift(self):
        # #25: incoming job with same externalId but different fingerprint
        # (old format with company vs new format without) should merge into
        # existing entry, not duplicate. merge_info.driftWarnings records it.
        from network_jobs.corpus import merge_jobs

        # Existing job with old-style fingerprint (id:company:externalId)
        existing = [self._job(externalId="7812856", fingerprint="id:stripe:7812856",
                              company="Stripe", title="Product Manager", location="NYC")]
        # Incoming job with new-style fingerprint (id:externalId) - same ATS id
        incoming = [self._job(externalId="7812856", fingerprint="id:7812856",
                              company="Stripe", title="Product Manager", location="NYC",
                              description="Updated description")]
        merged, info = merge_jobs(existing, incoming)
        self.assertEqual(len(merged), 1, "Should merge, not duplicate")
        self.assertEqual(merged[0]["description"], "Updated description")
        self.assertTrue(info["driftWarnings"])
        self.assertIn("7812856", info["driftWarnings"][0])
        self.assertIn("merging into existing", info["driftWarnings"][0])

    def test_expire_only_when_pagination_complete(self):
        from network_jobs.corpus import merge_jobs, write_shards

        existing = [
            self._job(externalId="1", url="https://fatboard.example/jobs/1"),
            self._job(title="Staff Engineer", externalId="2", url="https://fatboard.example/jobs/2",
                      category="engineering", seniority="senior"),
        ]
        incoming = [self._job(externalId="1", url="https://fatboard.example/jobs/1")]
        kept, _ = merge_jobs(existing, incoming, expire_company="FatBoard", pagination_complete=False)
        statuses = {j["externalId"]: j.get("status", "open") for j in kept}
        self.assertEqual(statuses["2"], "open")
        closed, _ = merge_jobs(existing, incoming, expire_company="FatBoard", pagination_complete=True)
        statuses = {j["externalId"]: j.get("status", "open") for j in closed}
        self.assertEqual(statuses["1"], "open")
        self.assertEqual(statuses["2"], "closed")
        summary = write_shards(self.corpus, closed)
        self.assertEqual(summary["totalJobs"], 1)
        self.assertEqual(summary["closedJobs"], 1)
        all_jobs = json.loads((self.corpus / "jobs-all.json").read_text())
        self.assertEqual(len(all_jobs), 2)

    def test_expiry_skipped_on_filtered_batch(self):
        # #16: a matches-only batch over a bigger board must not close live
        # roles, even with --pagination-complete. Refusal is loud, not fatal.
        from network_jobs.corpus import merge_jobs

        existing = [
            self._job(externalId="1", url="https://fatboard.example/jobs/1"),
            self._job(title="Staff Engineer", externalId="2", url="https://fatboard.example/jobs/2",
                      category="engineering", seniority="senior"),
            self._job(title="Designer", externalId="3", url="https://fatboard.example/jobs/3"),
        ]
        incoming = [self._job(externalId="1", url="https://fatboard.example/jobs/1")]
        merged, info = merge_jobs(
            existing, incoming, expire_company="FatBoard",
            pagination_complete=True, expected_total=3)
        statuses = {j["externalId"]: j.get("status", "open") for j in merged}
        self.assertEqual(statuses["2"], "open")
        self.assertEqual(statuses["3"], "open")
        self.assertTrue(info["expirySkipped"])
        self.assertIn("filtered", info["expiryReason"])

    def test_expiry_proceeds_on_full_coverage_and_force(self):
        from network_jobs.corpus import merge_jobs

        existing = [
            self._job(externalId="1", url="https://fatboard.example/jobs/1"),
            self._job(title="Old", externalId="gone", url="https://x/gone"),
        ]
        incoming = [self._job(externalId="1", url="https://fatboard.example/jobs/1")]
        # Full coverage (seen == expected): proceeds.
        merged, info = merge_jobs(
            existing, incoming, expire_company="FatBoard",
            pagination_complete=True, expected_total=1)
        self.assertFalse(info["expirySkipped"])
        self.assertEqual(info["expired"], 1)
        # Subset + force: operator override proceeds.
        merged, info = merge_jobs(
            existing, incoming, expire_company="FatBoard",
            pagination_complete=True, expected_total=9, force_expire=True)
        self.assertFalse(info["expirySkipped"])
        self.assertEqual(info["expired"], 1)
        # Legacy path (no expected_total): unchanged behavior.
        merged, info = merge_jobs(
            existing, incoming, expire_company="FatBoard",
            pagination_complete=True)
        self.assertFalse(info["expirySkipped"])
        self.assertEqual(info["expired"], 1)

    def test_rebuild_helper_honors_complete_flag(self):
        from network_jobs.corpus import rebuild

        existing = [self._job(externalId="keep"), self._job(title="Old Role", externalId="gone", url="https://x/gone")]
        (self.corpus / "jobs-all.json").write_text(json.dumps(existing) + "\n")
        batch = self.tmp / "batch.json"
        batch.write_text(json.dumps([self._job(externalId="keep")]) + "\n")
        incomplete = rebuild(batch, self.corpus, expire_company="FatBoard", pagination_complete=False)
        jobs = json.loads((self.corpus / "jobs-all.json").read_text())
        self.assertEqual(sum(1 for j in jobs if j.get("status") == "closed"), 0)
        complete = rebuild(batch, self.corpus, expire_company="FatBoard", pagination_complete=True)
        self.assertTrue(complete["expired"])
        jobs = json.loads((self.corpus / "jobs-all.json").read_text())
        closed = [j for j in jobs if j.get("status") == "closed"]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["externalId"], "gone")
        self.assertFalse(incomplete["expired"])


class ClassifierLocationTests(unittest.TestCase):
    def test_track_and_category_deterministic(self):
        from network_jobs.classify import classify_job

        pm = classify_job({"title": "Senior Product Manager", "department": "Product", "location": "NYC"}, company="Stripe")
        self.assertEqual(pm["category"], "product")
        self.assertEqual(pm["track"], "ic")
        self.assertEqual(pm["categoryConfidence"], "high")
        em = classify_job({"title": "Engineering Manager", "location": "Remote"}, company="Stripe")
        self.assertEqual(em["category"], "engineering")
        self.assertEqual(em["track"], "manager")
        self.assertIn("track", em)

    def test_explicit_manager_titles_are_high_confidence(self):
        from network_jobs.classify import classify_track

        self.assertEqual(classify_track("Engineering Manager"), ("manager", "high", False))
        self.assertEqual(classify_track("Director of Engineering"), ("manager", "high", False))
        # IC-flavored manager titles stay IC.
        self.assertEqual(classify_track("Product Manager", "Product"), ("ic", "high", False))
        self.assertEqual(classify_track("Staff Engineer"), ("ic", "high", False))

    def test_intern_and_staff_plus_signals(self):
        from network_jobs.classify import classify_job

        intern = classify_job({"title": "Product Manager Intern", "location": "New York, NY"}, company="X")
        self.assertEqual(intern["seniority"], "mid")
        self.assertIn("intern", intern["senioritySignals"])
        staff = classify_job({"title": "Staff Product Manager", "location": "Remote"}, company="X")
        self.assertEqual(staff["seniority"], "senior")
        self.assertIn("staff+", staff["senioritySignals"])

    def test_ambiguous_titles_flag_needs_llm(self):
        from network_jobs.classify import classify_listings

        result = classify_listings([{"title": "Lead", "location": "Remote"}], company="X")
        self.assertGreaterEqual(result["ambiguous"], 1)
        self.assertTrue(result["jobs"][0]["needsLlm"])

    def test_hybrid_hits_both_shards(self):
        from network_jobs.classify import classify_job
        from network_jobs.corpus import write_shards

        tmp = Path(tempfile.mkdtemp(prefix="nj-hyb-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        job = classify_job({
            "title": "Staff Product Manager",
            "location": "New York, NY or Remote",
            "url": "https://example.com/hybrid",
        }, company="FatBoard")
        self.assertIn("nyc", job["locationBuckets"])
        self.assertIn("remote", job["locationBuckets"])
        write_shards(tmp, [job])
        self.assertTrue((tmp / "product-nyc-senior.json").is_file())
        self.assertTrue((tmp / "product-remote-senior.json").is_file())
        nyc = json.loads((tmp / "product-nyc-senior.json").read_text())
        remote = json.loads((tmp / "product-remote-senior.json").read_text())
        self.assertEqual(nyc[0]["fingerprint"], remote[0]["fingerprint"])

    def test_onsite_locations_scope_city_not_every_office(self):
        from network_jobs.prefs import score_job

        prefs = json.loads((FIXTURES / "preferences.json").read_text())
        london = score_job({
            "title": "Senior Product Manager",
            "location": "London, UK",
            "department": "Product",
        }, prefs, company="Example")
        nyc = score_job({
            "title": "Senior Product Manager",
            "location": "New York, NY",
            "department": "Product",
        }, prefs, company="Example")
        self.assertFalse(london["matched"])
        self.assertTrue(nyc["matched"])
        self.assertEqual(nyc["locations"][0]["city"], "New York")

    def test_classify_helper(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-clf-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        src = tmp / "in.json"
        src.write_text(json.dumps([
            {"title": "Senior Product Manager", "location": "New York, NY"},
            {"title": "Lead", "location": "Remote"},
        ]) + "\n")
        dest = tmp / "out.json"
        rc = helper_main(["classify", "--input", str(src), "--out", str(dest), "--company", "Stripe"])
        self.assertEqual(rc, 0)
        jobs = json.loads(dest.read_text())
        self.assertEqual(jobs[0]["track"], "ic")
        self.assertEqual(jobs[0]["category"], "product")
        self.assertTrue(any(j.get("needsLlm") for j in jobs))

    def test_classify_accepts_matches_envelope(self):
        # `match-pregs` writes {"jobs": [...]}; `fetch-descriptions` writes a
        # bare array. The documented classify step follows match-preps, so it
        # has to take the envelope without the caller extracting it by hand.
        tmp = Path(tempfile.mkdtemp(prefix="nj-clf-env-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        rows = [{"title": "Senior Product Manager", "location": "New York, NY"},
                {"title": "Engineering Manager", "location": "Remote"}]

        dest = tmp / "out.json"
        src = tmp / "env.json"
        src.write_text(json.dumps({"jobs": rows}) + "\n")
        rc = helper_main(["classify", "--input", str(src), "--out", str(dest),
                          "--company", "Stripe"])
        self.assertEqual(rc, 0)
        env_jobs = json.loads(dest.read_text())

        bare = tmp / "bare.json"
        bare.write_text(json.dumps(rows) + "\n")
        rc = helper_main(["classify", "--input", str(bare), "--out", str(dest),
                          "--company", "Stripe"])
        self.assertEqual(rc, 0)
        self.assertEqual(env_jobs, json.loads(dest.read_text()))

    def test_classify_rejects_unrecognised_shape(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-clf-bad-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        src = tmp / "bad.json"
        src.write_text(json.dumps({"results": [{"title": "PM"}]}) + "\n")
        rc = helper_main(["classify", "--input", str(src),
                          "--out", str(tmp / "out.json"), "--company", "Stripe"])
        self.assertEqual(rc, 1)


class ProductTitleClassificationTests(unittest.TestCase):
    """#31: "product" must be the role's function, not a word in the title."""

    GENUINE = [
        "Senior Product Manager, Growth",
        "Head of Product",
        "Principal Product Manager",
        "VP, Product",
        "Director of Product Management",
        "Group Product Manager",
        "Manager, Product Program Management",
    ]

    def _category(self, title):
        from network_jobs.classify import classify_job

        return classify_job({"title": title, "location": "Remote"})

    def test_sourcing_and_incident_response_are_not_product(self):
        for title in (
            "New Product Introduction (NPI) Sourcing Manager, Data Center Power & Cooling",
            "Incident Response Manager - Product & Engineering",
        ):
            out = self._category(title)
            self.assertNotEqual(out["category"], "product", title)
            self.assertIn(out["category"], ("operations", "other"), title)
            self.assertEqual(out["categoryConfidence"], "low", title)
            self.assertTrue(out["needsLlm"], title)

    def test_genuine_product_titles_stay_high_confidence(self):
        for title in self.GENUINE:
            out = self._category(title)
            self.assertEqual(out["category"], "product", title)
            self.assertEqual(out["categoryConfidence"], "high", title)

    def test_product_in_trailing_qualifier_does_not_match(self):
        for title in (
            "Facilities Manager - Product",
            "Facilities Manager \u2014 Product",
            "Facilities Manager | Product",
            "Warehouse Manager, Product",
        ):
            out = self._category(title)
            self.assertNotEqual(out["category"], "product", title)

    def test_function_word_after_product_is_not_a_product_leader(self):
        # The leader pattern used to stop at "product" and ignore the next
        # word, so titles that lead a different function still earned
        # product/high (and the full +5 on a VP-Product search) (#37 review).
        for title in ("Head of Product Design", "Director, Product Design"):
            out = self._category(title)
            self.assertEqual(out["category"], "design", title)
            self.assertEqual(out["categoryConfidence"], "high", title)

        out = self._category("Head of Product Security")
        self.assertNotEqual(out["category"], "product")
        self.assertEqual(out["category"], "it-security")
        self.assertEqual(out["categoryConfidence"], "low")
        self.assertTrue(out["needsLlm"])

    def test_leader_with_unlisted_function_word_fails_closed(self):
        # What may follow "product" is a whitelist (end of head or a product
        # role noun), so a function word we have not enumerated keeps its own
        # category via the weak map and asks for LLM follow-up instead of
        # inheriting product's confident +5.
        for title, category in (
            ("Head of Product Finance", "finance"),
            ("Head of Product Data", "data"),
        ):
            out = self._category(title)
            self.assertEqual(out["category"], category, title)
            self.assertEqual(out["categoryConfidence"], "low", title)
            self.assertTrue(out["needsLlm"], title)

    def test_bare_product_word_is_not_a_category_signal(self):
        # No weak product fallback remains, so a title that only mentions the
        # word cannot claim the category (and its +5) on a guess.
        out = self._category("Product Wizard")
        self.assertEqual(out["category"], "other")
        self.assertEqual(out["categoryConfidence"], "low")
        self.assertTrue(out["needsLlm"])

    def test_other_weak_tokens_remain_low_confidence(self):
        # The remaining bare-token fallbacks still emit low confidence, so
        # score_job reports category-unconfirmed instead of a silent bonus.
        from network_jobs.prefs import score_job

        out = score_job({"title": "Data Wizard", "company": "Acme",
                         "location": "Remote"}, {"categories": ["product"]})
        self.assertIn("category-unconfirmed", out["matchReasons"])
        self.assertNotIn("category-affinity", out["matchReasons"])


class CrawlAndIntroTests(unittest.TestCase):
    def test_intro_role_filters(self):
        from network_jobs.intros import filter_jobs

        jobs = [
            {"title": "Senior Product Manager", "company": "Stripe",
             "url": "https://stripe.com/jobs/1"},
            {"title": "Staff PM, Growth", "company": "Figma",
             "url": "https://figma.com/jobs/2"},
        ]
        self.assertEqual(len(filter_jobs(jobs)), 2)
        got = filter_jobs(jobs, title="staff pm")
        self.assertEqual([j["company"] for j in got], ["Figma"])
        got = filter_jobs(jobs, company="stripe, inc.")
        self.assertEqual([j["title"] for j in got],
                         ["Senior Product Manager"])
        got = filter_jobs(jobs, url="figma.com/jobs/2")
        self.assertEqual(len(got), 1)
        self.assertEqual(filter_jobs(jobs, title="nope"), [])

    def test_prefer_pins_person_regardless_of_score(self):
        # #19: closeness comes from the user, not the scorer. A preferred
        # name sorts first even with a lower title-overlap score.
        from network_jobs.intros import rank_intros

        job = {"title": "Senior Product Manager", "company": "Stripe",
               "url": "https://stripe.com/j/1", "matchScore": 20,
               "status": "open"}
        people = [
            {"name": "Stranger Zee", "position": "Senior Product Manager",
             "company": "Stripe", "url": "", "email": "",
             "connectedOn": "2024-06-01"},
            {"name": "Good Friend", "position": "Janitor",
             "company": "Stripe", "url": "", "email": "",
             "connectedOn": "2016-03-01"},
        ]
        plain = rank_intros([job], people)
        self.assertEqual(plain["roles"][0]["forwarders"][0]["name"],
                         "Stranger Zee")
        pinned = rank_intros([job], people, prefer=["good friend"])
        self.assertEqual(pinned["roles"][0]["forwarders"][0]["name"],
                         "Good Friend")
        self.assertEqual(
            pinned["roles"][0]["forwarders"][0]["connectedOn"], "2016-03-01")

    def test_rank_intros_filters_and_empty_errors(self):
        import argparse
        import io
        from contextlib import redirect_stderr, redirect_stdout

        tmp = Path(tempfile.mkdtemp(prefix="nj-intro-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        search = tmp / "search"
        search.mkdir()
        (search / "ranked.json").write_text(json.dumps({
            "k": 2, "n": 2, "showing": "2 of 2",
            "jobs": [
                {"title": "Senior Product Manager", "company": "Stripe",
                 "url": "https://stripe.com/jobs/1", "matchScore": 21,
                 "status": "open"},
                {"title": "Designer", "company": "Figma",
                 "url": "https://figma.com/jobs/2", "matchScore": 5,
                 "status": "open"},
            ],
        }))
        conns = tmp / "connections"
        conns.mkdir()
        (conns / "connections.json").write_text(json.dumps([
            {"firstName": "Dan", "lastName": "Lee", "company": "Stripe",
             "position": "Senior PM", "url": "", "email": "",
             "connectedOn": "2024-01-01"},
        ]))
        args = argparse.Namespace(
            data=str(tmp), jobs="", connections="", out="",
            k_roles=2, k_forwarders=2,
            title="product manager", url="", company="",
            verbose=False,
        )
        from network_jobs import cli as cli_mod
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli_mod.cmd_rank_intros(args)
        self.assertEqual(rc, 0)
        intros = json.loads((search / "intros.json").read_text())
        self.assertEqual(len(intros["roles"]), 1)
        self.assertEqual(intros["roles"][0]["company"], "Stripe")
        args.title = "no such role"
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli_mod.cmd_rank_intros(args)
        self.assertEqual(rc, 1)
        self.assertIn("no ranked jobs match", err.getvalue())

    def test_search_verbose_prints_fit_brief(self):
        import io
        from contextlib import redirect_stdout

        from network_jobs import cli as cli_mod

        tmp = Path(tempfile.mkdtemp(prefix="nj-fit-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        jobs = [{
            "title": "Senior Product Manager", "company": "Stripe",
            "department": "Product", "location": "New York, NY",
            "url": "https://stripe.com/jobs/1", "matchScore": 21,
            "matchReasons": ["category", "seniority", "query"],
            "senioritySignals": [], "status": "open",
        }]
        (tmp / "connections").mkdir()
        (tmp / "connections" / "connections.json").write_text(json.dumps([
            {"firstName": "Dan", "lastName": "Lee", "company": "Stripe",
             "position": "Senior PM", "url": "", "email": "",
             "connectedOn": "2024-01-01"},
        ]))
        (tmp / "profile.json").write_text(json.dumps(
            {"name": "", "email": "", "title": "", "company": "", "url": ""}))
        (tmp / "preferences.json").write_text(json.dumps(
            {"mustHaves": ["platform"]})),
        out = io.StringIO()
        with redirect_stdout(out):
            cli_mod._print_fit_brief(tmp, jobs)
        text = out.getvalue()
        self.assertIn("Fit brief", text)
        self.assertIn("category", text)
        self.assertIn("warmth: 1 at Stripe", text)
        self.assertIn("Dan", text)
        # #19: the top pick is labeled as a title match, never a tie.
        self.assertIn("closest title match", text)
        self.assertNotIn("best:", text)

    def test_cli_stdout_has_no_accented_resume(self):
        # #18: user-facing output stays ASCII "resume". Narrow guard on the
        # reported class (é/è) — bullets and dashes elsewhere are intentional.
        import io
        from contextlib import redirect_stdout

        from network_jobs import cli as cli_mod

        out = io.StringIO()
        with redirect_stdout(out):
            for argv in (["--help"], ["search", "--help"],
                         ["intros", "--help"], ["report", "--help"],
                         ["review-matches", "--help"],
                         ["build-pack", "--help"], ["fetch-pack", "--help"]):
                try:
                    cli_mod.main(argv)
                except SystemExit:
                    pass
        text = out.getvalue()
        self.assertNotIn("é", text)
        self.assertNotIn("è", text)
        self.assertNotIn("résumé", text.lower())

    def test_verbose_flag_parses_after_subcommand(self):        # #17: helpers and bin append flags after the subcommand, where the
        # top-level -v never reaches. Every subparser must accept it.
        from network_jobs.cli import build_parser

        for argv in (["search", "-v", "-k", "3"],
                     ["match-prefs", "--triage-dir", "t", "-v"],
                     ["review-matches", "--triage-dir", "t", "--verbose"],
                     ["rank-intros", "--verbose"],
                     ["rebuild", "b.json", "-v"]):
            with self.subTest(argv=argv):
                self.assertTrue(build_parser().parse_args(argv).verbose)
        self.assertFalse(build_parser().parse_args(["search", "-k", "3"]).verbose)

    def test_listing_set_hash_skip_unchanged(self):
        from network_jobs.crawl import crawl_status, stamp_company_crawl

        tmp = Path(tempfile.mkdtemp(prefix="nj-crawl-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        companies = tmp / "companies.json"
        listings = [
            {"title": "Senior PM", "url": "https://x/1", "externalId": "1", "location": "Remote"},
            {"title": "Staff Engineer", "url": "https://x/2", "externalId": "2", "location": "NYC"},
        ]
        companies.write_text("[]\n")
        first = stamp_company_crawl(companies, "stripe", listings, pagination={"pages": 1, "complete": True, "truncated": False}, company_name="Stripe")
        self.assertFalse(first["unchanged"])
        graph = json.loads(companies.read_text())
        self.assertEqual(graph[0]["listingSetHash"], first["listingSetHash"])
        self.assertIn("lastCrawl", graph[0])
        second = crawl_status(graph, "stripe", listings, company_name="Stripe")
        self.assertTrue(second["unchanged"])
        listings2 = listings + [{"title": "New Role", "url": "https://x/3", "externalId": "3", "location": "Remote"}]
        third = crawl_status(graph, "stripe", listings2, company_name="Stripe")
        self.assertFalse(third["unchanged"])

    def test_incomplete_pagination_does_not_block_hash_but_skill_still_skips_expiry(self):
        from network_jobs.crawl import stamp_company_crawl

        tmp = Path(tempfile.mkdtemp(prefix="nj-crawl2-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        companies = tmp / "companies.json"
        companies.write_text("[]\n")
        listings = [{"title": "PM", "url": "https://x/1", "externalId": "1"}]
        status = stamp_company_crawl(
            companies, "acme", listings,
            pagination={"pages": 2, "complete": False, "truncated": True},
            company_name="Acme",
        )
        graph = json.loads(companies.read_text())
        self.assertEqual(graph[0]["lastPagination"]["complete"], False)
        self.assertTrue(status["listingSetHash"])

    def test_rank_intros_picks_two_roles_and_forwarders(self):
        from network_jobs.intros import rank_intros

        jobs = [
            {"title": "Senior Product Manager", "company": "Stripe", "department": "Product", "category": "product", "track": "ic", "url": "https://stripe.com/jobs/pm", "status": "open"},
            {"title": "Staff Engineer", "company": "Stripe", "department": "Infrastructure", "category": "engineering", "track": "ic", "url": "https://stripe.com/jobs/eng", "status": "open"},
            {"title": "Account Executive", "company": "Figma", "department": "Sales", "url": "https://figma.com/jobs/ae", "status": "open"},
        ]
        connections = [
            {"firstName": "Jane", "lastName": "Doe", "company": "Stripe", "position": "Product Manager", "url": "https://linkedin.com/in/jane"},
            {"firstName": "Eve", "lastName": "Kim", "company": "Stripe", "position": "Staff Engineer", "url": "https://linkedin.com/in/eve"},
            {"firstName": "John", "lastName": "Smith", "company": "Stripe", "position": "Engineer", "url": "https://linkedin.com/in/john"},
            {"firstName": "Alice", "lastName": "Chen", "company": "Figma", "position": "Designer", "url": "https://linkedin.com/in/alice"},
        ]
        result = rank_intros(jobs, connections, k_roles=2, k_forwarders=2)
        self.assertEqual(result["kRoles"], 2)
        self.assertEqual(len(result["fetchJdUrls"]), 2)
        self.assertTrue(result["useDepartmentWithoutJd"])
        stripe_pm = result["roles"][0]
        self.assertEqual(len(stripe_pm["forwarders"]), 2)
        self.assertEqual(stripe_pm["forwarders"][0]["name"], "Jane Doe")
        self.assertEqual(stripe_pm["department"], "Product")

    def test_rank_intros_sorts_by_match_score(self):
        from network_jobs.intros import rank_intros

        jobs = [
            {"title": "Low Score Role", "company": "Stripe", "matchScore": 1.0, "status": "open",
             "url": "https://stripe.com/jobs/low"},
            {"title": "Top Role", "company": "Stripe", "matchScore": 9.5, "status": "open",
             "url": "https://stripe.com/jobs/top"},
        ]
        connections = [
            {"firstName": "Jane", "lastName": "Doe", "company": "Stripe", "position": "PM"},
        ]
        result = rank_intros(jobs, connections, k_roles=1, k_forwarders=1)
        self.assertEqual(result["roles"][0]["title"], "Top Role")
        self.assertEqual(result["fetchJdUrls"], ["https://stripe.com/jobs/top"])

    def test_rank_intros_helper(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-intro-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        (tmp / "search").mkdir()
        (tmp / "connections").mkdir()
        ranked = {
            "jobs": [
                {"title": "Senior Product Manager", "company": "Stripe", "department": "Product", "url": "https://stripe.com/jobs/pm"},
                {"title": "Staff Engineer", "company": "Stripe", "department": "Engineering", "url": "https://stripe.com/jobs/eng"},
            ]
        }
        (tmp / "search" / "ranked.json").write_text(json.dumps(ranked) + "\n")
        (tmp / "connections" / "connections.json").write_text(json.dumps([
            {"firstName": "Jane", "lastName": "Doe", "company": "Stripe", "position": "PM", "url": "https://linkedin.com/in/jane"},
            {"firstName": "John", "lastName": "Smith", "company": "Stripe", "position": "Engineer", "url": "https://linkedin.com/in/john"},
        ]) + "\n")
        rc = helper_main(["rank-intros", "--data", str(tmp), "--k-roles", "2", "--k-forwarders", "2"])
        self.assertEqual(rc, 0)
        intros = json.loads((tmp / "search" / "intros.json").read_text())
        self.assertEqual(len(intros["fetchJdUrls"]), 2)
        self.assertLessEqual(len(intros["roles"]), 2)


class ManagerSeniorityTests(unittest.TestCase):
    def test_engineering_manager_is_senior(self):
        from network_jobs.classify import classify_job

        out = classify_job({"title": "Engineering Manager", "department": "Engineering"})
        self.assertEqual(out["track"], "manager")
        self.assertEqual(out["seniority"], "senior")
        self.assertIn("manager", out["senioritySignals"])

    def test_ic_manager_titles_stay_mid(self):
        from network_jobs.classify import classify_job

        # "Account Manager" is an IC role — must not be promoted to senior.
        out = classify_job({"title": "Account Manager", "department": "Sales"})
        self.assertEqual(out["track"], "ic")
        self.assertEqual(out["seniority"], "mid")

    def test_manager_intern_not_promoted(self):
        from network_jobs.classify import classify_job

        out = classify_job({"title": "Engineering Manager Intern", "department": "Engineering"})
        self.assertIn("intern", out["senioritySignals"])
        self.assertEqual(out["seniority"], "mid")


class SenioritySoftMatchTests(unittest.TestCase):
    def test_plain_pm_title_is_unmarked(self):
        from network_jobs.classify import classify_job

        out = classify_job({"title": "Product Manager, Claude Science", "department": "Product"})
        self.assertEqual(out["seniority"], "mid")
        self.assertIn("unmarked", out["senioritySignals"])

    def test_junior_signal_detected(self):
        from network_jobs.classify import classify_job

        out = classify_job({"title": "Associate Product Manager", "department": "Product"})
        self.assertIn("junior", out["senioritySignals"])
        self.assertEqual(out["seniority"], "mid")

    def test_generic_manager_not_promoted(self):
        from network_jobs.classify import classify_job

        # "Escalations Manager" is a low-confidence generic manager title —
        # must not be promoted to senior the way "Engineering Manager" is.
        out = classify_job({"title": "Executive Escalations Manager", "department": "Support"})
        self.assertEqual(out["seniority"], "mid")
        self.assertNotIn("manager", out["senioritySignals"])

    def test_unmarked_pm_matches_senior_prefs(self):
        prefs = {"seniority": ["senior"]}
        job = {"title": "Product Manager, Claude Science", "company": "Anthropic",
               "location": "New York, NY"}
        scored = score_job(job, prefs)
        self.assertTrue(scored["matched"])
        self.assertIn("seniority-ambiguous", scored["matchReasons"])

    def test_seniority_mismatch_is_soft_penalty(self):
        # Senior-titled role against mid prefs: still matches, just scores lower.
        prefs = {"seniority": ["mid"]}
        job = {"title": "Senior Product Manager", "company": "Anthropic",
               "location": "New York, NY"}
        scored = score_job(job, prefs)
        self.assertTrue(scored["matched"])
        self.assertIn("seniority-mismatch", scored["matchReasons"])

    def test_junior_signal_hard_fails_senior_prefs(self):
        prefs = {"seniority": ["senior"]}
        job = {"title": "Associate Product Manager", "company": "Anthropic",
               "location": "New York, NY"}
        scored = score_job(job, prefs)
        self.assertFalse(scored["matched"])
        self.assertIn("junior-mismatch", scored["matchReasons"])

    def test_staff_pref_mismatch_is_soft(self):
        prefs = {"seniority": ["staff+"]}
        job = {"title": "Senior Product Manager", "company": "Anthropic",
               "location": "New York, NY"}
        scored = score_job(job, prefs)
        self.assertTrue(scored["matched"])
        self.assertIn("staff-mismatch", scored["matchReasons"])

    def test_shards_load_both_seniorities(self):
        from network_jobs.rank import _iter_shards

        tmp = Path(tempfile.mkdtemp(prefix="nj-sen-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        corpus = tmp / "corpus"
        corpus.mkdir()
        manifest = {"categories": {"product": {"byLocation": {"nyc": {
            "senior": {"file": "product-nyc-senior.json"},
            "mid": {"file": "product-nyc-mid.json"},
        }}}}}
        (corpus / "manifest.json").write_text(json.dumps(manifest))
        (corpus / "product-nyc-senior.json").write_text(json.dumps(
            [{"title": "Senior PM", "company": "Acme", "fingerprint": "s1"}]))
        (corpus / "product-nyc-mid.json").write_text(json.dumps(
            [{"title": "Product Manager", "company": "Acme", "fingerprint": "m1"}]))
        prefs = {"seniority": ["senior"], "categories": ["product"],
                 "locationBuckets": ["nyc"]}
        jobs, _ = _iter_shards(corpus, prefs, None)
        titles = {j["title"] for j in jobs}
        self.assertIn("Senior PM", titles)
        self.assertIn("Product Manager", titles)



class RecencyStaleTests(unittest.TestCase):
    def _corpus(self, jobs):
        tmp = Path(tempfile.mkdtemp(prefix="nj-stale-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        corpus = tmp / "corpus"
        corpus.mkdir()
        (corpus / "jobs-all.json").write_text(json.dumps(jobs, indent=2) + "\n")
        (corpus / "manifest.json").write_text(json.dumps({
            "lastUpdated": "2026-09-20T00:00:00Z",
            "totalJobs": len(jobs),
            "categories": {},
        }) + "\n")
        (tmp / "preferences.json").write_text(json.dumps({}) + "\n")
        return tmp

    def _job(self, jid, postedAt=None, lastSeen="2026-09-20"):
        return {
            "id": jid, "title": "Senior Backend Engineer", "company": "Acme",
            "department": "Engineering", "category": "engineering",
            "location": "Remote", "locationBucket": "remote", "seniority": "senior",
            "url": f"https://example.com/{jid}", "postedAt": postedAt,
            "lastSeen": lastSeen,
        }

    def test_recent_posting_gets_boost(self):
        tmp = self._corpus([
            self._job("fresh", postedAt="2026-09-18"),
            self._job("oldish", postedAt="2026-08-01"),
        ])
        res = rank_corpus(data_dir=tmp, k=2, today="2026-09-20")
        by_id = {j["id"]: j for j in res["jobs"]}
        self.assertIn("recent", by_id["fresh"]["matchReasons"])
        self.assertNotIn("recent", by_id["oldish"]["matchReasons"])
        self.assertGreater(by_id["fresh"]["matchScore"], by_id["oldish"]["matchScore"])

    def test_stale_posting_hidden_by_default(self):
        tmp = self._corpus([
            self._job("fresh", postedAt="2026-09-18"),
            self._job("ancient", postedAt="2026-05-01"),
        ])
        res = rank_corpus(data_dir=tmp, k=10, today="2026-09-20")
        self.assertEqual(res["staleHidden"], 1)
        self.assertEqual([j["id"] for j in res["jobs"]], ["fresh"])

    def test_include_stale_sorts_last(self):
        tmp = self._corpus([
            self._job("fresh", postedAt="2026-09-18"),
            self._job("ancient", postedAt="2026-05-01"),
        ])
        res = rank_corpus(data_dir=tmp, k=10, today="2026-09-20", include_stale=True)
        self.assertEqual(res["staleHidden"], 0)
        self.assertEqual(res["jobs"][-1]["id"], "ancient")
        self.assertTrue(res["jobs"][-1]["stale"])

    def test_old_unseen_job_without_postedAt_is_stale(self):
        tmp = self._corpus([self._job("ghost", postedAt=None, lastSeen="2026-01-01")])
        res = rank_corpus(data_dir=tmp, k=10, today="2026-09-20")
        self.assertEqual(res["n"], 0)
        self.assertEqual(res["staleHidden"], 1)

    def test_postedAt_formats_parsed(self):
        from datetime import date

        from network_jobs.rank import posting_age_days

        today = date(2026, 9, 20)
        self.assertEqual(posting_age_days("2026-09-18T10:00:00Z", today), 2)
        self.assertEqual(posting_age_days("09/18/2026", today), 2)
        self.assertIsNone(posting_age_days("sometime last week", today))
        self.assertIsNone(posting_age_days(None, today))


class ResumeKeywordTests(unittest.TestCase):
    def test_noise_and_name_tokens_dropped(self):
        from network_jobs.prefs import load_resume_keywords, name_tokens

        text = (
            "Ada Lovelace led a team building new distributed systems. "
            "Work included Python, Kubernetes, and 0-to-1 product launches. "
            "Ada Lovelace has a strong track record of shipping."
        )
        kws = load_resume_keywords(text, exclude=name_tokens({"name": "Ada Lovelace"}))
        for noisy in ("ada", "lovelace", "new", "work", "led", "strong", "track", "record"):
            self.assertNotIn(noisy, kws)
        self.assertIn("python", kws)
        self.assertIn("kubernetes", kws)
        self.assertIn("distributed", kws)


class CompaniesRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nj-comp-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        (self.tmp / "companies").mkdir()
        (self.tmp / "companies" / "companies.json").write_text(json.dumps([
            {
                "name": "Acme", "normalized": "acme", "slug": "acme",
                "domain": "acme.com", "connectionCount": 5, "people": [],
                "lastCrawl": "2026-09-01T00:00:00Z",
                "listingSetHash": "stale-hash",
                "jobsUrl": "https://jobs.example.com/acme.json",
            },
            {
                "name": "Beta", "normalized": "beta", "slug": "beta",
                "domain": "beta.com", "connectionCount": 2, "people": [],
            },
        ]) + "\n")
        (self.tmp / "preferences.json").write_text(json.dumps({}) + "\n")

    def _listing_payload(self, title="Senior Backend Engineer"):
        return {
            "jobs": [
                {
                    "title": title, "department": "Engineering",
                    "location": "Remote", "url": "https://jobs.example.com/acme/1",
                    "postedAt": "2026-09-19",
                }
            ]
        }

    def test_companies_json_flag(self):
        rc = helper_main(["companies", "--data", str(self.tmp), "--json"])
        self.assertEqual(rc, 0)

    def test_crawl_state_stamp_stores_jobs_url(self):
        listings_path = self.tmp / "listings.json"
        listings_path.write_text(json.dumps([{"title": "X", "company": "Acme"}]) + "\n")
        rc = helper_main([
            "crawl-state", "--data", str(self.tmp), "--company", "Beta",
            "--stamp", "--source-url", "https://jobs.example.com/beta.json",
            "--listings", str(listings_path),
        ])
        self.assertEqual(rc, 0)
        companies = json.loads((self.tmp / "companies" / "companies.json").read_text())
        beta = next(c for c in companies if c["slug"] == "beta")
        self.assertEqual(beta["jobsUrl"], "https://jobs.example.com/beta.json")
        self.assertTrue(beta["lastCrawl"])

    def test_refresh_stages_changed_board(self):
        from network_jobs.cli import _do_refresh

        payload = self._listing_payload()
        body = json.dumps(payload).encode()
        results = _do_refresh(
            self.tmp, company="acme", limit=5,
            fetch=lambda url: (payload, 200, body),
            today="2026-09-20",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "changed")
        self.assertEqual(results[0]["matches"], 1)
        triage = Path(results[0]["triage"])
        self.assertTrue((triage / "index" / "matches.json").is_file())
        companies = json.loads((self.tmp / "companies" / "companies.json").read_text())
        acme = next(c for c in companies if c["slug"] == "acme")
        self.assertNotEqual(acme["listingSetHash"], "stale-hash")
        self.assertEqual(acme["jobsUrl"], "https://jobs.example.com/acme.json")

    def test_refresh_unchanged_board_only_stamps(self):
        from network_jobs.cli import _do_refresh
        from network_jobs.fingerprint import listing_set_hash

        payload = self._listing_payload()
        body = json.dumps(payload).encode()
        first = _do_refresh(
            self.tmp, company="acme", limit=5,
            fetch=lambda url: (payload, 200, body),
            today="2026-09-20",
        )
        self.assertEqual(first[0]["status"], "changed")
        second = _do_refresh(
            self.tmp, company="acme", limit=5,
            fetch=lambda url: (payload, 200, body),
            today="2026-09-20",
        )
        self.assertEqual(second[0]["status"], "unchanged")
        self.assertNotIn("triage", second[0])

    def test_refresh_error_does_not_kill_run(self):
        from network_jobs.cli import _do_refresh

        def boom(url):
            raise ConnectionError("dns blew up")

        results = _do_refresh(
            self.tmp, company="acme", limit=5, fetch=boom, today="2026-09-20",
        )
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("ConnectionError", results[0]["error"])

    def test_refresh_skips_companies_without_jobs_url(self):
        from network_jobs.cli import _do_refresh

        results = _do_refresh(self.tmp, limit=5, fetch=lambda url: ({}, 200, b"{}"))
        # Only Acme has a jobsUrl; Beta is skipped.
        self.assertEqual([r["slug"] for r in results], ["acme"])


class FetchDescriptionsTests(unittest.TestCase):
    HTML = ("<html><head><title>Jobs</title><style>x{}</style></head><body>"
            "<h1>Senior Product Manager, Payments</h1>"
            "<p>Own payments infrastructure end to end. Partner deeply with "
            "engineering on platform reliability and payments compliance across "
            "our global money movement network serving millions of users.</p>"
            "<script>track()</script></body></html>")

    def _fetch(self, mapping):
        def fetch(url):
            if url not in mapping:
                raise ConnectionError("unreachable")
            status, ctype, body = mapping[url]
            return status, ctype, body.encode()
        return fetch

    def test_strip_html_keeps_prose_drops_chrome(self):
        from network_jobs.descriptions import strip_html

        text = strip_html(self.HTML)
        self.assertIn("Own payments infrastructure", text)
        self.assertNotIn("track()", text)
        self.assertNotIn("x{}", text)

    def test_fetch_html_and_json_pages(self):
        from network_jobs.descriptions import fetch_job_description

        fetch = self._fetch({
            "https://x/jobs/1": (200, "text/html", self.HTML),
            "https://x/api/2": (200, "application/json", json.dumps({
                "title": "PM",
                "content": "Lead our " + "payments platform. " * 30,
                "departments": [{"name": "Product"}],
            })),
            "https://x/gone": (404, "text/html", "nope"),
        })
        html = fetch_job_description("https://x/jobs/1", fetch=fetch)
        self.assertIn("payments infrastructure", html["description"])
        self.assertTrue(html["descriptionFetchedAt"])
        js = fetch_job_description("https://x/api/2", fetch=fetch)
        self.assertIn("payments platform", js["description"])
        self.assertEqual(js["department"], "Product")
        self.assertIn("error", fetch_job_description("https://x/gone", fetch=fetch))
        self.assertIn("error", fetch_job_description("not a url", fetch=fetch))

    def test_fetch_descriptions_writes_back_matches(self):
        from network_jobs.descriptions import fetch_descriptions

        tmp = Path(tempfile.mkdtemp(prefix="nj-desc-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        index = tmp / "triage" / "t" / "index"
        index.mkdir(parents=True)
        (index / "matches.json").write_text(json.dumps([
            {"title": "Senior PM", "url": "https://x/jobs/1"},
            {"title": "No URL Job"},
        ]))
        result = fetch_descriptions(
            tmp / "triage" / "t",
            fetch=self._fetch({"https://x/jobs/1": (200, "text/html", self.HTML)}))
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["failed"], 1)
        rows = json.loads((index / "matches.json").read_text())
        self.assertIn("payments infrastructure", rows[0]["description"])
        self.assertNotIn("description", rows[1])

    def test_must_haves_fire_on_description_not_title(self):
        # #21: aspirational phrases are dead against title-only blobs;
        # AND-set matching over requirements text makes them discriminate.
        from network_jobs.prefs import must_have_hits

        title_only = {"title": "Senior Product Manager", "department": "",
                      "company": "Stripe"}
        self.assertEqual(
            must_have_hits(title_only, ["strong engineering partnership"]), [])
        with_desc = dict(
            title_only,
            description="You will build a strong engineering partnership "
                        "with platform teams.")
        self.assertEqual(
            must_have_hits(with_desc, ["strong engineering partnership"]),
            ["strong engineering partnership"])
        self.assertEqual(must_have_hits(title_only, ["platform"]), [])

    def test_must_haves_require_token_locality(self):
        # #24: a whole-document AND-set is too loose once real JD text is
        # in scope. Anthropic's legal boilerplate ("Not all strong
        # candidates will meet every single qualification") plus scattered
        # "engineering"/"partnership" satisfied the phrase and promoted two
        # roles on nothing. Tokens must co-occur locally.
        from network_jobs.prefs import must_have_hits

        phrase = ["strong engineering partnership"]
        base = {"title": "Product Manager", "department": "", "company": "Anthropic"}

        filler = ("Responsibilities include partnering with teams, "
                  "engineering standards, and strong ownership. ") * 12
        scattered = dict(base, description=(
            "Have a strong grasp of AI. " + filler
            + " Apply if you do not believe you meet every single "
              "qualification."))
        self.assertEqual(must_have_hits(scattered, phrase), [])

        # All three tokens present, but nowhere near each other — the shape
        # that promoted two Anthropic roles on legal boilerplate alone.
        far_apart = dict(base, description=(
            "Have a strong grasp of AI. "
            + "Our teams ship quickly and iterate. " * 12
            + "Engineering owns the platform. "
            + "We measure outcomes each quarter. " * 12
            + "A partnership with customers matters here."))
        self.assertEqual(must_have_hits(far_apart, phrase), [])

        genuine = dict(base, description=(
            "You will build a strong engineering partnership with "
            "platform teams."))
        self.assertEqual(
            must_have_hits(genuine, phrase), ["strong engineering partnership"])

    def test_match_prefs_rehydrates_descriptions_from_prior_matches(self):
        # #24: fetch-descriptions writes bodies into matches.json, but
        # match-prefs scores listings.json, so a re-score destroyed every
        # body and mustHaves could never fire in any step order.
        import json as _json
        import tempfile
        from network_jobs.cli import _rehydrate_descriptions
        from network_jobs.fingerprint import fingerprint

        listing = {"title": "Product Manager, Payments", "location": "NYC",
                   "url": "https://example.invalid/j/1", "externalId": "1"}
        fp = fingerprint(listing, company="Stripe")
        prior = {"jobs": [{
            "fingerprint": fp, "title": listing["title"], "company": "Stripe",
            "description": "Own payments intelligence tooling.",
            "descriptionFetchedAt": "2026-09-29T00:00:00Z",
            "descriptionSource": "ats-json",
        }]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "matches.json"
            path.write_text(_json.dumps(prior))
            listings = [dict(listing)]
            restored = _rehydrate_descriptions(listings, path, "Stripe")

        self.assertEqual(restored, 1)
        self.assertEqual(listings[0]["description"],
                         "Own payments intelligence tooling.")
        self.assertEqual(listings[0]["descriptionSource"], "ats-json")

    def test_rehydrate_does_not_clobber_or_invent_bodies(self):
        # A row that already has a body keeps it, and a row with no prior
        # body stays honestly undescribed rather than borrowing one.
        import json as _json
        import tempfile
        from network_jobs.cli import _rehydrate_descriptions
        from network_jobs.fingerprint import fingerprint

        have = {"title": "A", "location": "NYC", "externalId": "1"}
        fresh = {"title": "B", "location": "NYC", "externalId": "2"}
        prior = {"jobs": [{
            "fingerprint": fingerprint(have, company="Stripe"),
            "description": "prior body",
        }]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "matches.json"
            path.write_text(_json.dumps(prior))
            already = dict(have, description="current body")
            rows = [already, dict(fresh)]
            restored = _rehydrate_descriptions(rows, path, "Stripe")

        self.assertEqual(restored, 0)
        self.assertEqual(rows[0]["description"], "current body")
        self.assertNotIn("description", rows[1])

    def test_rehydrate_survives_fingerprint_spelling_drift(self):
        # #24 follow-up: `fingerprint()` returns an existing field verbatim,
        # so the stored key depends on whether the writing run passed
        # `--company`. Keying the join on one spelling silently restored 0
        # — and re-ingesting then duplicated every corpus job. All four
        # combinations must join.
        import json as _json
        import tempfile
        from network_jobs.cli import _rehydrate_descriptions

        listing = {"title": "Product Manager, Payments", "location": "NYC",
                   "externalId": "7812856", "url": "https://example.invalid/j/1"}
        for stored in ("id:stripe:7812856", "id::7812856"):
            prior = {"jobs": [dict(listing, fingerprint=stored,
                                   company="Stripe", description="BODY")]}
            for company in ("Stripe", ""):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "matches.json"
                    path.write_text(_json.dumps(prior))
                    rows = [dict(listing)]
                    restored = _rehydrate_descriptions(rows, path, company)
                self.assertEqual(restored, 1, f"{stored} / {company!r}")
                self.assertEqual(rows[0]["description"], "BODY")

    def test_description_survives_classify_and_merge(self):
        from network_jobs.classify import classify_job
        from network_jobs.corpus import merge_jobs

        job = {"title": "Senior PM", "company": "Acme", "location": "Remote",
               "description": "Own payments infrastructure."}
        classified = classify_job(job, company="Acme")
        self.assertEqual(classified["description"], "Own payments infrastructure.")
        merged, _ = merge_jobs([], [classified])
        self.assertEqual(merged[0]["description"], "Own payments infrastructure.")

    def test_fetched_department_upgrades_category(self):
        # An explicit ATS department beats weak title-token guessing
        # (classify already prefers it; the fetch step fills it in).
        from network_jobs.classify import classify_job

        bare = classify_job({"title": "Growth Catalyst",
                             "department": "", "location": "NYC"})
        self.assertEqual(bare["category"], "other")
        filled = classify_job({"title": "Growth Catalyst",
                               "department": "Product", "location": "NYC"})
        self.assertEqual((filled["category"], filled["categoryConfidence"]),
                         ("product", "high"))


class ParseLocationsIdempotencyTests(unittest.TestCase):
    def test_ambiguous_level_penalized_not_neutral(self):
        # #23: unknown level must not score like a confirmed match.
        from network_jobs.prefs import score_job

        prefs = {"seniority": ["senior"]}
        senior = score_job(
            {"title": "Senior Engineer", "company": "Acme", "location": "Remote"},
            prefs)
        plain = score_job(
            {"title": "Engineer", "company": "Acme", "location": "Remote"},
            prefs)
        self.assertTrue(senior["matched"] and plain["matched"])
        self.assertIn("seniority-ambiguous", plain["matchReasons"])
        self.assertEqual(
            senior["matchScore"] - plain["matchScore"], 4)

    def test_confirmed_outranks_ambiguous_at_equal_scores(self):
        from network_jobs.prefs import match_sort_key, seniority_unconfirmed

        confirmed = {"title": "B Role", "matchScore": 10,
                     "matchReasons": ["seniority"]}
        ambiguous = {"title": "A Role", "matchScore": 10,
                     "matchReasons": ["seniority-ambiguous"]}
        self.assertFalse(seniority_unconfirmed(confirmed))
        self.assertTrue(seniority_unconfirmed(ambiguous))
        self.assertEqual(
            sorted([ambiguous, confirmed], key=match_sort_key),
            [confirmed, ambiguous])
        # No markers at all (no seniority prefs) counts as confirmed.
        self.assertFalse(seniority_unconfirmed({"matchReasons": ["category"]}))

    def test_musthave_unmet_recorded_without_penalty(self):
        from network_jobs.prefs import score_job

        prefs = {"mustHaves": ["platform reliability"]}
        described = {"title": "Senior PM", "company": "Acme",
                     "location": "Remote",
                     "description": "Own payments infrastructure."}
        out = score_job(described, prefs)
        self.assertIn("mustHave-unmet", out["matchReasons"])
        bare = {"title": "Senior PM", "company": "Acme", "location": "Remote"}
        out_bare = score_job(bare, prefs)
        self.assertNotIn("mustHave-unmet", out_bare["matchReasons"])
        self.assertNotIn("mustHave", out_bare["matchReasons"])

    def test_target_roles_bonus(self):
        from network_jobs.prefs import score_job

        prefs = {"targetRoles": ["vp-product", "head-of-product"]}
        vp = score_job({"title": "VP Product", "company": "Acme",
                        "location": "Remote"}, prefs)
        self.assertIn("targetRole", vp["matchReasons"])
        pm = score_job({"title": "Staff Engineer", "company": "Acme",
                        "location": "Remote"}, prefs)
        self.assertNotIn("targetRole", pm["matchReasons"])
        self.assertGreater(
            vp["matchScore"] - pm["matchScore"], 0)

    def test_low_confidence_affinity_gets_no_bonus(self):
        from network_jobs.prefs import score_job

        prefs = {"categories": ["product"]}
        out = score_job({"title": "Data Wizard", "company": "Acme",
                         "location": "Remote"}, prefs)
        self.assertIn("category-unconfirmed", out["matchReasons"])
        self.assertNotIn("category-affinity", out["matchReasons"])

    def test_ambiguous_count_reported(self):
        from network_jobs.prefs import match_listings

        result = match_listings(
            [{"title": "Engineer", "company": "Acme", "location": "Remote"},
             {"title": "Senior Engineer", "company": "Acme", "location": "Remote"}],
            {"seniority": ["senior"]}, company="Acme")
        self.assertEqual(result["ambiguousSeniority"], 1)
    def test_round_trip_does_not_duplicate(self):
        # #15: persisted locations[] re-parsed alongside location must not
        # double — dict and string branches share one dedup namespace now.
        from network_jobs.locations import parse_locations

        raw = "San Francisco, Seattle, New York, Chicago, Atlanta, Remote"
        once = parse_locations({"location": raw})
        twice = parse_locations({"location": raw, "locations": once})
        self.assertEqual(len(twice), len(once))
        self.assertEqual(twice, once)

    def test_stripe_comma_list_yields_one_entry_per_city(self):
        from network_jobs.locations import parse_locations

        entries = parse_locations(
            {"location": "San Francisco, Seattle, New York, Chicago, Atlanta, Remote"})
        cities = [e.get("city") for e in entries]
        self.assertEqual(
            cities,
            ["San Francisco", "Seattle", "New York", "Chicago", "Atlanta", None])
        buckets = {e.get("city"): e.get("bucket") for e in entries if e.get("city")}
        self.assertEqual(buckets["San Francisco"], "sf")
        self.assertEqual(buckets["Seattle"], "seattle")
        self.assertEqual(buckets["New York"], "nyc")
        self.assertTrue(entries[-1]["remote"])

    def test_city_state_pairs_stay_whole(self):
        # Two comma tokens = one place (city + state/country), never split.
        from network_jobs.locations import parse_locations

        sf = parse_locations({"location": "San Francisco, CA"})
        self.assertEqual(len(sf), 1)
        self.assertEqual(sf[0]["city"], "San Francisco")
        self.assertEqual(sf[0]["region"], "CA")
        dublin = parse_locations({"location": "Dublin, Ireland"})
        self.assertEqual(len(dublin), 1)
        self.assertEqual(dublin[0]["city"], "Dublin")

    def test_state_reattaches_in_long_lists(self):
        from network_jobs.locations import parse_locations

        entries = parse_locations({"location": "Portland, Oregon, Remote"})
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["city"], "Portland")
        self.assertEqual(entries[0]["region"], "Oregon")
        self.assertTrue(entries[1]["remote"])

    def test_anywhere_parses_as_remote(self):
        from network_jobs.locations import parse_locations

        entries = parse_locations({"location": "anywhere"})
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["remote"])
        self.assertEqual(entries[0]["bucket"], "remote")


class CityMatchFloorTests(unittest.TestCase):
    def test_sentinel_location_matches_nothing(self):
        # #11: Greenhouse "unknown location" sentinel must not pass any
        # onsite preference.
        from network_jobs.locations import city_matches

        for place in ("New York", "Austin", "Paris", "San Francisco"):
            with self.subTest(place=place):
                self.assertFalse(city_matches({"location": "N/A"}, [place]))

    def test_single_letter_token_matches_nothing(self):
        # #11 follow-up: short tokens need word boundaries — 'n' is a
        # substring of 'new york' but not a word in it.
        from network_jobs.locations import city_matches

        self.assertFalse(city_matches({"location": "N"}, ["Boston"]))
        self.assertFalse(city_matches({"location": "X"}, ["Austin"]))

    def test_short_real_tokens_still_match_on_word_boundaries(self):
        from network_jobs.locations import city_matches

        self.assertTrue(city_matches({"location": "Washington, DC"}, ["DC"]))
        self.assertTrue(city_matches({"location": "New York, NY"}, ["New York"]))


class LocationBucketRound3Tests(unittest.TestCase):
    def test_new_metro_buckets(self):
        from network_jobs.locations import location_buckets_for

        cases = {
            "Los Angeles, CA": "la",
            "Santa Monica, CA": "la",
            "Seattle, WA": "seattle",
            "Bellevue, WA": "seattle",
            "Austin, TX": "austin",
            "Boston, MA": "boston",
            "Cambridge, MA": "boston",
            "Chicago, IL": "chicago",
            "Denver, CO": "denver",
            "Boulder, CO": "denver",
            "Washington, DC": "dc",
            "Arlington, VA": "dc",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(location_buckets_for({"location": raw})[0], want)

    def test_atlanta_is_not_la(self):
        from network_jobs.locations import location_buckets_for

        # Short aliases like "la" must not substring-match "Atlanta".
        self.assertEqual(location_buckets_for({"location": "Atlanta, GA"})[0], "other")
        self.assertEqual(location_buckets_for({"location": "Philadelphia, PA"})[0], "other")

    def test_primary_bucket_order(self):
        from network_jobs.locations import primary_location_bucket

        self.assertEqual(
            primary_location_bucket({"location": "Austin, TX or Remote"}), "austin"
        )
        self.assertEqual(primary_location_bucket({"location": "Remote"}), "remote")


class CategoryAffinityTests(unittest.TestCase):
    def test_affinity_soft_match_no_hard_fail(self):
        prefs = {"categories": ["engineering"]}
        job = {"title": "Machine Learning Engineer", "company": "Initech",
               "location": "Remote"}
        scored = score_job(job, prefs)
        self.assertTrue(scored["matched"])
        self.assertIn("category-affinity", scored["matchReasons"])
        self.assertNotIn("category", scored["matchReasons"])

    def test_exact_category_still_wins(self):
        prefs = {"categories": ["engineering"]}
        job = {"title": "Senior Backend Engineer", "company": "Acme",
               "location": "Remote"}
        scored = score_job(job, prefs)
        self.assertIn("category", scored["matchReasons"])
        self.assertGreaterEqual(scored["matchScore"], 5)

    def test_unrelated_category_still_hard_fails(self):
        prefs = {"categories": ["engineering"]}
        job = {"title": "Account Executive", "company": "Umbrella",
               "location": "Chicago, IL"}
        scored = score_job(job, prefs)
        self.assertFalse(scored["matched"])


class CompanyCapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nj-cap-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        corpus = self.tmp / "corpus"
        corpus.mkdir()
        jobs = [
            {
                "title": f"Backend Engineer {i}",
                "company": "Acme Corp",
                "department": "Engineering",
                "location": "Remote",
                "lastSeen": "2026-09-19",
                "url": f"https://example.com/jobs/{i}",
            }
            for i in range(5)
        ]
        (corpus / "jobs-all.json").write_text(json.dumps(jobs) + "\n")
        self.prefs = {"categories": ["engineering"]}

    def test_cap_limits_single_company(self):
        result = rank_corpus(data_dir=self.tmp, k=10, today="2026-09-20",
                             prefs=self.prefs, company_cap=3)
        self.assertEqual(len(result["jobs"]), 3)
        self.assertEqual(result["showing"], "3 of 5")

    def test_cap_zero_disables(self):
        result = rank_corpus(data_dir=self.tmp, k=10, today="2026-09-20",
                             prefs=self.prefs, company_cap=0)
        self.assertEqual(len(result["jobs"]), 5)


class SalaryParseTests(unittest.TestCase):
    def test_range(self):
        from network_jobs.salary import parse_salary

        self.assertEqual(parse_salary("$150k–$180k")["min"], 150000)
        self.assertEqual(parse_salary("$150k–$180k")["max"], 180000)

    def test_up_to(self):
        from network_jobs.salary import parse_salary

        parsed = parse_salary("up to $200k")
        self.assertEqual(parsed["max"], 200000)

    def test_hourly_flagged_not_annualized(self):
        from network_jobs.salary import parse_salary

        parsed = parse_salary("$75/hr")
        self.assertEqual(parsed["unit"], "hourly")
        self.assertEqual(parsed["min"], 75)

    def test_garbage_returns_none(self):
        from network_jobs.salary import parse_salary

        self.assertIsNone(parse_salary("competitive pay"))
        self.assertIsNone(parse_salary(None))

    def test_prefixed_dollar_currencies(self):
        from network_jobs.salary import parse_salary

        # "$" must not win over the longer "C$"/"A$" symbols.
        self.assertEqual(parse_salary("C$150k")["currency"], "CAD")
        self.assertEqual(parse_salary("A$200k")["currency"], "AUD")
        self.assertEqual(parse_salary("$150k")["currency"], "USD")

    def test_fmt_salary_marks_hourly(self):
        from network_jobs.cli import _fmt_salary

        self.assertEqual(_fmt_salary({"min": 75, "max": 75, "unit": "hourly"}), "$75/hr")
        self.assertEqual(_fmt_salary({"min": 150000, "max": 180000}), "$150k–$180k")


class PaginationNormalizeTests(unittest.TestCase):
    def test_greenhouse_object_location_normalized(self):
        # Greenhouse boards-api sends location as {"name": ...}. The old
        # truthiness-based staged fast path passed it through verbatim
        # (missing url/externalId, dict location). It must normalize.
        from network_jobs.pagination import normalize_listing

        raw = {"id": 8172487, "title": "Abuse Investigator",
               "location": {"name": "Dublin"},
               "absolute_url": "https://x/?gh_jid=8172487"}
        job = normalize_listing(raw, "https://src")
        assert job is not None
        self.assertEqual(job["url"], "https://x/?gh_jid=8172487")
        self.assertEqual(job["externalId"], "8172487")
        self.assertEqual(job["location"], "Dublin")
        self.assertEqual(job["locations"], ["Dublin"])

    def test_staged_string_listing_passes_through(self):
        from network_jobs.pagination import normalize_listing

        staged = {"title": "Eng", "url": "https://x/1", "location": "Remote"}
        job = normalize_listing(staged, "https://src")
        assert job is not None
        self.assertEqual(job["url"], "https://x/1")
        self.assertEqual(job["location"], "Remote")
        self.assertEqual(job["sourceUrl"], "https://src")

    def test_string_salary_parsed(self):
        from network_jobs.pagination import normalize_listing

        job = normalize_listing({"title": "Eng", "location": "Remote",
                                 "salary": "$150k–$180k"})
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["salary"]["min"], 150000)
        self.assertEqual(job["salary"]["max"], 180000)

    def test_hourly_salary_kept_hourly(self):
        from network_jobs.pagination import normalize_listing

        job = normalize_listing({"title": "Eng", "location": "Remote",
                                 "salary": "$90 per hour"})
        assert job is not None
        self.assertEqual(job["salary"]["unit"], "hourly")

    def test_posted_at_iso_when_parseable(self):
        from network_jobs.pagination import normalize_listing

        job = normalize_listing({"title": "Eng", "location": "Remote",
                                 "postedAt": "09/18/2026"})
        assert job is not None
        self.assertEqual(job["postedAt"], "2026-09-18")

    def test_posted_at_raw_when_unparseable(self):
        from network_jobs.pagination import normalize_listing

        job = normalize_listing({"title": "Eng", "location": "Remote",
                                 "postedAt": "sometime last week"})
        assert job is not None
        self.assertEqual(job["postedAt"], "sometime last week")

    def test_input_url_conflict_warns(self):
        import argparse
        import io
        from contextlib import redirect_stderr

        import network_jobs.cli as cli_mod

        tmp = Path(tempfile.mkdtemp(prefix="nj-pag-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        infile = tmp / "in.json"
        infile.write_text(json.dumps([{"title": "Eng", "location": "Remote"}]))
        args = argparse.Namespace(url="http://example.com/jobs", input=str(infile),
                                  triage_dir=str(tmp), company="", max_pages=1,
                                  max_listings=10, verbose=False)
        calls = []
        real_paginate = cli_mod.paginate
        cli_mod.paginate = lambda *a, **k: calls.append((a, k)) or {
            "listings": [], "pagination": {}, "fetchLog": []}
        try:
            err = io.StringIO()
            with redirect_stderr(err):
                cli_mod.cmd_paginate(args)
            self.assertIn("--input", err.getvalue())
            self.assertIn("--url", err.getvalue())
        finally:
            cli_mod.paginate = real_paginate


class CorpusMergeTests(unittest.TestCase):
    def test_inherits_last_seen_when_incoming_lacks_it(self):
        from network_jobs.corpus import merge_jobs

        existing = [{"title": "Eng", "company": "Acme", "location": "Remote",
                     "fingerprint": "fp:acme:abc123",
                     "firstSeen": "2026-09-01", "lastSeen": "2026-09-10",
                     "postedAt": "2026-08-28"}]
        incoming = [{"title": "Eng", "company": "Acme", "location": "Remote",
                     "fingerprint": "fp:acme:abc123"}]
        merged, _ = merge_jobs(existing, incoming)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["lastSeen"], "2026-09-10")
        self.assertEqual(merged[0]["postedAt"], "2026-08-28")
        self.assertEqual(merged[0]["firstSeen"], "2026-09-01")


class FingerprintPlaceholderTests(unittest.TestCase):
    def test_placeholder_ats_ids_ignored(self):
        from network_jobs.fingerprint import ats_id, fingerprint

        for placeholder in ("null", "N/A", "undefined", "TBD", "-", "unknown"):
            with self.subTest(placeholder=placeholder):
                self.assertIsNone(ats_id({"atsId": placeholder, "title": "x"}))

    def test_distinct_jobs_do_not_collide_on_placeholder(self):
        from network_jobs.fingerprint import fingerprint

        a = {"title": "Backend Engineer", "company": "Acme", "location": "NYC",
             "atsId": "null"}
        b = {"title": "Frontend Engineer", "company": "Acme", "location": "NYC",
             "atsId": "null"}
        self.assertNotEqual(fingerprint(a), fingerprint(b))
        self.assertTrue(fingerprint(a).startswith("fp:"))

    def test_prose_placeholder_ids_ignored(self):
        # Boards that render a label where an id belongs (Stripe leaves
        # requisition_id as "See Opening ID" on every posting). These must
        # not become a shared fingerprint that collapses the board.
        from network_jobs.fingerprint import ats_id, fingerprint

        for placeholder in ("See Opening ID", "see job id", "View Posting",
                            "apply now", "Job Details"):
            with self.subTest(placeholder=placeholder):
                self.assertIsNone(ats_id({"requisition_id": placeholder,
                                          "title": "x"}))

    def test_shared_prose_placeholder_does_not_collapse_board(self):
        from network_jobs.fingerprint import fingerprint

        base = {"company": "Stripe", "location": "Dublin",
                "externalId": "See Opening ID"}
        fps = {
            fingerprint(dict(base, title=t))
            for t in ("Abuse Investigator", "Data Analyst", "Support Lead")
        }
        self.assertEqual(len(fps), 3)
        self.assertTrue(all(fp.startswith("fp:") for fp in fps))


class IntrosConstantTests(unittest.TestCase):
    def test_unknown_position_score_named(self):
        from network_jobs.intros import UNKNOWN_POSITION_SCORE, _score_forwarder

        self.assertEqual(UNKNOWN_POSITION_SCORE, 0.5)
        score = _score_forwarder({"title": "Eng"}, {"name": "Pat"})
        self.assertEqual(score, UNKNOWN_POSITION_SCORE)


class DatesModuleTests(unittest.TestCase):
    def test_parse_date_flexible(self):
        from datetime import date

        from network_jobs.dates import normalize_date_iso, parse_date_flexible

        self.assertEqual(parse_date_flexible("2026-09-18T10:00:00Z"), date(2026, 9, 18))
        self.assertEqual(parse_date_flexible("09/18/2026"), date(2026, 9, 18))
        self.assertIsNone(parse_date_flexible("sometime last week"))
        self.assertIsNone(parse_date_flexible(None))
        self.assertEqual(normalize_date_iso("Sep 18, 2026"), "2026-09-18")
        self.assertIsNone(normalize_date_iso("garbage"))


class DemoCommandTests(unittest.TestCase):
    def test_demo_runs_offline(self):
        import argparse
        import io
        from contextlib import redirect_stdout

        import network_jobs.cli as cli_mod

        args = argparse.Namespace(data="/tmp/nj-demo-nope", k=8, company_cap=3)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli_mod.cmd_demo(args)
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("demo:", text)
        self.assertIn("category-affinity", text)


class EmbeddingsTests(unittest.TestCase):
    def setUp(self):
        from network_jobs import embeddings as emb

        self.emb = emb
        self.tmp = Path(tempfile.mkdtemp(prefix="nj-emb-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._old_env = os.environ.get("NJ_EMBED_PROVIDER")
        os.environ["NJ_EMBED_PROVIDER"] = "none"
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old_env is None:
            os.environ.pop("NJ_EMBED_PROVIDER", None)
        else:
            os.environ["NJ_EMBED_PROVIDER"] = self._old_env

    class FakeProvider:
        name = "fake"
        model = "fake-model"
        dims = 3

        def __init__(self):
            self.calls: list[list[str]] = []

        def embed(self, texts):
            self.calls.append(list(texts))
            out = []
            for t in texts:
                tl = t.lower()
                if "product" in tl:
                    out.append([1.0, 0.0, 0.0])
                elif "engineer" in tl:
                    out.append([0.0, 1.0, 0.0])
                else:
                    out.append([0.0, 0.0, 1.0])
            return out

    def _job(self, title="Senior Product Manager", company="Acme", **kw):
        job = {"title": title, "company": company, "department": "Product",
               "location": "Remote"}
        job.update(kw)
        return job

    # --- math ---

    def test_cosine_known_values(self):
        self.assertAlmostEqual(self.emb.cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(self.emb.cosine([1, 0], [0, 1]), 0.0)
        self.assertAlmostEqual(self.emb.cosine([1, 1], [1, 1]), 1.0)
        self.assertEqual(self.emb.cosine([], [1]), 0.0)

    def test_semantic_bonus_mapping(self):
        b = self.emb.semantic_bonus
        self.assertEqual(b(0.55, 0.55, 4.0), 0.0)   # at floor -> nothing
        self.assertEqual(b(0.10, 0.55, 4.0), 0.0)   # below floor -> nothing
        self.assertEqual(b(1.0, 0.55, 4.0), 4.0)    # perfect -> cap
        self.assertAlmostEqual(b(0.775, 0.55, 4.0), 2.0)  # halfway -> half

    def test_embed_text_recipe(self):
        text = self.emb.embed_text_for(
            {"title": "PM", "department": "Product", "description": "Owns roadmap"})
        self.assertIn("PM", text)
        self.assertIn("Product", text)
        self.assertIn("Owns roadmap", text)
        # Missing description degrades gracefully.
        self.assertTrue(self.emb.embed_text_for({"title": "PM"}).strip())

    # --- provider detection never raises ---

    def test_get_provider_none_override(self):
        self.assertIsNone(self.emb.get_provider())

    # --- score_job wiring ---

    def test_semantic_replaces_keyword_bonus(self):
        job = self._job()
        kws = ["product"]  # would hit the keyword path
        base = score_job(job, {}, resume_keywords=kws)
        self.assertIn("resume", base["matchReasons"])
        sem = score_job(
            job, {}, resume_keywords=kws,
            resume_vector=[1.0, 0.0, 0.0], job_vector=[1.0, 0.0, 0.0],
        )
        self.assertIn("resume-semantic", sem["matchReasons"])
        self.assertNotIn("resume", sem["matchReasons"])  # replaced, not stacked
        self.assertGreater(sem["matchScore"], base["matchScore"] - 4)

    def test_semantic_floor_no_bonus_no_keyword_fallback(self):
        job = self._job()
        sem = score_job(
            job, {}, resume_keywords=["product"],
            resume_vector=[1.0, 0.0, 0.0], job_vector=[0.0, 1.0, 0.0],
        )
        self.assertNotIn("resume-semantic", sem["matchReasons"])
        self.assertNotIn("resume", sem["matchReasons"])

    def test_no_vectors_keyword_path_unchanged(self):
        job = self._job()
        res = score_job(job, {}, resume_keywords=["product"],
                        resume_vector=[1.0, 0.0, 0.0], job_vector=None)
        self.assertIn("resume", res["matchReasons"])

    def test_match_listings_threads_vectors(self):
        from network_jobs.fingerprint import fingerprint

        job = self._job()
        fp = fingerprint(job, company="Acme")
        result = match_listings(
            [job], {}, resume_keywords=["product"],
            resume_vector=[1.0, 0.0, 0.0],
            job_vectors={fp: [1.0, 0.0, 0.0]},
            company="Acme",
        )
        reasons = result["jobs"][0]["matchReasons"]
        self.assertIn("resume-semantic", reasons)
        self.assertNotIn("resume", reasons)

    # --- cache behavior ---

    def _corpus_jobs(self, n=2):
        from network_jobs.fingerprint import fingerprint

        jobs = []
        for i in range(n):
            job = self._job(title=f"Senior Product Manager {i}", company="Acme")
            job["fingerprint"] = fingerprint(job, company="Acme")
            jobs.append(job)
        return jobs

    def test_cache_incremental_embeds_only_new(self):
        corpus = self.tmp / "corpus"
        corpus.mkdir()
        provider = self.FakeProvider()
        jobs = self._corpus_jobs(2)
        first = self.emb.maybe_update_embeddings(corpus, jobs, provider=provider)
        self.assertTrue(first["updated"])
        self.assertEqual(first["embedded"], 2)
        self.assertEqual(len(provider.calls), 1)

        # Second run: nothing new -> no embed calls.
        provider.calls.clear()
        second = self.emb.maybe_update_embeddings(corpus, jobs, provider=provider)
        self.assertTrue(second["updated"])
        self.assertEqual(second["embedded"], 0)
        self.assertEqual(provider.calls, [])

        # One new job -> exactly one text embedded.
        jobs.append(self._corpus_jobs(3)[2])
        third = self.emb.maybe_update_embeddings(corpus, jobs, provider=provider)
        self.assertEqual(third["embedded"], 1)
        self.assertEqual(sum(len(c) for c in provider.calls), 1)

    def test_cache_prunes_removed_jobs(self):
        corpus = self.tmp / "corpus"
        corpus.mkdir()
        provider = self.FakeProvider()
        jobs = self._corpus_jobs(2)
        self.emb.maybe_update_embeddings(corpus, jobs, provider=provider)
        self.emb.maybe_update_embeddings(corpus, jobs[:1], provider=provider)
        cache = self.emb.load_embeddings_cache(corpus)
        self.assertEqual(len(cache["vectors"]), 1)

    def test_cache_model_change_full_refresh(self):
        corpus = self.tmp / "corpus"
        corpus.mkdir()
        provider = self.FakeProvider()
        jobs = self._corpus_jobs(2)
        self.emb.maybe_update_embeddings(corpus, jobs, provider=provider)
        provider.model = "fake-model-v2"
        provider.calls.clear()
        res = self.emb.maybe_update_embeddings(corpus, jobs, provider=provider)
        self.assertTrue(res["fullRefresh"])
        self.assertEqual(res["embedded"], 2)
        cache = self.emb.load_embeddings_cache(corpus)
        self.assertEqual(cache["model"], "fake-model-v2")

    def test_no_provider_leaves_no_cache(self):
        corpus = self.tmp / "corpus"
        corpus.mkdir()
        res = self.emb.maybe_update_embeddings(corpus, self._corpus_jobs(1))
        self.assertEqual(res, {"updated": False, "reason": "no-provider"})
        self.assertFalse((corpus / "embeddings.json").exists())

    # --- rank_corpus degradation ---

    def test_rank_degrades_cleanly_without_provider(self):
        from network_jobs.rank import _semantic_context

        corpus = self.tmp / "corpus"
        corpus.mkdir()
        (self.tmp / "preferences.json").write_text(json.dumps({}) + "\n")
        jobs = self._corpus_jobs(1)
        rv, qv, jv = _semantic_context(corpus, jobs, "résumé text", "product")
        self.assertIsNone(rv)
        self.assertIsNone(qv)
        self.assertEqual(jv, {})

        shard = self.tmp / "corpus" / "senior.json"
        shard.write_text(json.dumps(jobs) + "\n")
        result = rank_corpus(data_dir=self.tmp, k=10)
        for job in result["jobs"]:
            reasons = job.get("matchReasons") or []
            self.assertFalse([r for r in reasons if "semantic" in r])


class ConsumerCliTests(unittest.TestCase):
    """Regression tests for the installed-CLI surface (bash dispatch).

    Bug 1 (2026-09-28): `npx 'github:hirefrank/network-jobs#main' setup`
    failed because bin/network-jobs computed ROOT from BASH_SOURCE[0]
    without resolving the npm .bin symlink.
    Bug 2 (2026-09-28): `network-jobs embed-setup` was documented and
    implemented in the Python CLI but missing from the bash dispatch table.
    """

    def _run(self, argv, env=None):
        import subprocess

        e = dict(os.environ)
        e["NETWORK_JOBS_HOME"] = str(Path(tempfile.mkdtemp(prefix="nj-cli-")))
        if env:
            e.update(env)
        return subprocess.run(argv, capture_output=True, text=True, env=e,
                              timeout=60)

    def test_embed_setup_dispatched(self):
        proc = self._run([str(ROOT / "bin" / "network-jobs"),
                          "embed-setup", "--help"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Usage: network-jobs embed-setup", proc.stdout)

    def test_bin_resolves_npm_shim_symlink(self):
        # Simulate npm's layout: node_modules/.bin/network-jobs ->
        # ../pkg/bin/network-jobs, with pkg itself a symlink to the suite.
        tmp = Path(tempfile.mkdtemp(prefix="nj-shim-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        bin_dir = tmp / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (tmp / "node_modules" / "pkg").symlink_to(ROOT, target_is_directory=True)
        (bin_dir / "network-jobs").symlink_to("../pkg/bin/network-jobs")
        proc = self._run([str(bin_dir / "network-jobs"), "which"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"suite={tmp}/node_modules/pkg", proc.stdout)

    def test_import_is_quiet(self):
        # parse-linkedin.sh prints one summary line; detail goes to the log file.
        import subprocess

        tmp = Path(tempfile.mkdtemp(prefix="nj-import-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        helper = (ROOT / "skills" / "network-jobs-import" / "helpers"
                  / "parse-linkedin.sh")
        proc = subprocess.run(
            ["bash", str(helper), str(FIXTURES / "Connections.csv"),
             "--out", str(tmp)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [l for l in proc.stdout.strip().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, proc.stdout)
        self.assertTrue(lines[0].startswith("import: "), proc.stdout)
        self.assertEqual(len(list((tmp / "logs").glob("import-*.json"))), 1)

    def test_intros_dispatched(self):
        proc = self._run([str(ROOT / "bin" / "network-jobs"),
                          "intros", "--help"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Usage: network-jobs intros", proc.stdout)

    def test_intros_runs(self):
        import json
        import subprocess

        data = Path(tempfile.mkdtemp(prefix="nj-intros-"))
        self.addCleanup(lambda: shutil.rmtree(data, ignore_errors=True))
        (data / "search").mkdir()
        (data / "connections").mkdir()
        ranked = {"jobs": [
            {"title": "Senior Product Manager", "company": "Stripe",
             "url": "https://example.invalid/1"},
            {"title": "Staff Product Manager", "company": "Stripe",
             "url": "https://example.invalid/2"},
        ]}
        (data / "search" / "ranked.json").write_text(json.dumps(ranked))
        conns = [
            {"firstName": "Jane", "lastName": "Doe", "company": "Stripe"},
            {"firstName": "John", "lastName": "Smith", "company": "Stripe"},
        ]
        (data / "connections" / "connections.json").write_text(json.dumps(conns))
        env = {"NETWORK_JOBS_HOME": str(data)}
        proc = subprocess.run(
            [str(ROOT / "bin" / "network-jobs"), "intros"],
            capture_output=True, text=True, env={**os.environ, **env},
            timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads((data / "search" / "intros.json").read_text())
        self.assertEqual(out["kRoles"], 2)

    def test_doctor_reports_embeddings_unconfigured(self):
        # Bug (2026-09-28): the bash `doctor` never surfaced the Python
        # doctor's embedding provider + cache section.
        import subprocess

        data = Path(tempfile.mkdtemp(prefix="nj-doc-"))
        for p in ("connections", "companies", "config", "triage", "corpus",
                  "logs"):
            (data / p).mkdir()
        (data / "profile.json").write_text(
            json.dumps({"name": "Test User", "email": "t@example.com"}))
        env = dict(os.environ, NETWORK_JOBS_HOME=str(data),
                   NJ_EMBED_PROVIDER="none")
        proc = subprocess.run([str(ROOT / "bin" / "network-jobs"), "doctor"],
                              capture_output=True, text=True, env=env,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("embeddings: not configured (keyword scoring only)",
                      proc.stdout)
        self.assertIn("cache: none", proc.stdout)

    def test_embed_setup_warmup_failure_reports_reason(self):
        # Bug (2026-09-28): a detected-but-broken provider (e.g. model
        # download blocked) fell through to "fastembed is not installed",
        # sending the user to reinstall instead of reporting the cause.
        import argparse
        import contextlib
        import io

        from network_jobs import cli as py_cli
        from network_jobs import embeddings as emb_mod

        class BrokenProvider:
            name = "fastembed"
            model = "BAAI/bge-small-en-v1.5"
            dims = 384
            last_error = "InvalidURL: Invalid port: ':1]'"

            def embed(self, texts):
                return None

        real = emb_mod.get_provider
        emb_mod.get_provider = lambda: BrokenProvider()
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = py_cli.cmd_embed_setup(argparse.Namespace())
        finally:
            emb_mod.get_provider = real
        self.assertEqual(rc, 1)
        self.assertIn("warmup", err.getvalue())
        self.assertIn("InvalidURL", err.getvalue())
        self.assertNotIn("pip install", err.getvalue())


class BuildPackTests(unittest.TestCase):
    CSV = (
        "First Name,Last Name,Email Address,Company,Position,Connected On\n"
        'Ada,Lovelace,ada@example.com,Acme Inc,Senior Product Manager,01 Jan 2024\n'
        'Grace,Hopper,grace@example.com,Acme Corp,Product Manager,02 Feb 2024\n'
        'Alan,Turing,alan@example.com,Acme LLC,Engineering Manager,03 Mar 2024\n'
        'Katherine,Johnson,kj@example.com,Globex,CEO,04 Apr 2024\n'
        'Margaret,Hamilton,mh@example.com,Self-employed,Founder,05 May 2024\n'
        'Anita,Borg,ab@example.com,,Designer,06 Jun 2024\n'
    )

    def _csv_file(self, tmp):
        p = Path(tmp) / "Connections.csv"
        p.write_text(self.CSV, encoding="utf-8")
        return p

    def test_pack_has_no_personal_data(self):
        from network_jobs.pack import build_pack
        with tempfile.TemporaryDirectory() as tmp:
            pack = build_pack(self._csv_file(tmp), label="Test Owner",
                              generated_on="2026-09-28")
        dump = json.dumps(pack)
        for needle in ("Ada", "Lovelace", "Grace", "Hopper", "Alan", "Turing",
                       "Katherine", "Johnson", "Margaret", "Hamilton",
                       "example.com", "linkedin.com", "2024", "Test OwnerX"):
            self.assertNotIn(needle, dump)
        self.assertNotIn("@", dump)  # no emails or URLs survived
        # ...but the label itself is fine
        self.assertEqual(pack["label"], "Test Owner")

    def test_company_aggregation_and_normalization(self):
        from network_jobs.pack import build_pack
        with tempfile.TemporaryDirectory() as tmp:
            pack = build_pack(self._csv_file(tmp), generated_on="2026-09-28")
        by_slug = {c["slug"]: c for c in pack["companies"]}
        # Acme Inc + Acme Corp + Acme LLC normalize together
        self.assertEqual(by_slug["acme"]["connectionCount"], 3)
        self.assertEqual(by_slug["globex"]["connectionCount"], 1)
        # ignored + company-less rows never appear
        self.assertNotIn("self-employed", by_slug)
        self.assertEqual(pack["totalConnections"], 4)

    def test_title_threshold(self):
        from network_jobs.pack import build_pack
        with tempfile.TemporaryDirectory() as tmp:
            pack = build_pack(self._csv_file(tmp), generated_on="2026-09-28")
        by_slug = {c["slug"]: c for c in pack["companies"]}
        # 3 connections -> titles published
        self.assertIn("topTitles", by_slug["acme"])
        self.assertTrue(any("product manager" in t for t in by_slug["acme"]["topTitles"]))
        # 1 connection -> count only, no titles (lone CEO stays anonymous)
        self.assertNotIn("topTitles", by_slug["globex"])

    def test_zip_input_and_cli(self):
        import zipfile
        from network_jobs import pack as pack_mod
        with tempfile.TemporaryDirectory() as tmp:
            zp = Path(tmp) / "linkedin-export.zip"
            with zipfile.ZipFile(zp, "w") as zf:
                zf.writestr("Connections.csv", self.CSV)
            pack = pack_mod.build_pack(zp, label="Zip Owner", generated_on="2026-09-28")
            self.assertEqual(pack["totalConnections"], 4)
            out = Path(tmp) / "out.json"
            pack_mod.write_pack(pack, out)
            self.assertTrue(out.is_file())
            # CLI end to end
            rc = helper_main(["build-pack", str(zp), "--label", "Cli Owner",
                              "--out", str(Path(tmp) / "cli.json")])
            self.assertEqual(rc, 0)
            cli_pack = json.loads((Path(tmp) / "cli.json").read_text())
            self.assertEqual(cli_pack["label"], "Cli Owner")
            self.assertNotIn("@", json.dumps(cli_pack))

    def test_min_count(self):
        from network_jobs.pack import build_pack
        with tempfile.TemporaryDirectory() as tmp:
            pack = build_pack(self._csv_file(tmp), min_count=2,
                              generated_on="2026-09-28")
        slugs = {c["slug"] for c in pack["companies"]}
        self.assertIn("acme", slugs)
        self.assertNotIn("globex", slugs)

    def test_company_matching_person_name_excluded(self):
        from network_jobs.pack import build_pack
        csv_text = (
            "First Name,Last Name,Email Address,Company,Position,Connected On\n"
            'Jane,Doe,jane@example.com,Jane Doe,Founder,01 Jan 2024\n'
            'John,Smith,john@example.com,Initech,Engineer,02 Feb 2024\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "Connections.csv"
            p.write_text(csv_text, encoding="utf-8")
            pack = build_pack(p, generated_on="2026-09-28")
        dump = json.dumps(pack)
        self.assertNotIn("Jane Doe", dump)
        self.assertNotIn("jane@example.com", dump)
        slugs = {c["slug"] for c in pack["companies"]}
        self.assertIn("initech", slugs)
        self.assertNotIn("jane-doe", slugs)


class FetchPackTests(unittest.TestCase):
    PACK = {
        "pack": "network-jobs",
        "packVersion": 1,
        "label": "Test Owner",
        "generatedAt": "2026-09-28",
        "source": "LinkedIn connections export (company-aggregated; no personal data)",
        "totalConnections": 4,
        "companies": [
            {"name": "Acme", "normalized": "acme", "slug": "acme",
             "connectionCount": 3, "topTitles": ["product manager"]},
            {"name": "Globex", "normalized": "globex", "slug": "globex",
             "connectionCount": 1},
        ],
    }

    def _serve(self, tmp):
        """Serve tmp over HTTP on an ephemeral port; returns (server, url)."""
        import functools
        import http.server
        import threading
        handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                    directory=tmp)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def test_fetch_pack_downloads_validates_saves(self):
        from network_jobs import pack as pack_mod
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pack.json").write_text(json.dumps(self.PACK))
            server, url = self._serve(tmp)
            try:
                with tempfile.TemporaryDirectory() as data:
                    pack, dest = pack_mod.fetch_pack(f"{url}/pack.json",
                                                    data_dir=data)
                    self.assertEqual(pack["label"], "Test Owner")
                    self.assertEqual(dest, Path(data) / "packs" / "test-owner.json")
                    self.assertTrue(dest.is_file())
                    saved = json.loads(dest.read_text())
                    self.assertEqual(saved["totalConnections"], 4)
                    # re-fetch refreshes in place
                    pack2, dest2 = pack_mod.fetch_pack(f"{url}/pack.json",
                                                      data_dir=data)
                    self.assertEqual(dest2, dest)
            finally:
                server.shutdown()

    def test_fetch_pack_as_name_override(self):
        from network_jobs import pack as pack_mod
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pack.json").write_text(json.dumps(self.PACK))
            server, url = self._serve(tmp)
            try:
                with tempfile.TemporaryDirectory() as data:
                    _, dest = pack_mod.fetch_pack(f"{url}/pack.json",
                                                 data_dir=data, name="Frank")
                    self.assertEqual(dest.name, "frank.json")
            finally:
                server.shutdown()

    def test_fetch_pack_rejects_bad_shape(self):
        from network_jobs import pack as pack_mod
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "nope.json").write_text(json.dumps({"hello": "world"}))
            (Path(tmp) / "broken.json").write_text("not json at all {{{")
            server, url = self._serve(tmp)
            try:
                with tempfile.TemporaryDirectory() as data:
                    with self.assertRaises(ValueError):
                        pack_mod.fetch_pack(f"{url}/nope.json", data_dir=data)
                    with self.assertRaises(ValueError):
                        pack_mod.fetch_pack(f"{url}/broken.json", data_dir=data)
                    with self.assertRaises(ValueError):
                        pack_mod.fetch_pack(f"{url}/missing.json", data_dir=data)
            finally:
                server.shutdown()

    def test_fetch_pack_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pack.json").write_text(json.dumps(self.PACK))
            server, url = self._serve(tmp)
            try:
                with tempfile.TemporaryDirectory() as data:
                    rc = helper_main(["fetch-pack", f"{url}/pack.json",
                                      "--data", data])
                    self.assertEqual(rc, 0)
                    self.assertTrue((Path(data) / "packs" / "test-owner.json").is_file())
            finally:
                server.shutdown()


if __name__ == "__main__":
    unittest.main()