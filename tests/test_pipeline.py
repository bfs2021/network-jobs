#!/usr/bin/env python3
"""Tests for Network Jobs helpers (prefs shortlist, ranker, later layers)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from network_jobs.inventory import (  # noqa: E402
    MARKER_START,
    department_histogram_line,
    render_summary_block,
    upsert_inventory,
)
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

    def test_smaller_families_bucket_has_full_family_contract(self):
        # #30: the aggregated bucket used to omit label/suggestedVeto, so
        # cli.py crashed with KeyError and --json drifted in shape.
        from network_jobs.review import cluster_matches

        jobs = [
            {"title": "Senior Product Manager, Payments", "category": "product",
             "location": "NYC", "matchScore": 12},
            {"title": "Staff PM, Payments", "category": "product",
             "location": "Remote", "matchScore": 10},
            {"title": "Principal Product Manager, Payments", "category": "product",
             "location": "NYC", "matchScore": 8},
            # Four one-member families, all below min_cluster=2.
            {"title": "Data Center Architect", "category": "operations",
             "location": "Austin", "matchScore": 4},
            {"title": "Legal Counsel", "category": "legal",
             "location": "NYC", "matchScore": 3},
            {"title": "Technical Recruiter", "category": "people",
             "location": "Remote", "matchScore": 2},
            {"title": "Account Executive", "category": "sales",
             "location": "London", "matchScore": 1},
        ]
        fams = cluster_matches(jobs, min_cluster=2)
        bucket = next(f for f in fams if f["stem"] == "(smaller families)")
        self.assertEqual(bucket["label"], "(smaller families)")
        self.assertIsNone(bucket["suggestedVeto"])
        self.assertEqual(bucket["count"], 4)
        self.assertEqual(bucket["locations"], [])
        self.assertEqual(bucket["sampleTitles"], [])
        self.assertEqual(bucket["category"], "(mixed)")
        self.assertIn("medianScore", bucket)

    def test_every_emitted_family_has_the_same_keys(self):
        from network_jobs.review import cluster_matches

        expected = {"category", "stem", "label", "suggestedVeto", "count",
                    "medianScore", "locations", "sampleTitles"}
        jobs = [
            {"title": "Senior Product Manager, Payments", "category": "product",
             "location": "NYC", "matchScore": 12},
            {"title": "Staff PM, Payments", "category": "product",
             "location": "Remote", "matchScore": 10},
            {"title": "Data Center Architect", "category": "operations",
             "location": "Austin", "matchScore": 4},
            {"title": "Legal Counsel", "category": "legal",
             "location": "NYC", "matchScore": 3},
        ]
        for min_cluster in (1, 2):
            with self.subTest(min_cluster=min_cluster):
                fams = cluster_matches(jobs, min_cluster=min_cluster)
                self.assertTrue(fams)
                for fam in fams:
                    self.assertEqual(set(fam), expected)
                    self.assertIsInstance(fam["label"], str)

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

    def test_cli_prints_smaller_families_bucket_without_crashing(self):
        # #30 end to end: the fixture shortlist has two one-member families,
        # so default --min-cluster 2 emits only the aggregated bucket.
        import io
        from contextlib import redirect_stdout

        tmp = Path(tempfile.mkdtemp(prefix="nj-rev-bucket-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        triage = tmp / "triage" / "careers-example-2026-09-16"
        (triage / "index").mkdir(parents=True)
        shutil.copy(FIXTURES / "listings-prefs.json",
                    triage / "index" / "listings.json")
        data = tmp / "data"
        data.mkdir()
        shutil.copy(FIXTURES / "preferences.json", data / "preferences.json")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = helper_main([
                "review-matches",
                "--triage-dir", str(triage),
                "--data", str(data),
                "--company", "Example",
            ])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("(smaller families) — 2", text)
        self.assertNotIn("veto with:", text)

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
        # The fixture has 38 raw rows, but three are copies of one posting
        # (36 unique). A real board reports the unique count; the dedupe then
        # does not look like missing coverage.
        cls.total = 36

        class Handler(BaseHTTPRequestHandler):
            jobs = cls.jobs
            total = cls.total

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
                    self._send({"jobs": chunk, "total": self.total})
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
                payload = {"jobs": chunk, "total": self.total}
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
        # Coverage is unique listings, not raw rows: dedupe is not a short
        # crawl when the board's total already counts unique postings.
        self.assertEqual(result["pagination"]["reportedTotal"], 36)
        self.assertEqual(result["pagination"]["rowsFetched"], 38)
        self.assertEqual(result["pagination"]["deduped"], 2)
        quiet = write_pagination(triage, result, company="FatBoard")
        self.assertTrue((triage / "index" / "pagination.json").is_file())
        inv = (triage / "INVENTORY.md").read_text()
        self.assertIn("complete=true", inv)
        self.assertIn("reportedTotal=36", inv)
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

    def test_distinct_postings_sharing_requisition_id_all_staged(self):
        # #48: one Greenhouse requisition can cover several distinct postings.
        # Keying identity on requisition_id silently dropped them at the
        # fingerprint dedupe while still reporting a `complete` crawl.
        from network_jobs.pagination import paginate
        from network_jobs.fingerprint import fingerprint

        payload = {"jobs": [
            {
                "id": 5211297008,
                "requisition_id": "10225",
                "title": "Staff Infrastructure Engineer, Cluster Infrastructure",
                "absolute_url": "https://job-boards.greenhouse.io/x/jobs/5211297008",
                "location": {"name": "London, UK"},
            },
            {
                "id": 5211305008,
                "requisition_id": "10225",
                "title": "Staff Software Engineer, Kubernetes Platform",
                "absolute_url": "https://job-boards.greenhouse.io/x/jobs/5211305008",
                "location": {"name": "London, UK"},
            },
            {
                "id": 5211498008,
                "requisition_id": "10225",
                "title": "Staff Software Engineer, Node Infra",
                "absolute_url": "https://job-boards.greenhouse.io/x/jobs/5211498008",
                "location": {"name": "London, UK"},
            },
        ]}

        def fake_fetch(url):
            import json as _json
            return payload, 200, _json.dumps(payload).encode()

        result = paginate("https://boards-api.greenhouse.io/v1/boards/x/jobs",
                          fetch=fake_fetch, max_pages=5)
        self.assertEqual(len(result["listings"]), 3)
        titles = {j["title"] for j in result["listings"]}
        self.assertIn("Staff Software Engineer, Kubernetes Platform", titles)
        self.assertEqual(len({fingerprint(j) for j in result["listings"]}), 3)

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


class EnvelopeOffsetPaginationTests(unittest.TestCase):
    """Eightfold-shaped boards (NVIDIA): rows and the total sit under `data`,
    and the board pages by `start`, not `offset`/`page`. Upstream read one
    page, found no total, saw no page param, and recorded `complete: true` at
    10 of 2,725 — which the ingest expiry then trusted. The fallback runs after
    detect_next (and replaces an invented `offset=` when the board only pages
    by `start`), so every shape detect_next already handled is untouched; the
    tests below pin both halves, plus the completeness rules."""

    @staticmethod
    def _rows(n, prefix="Eng"):
        return [{"title": f"{prefix} {i}", "url": f"https://x.io/jobs/{i}", "location": "NY"} for i in range(n)]

    @staticmethod
    def _fetch(builder):
        def fake_fetch(url):
            import json as _json
            from urllib.parse import parse_qsl, urlsplit
            q = dict(parse_qsl(urlsplit(url).query))
            body = builder(q)
            return body, 200, _json.dumps(body).encode()
        return fake_fetch

    @classmethod
    def _eightfold(cls, total, size):
        positions = [
            {"id": 893000000000 + i, "name": f"Engineer {i}", "location": "Santa Clara, CA",
             "positionUrl": f"/careers/job/{893000000000 + i}"}
            for i in range(total)
        ]
        return cls._fetch(lambda q: {"status": 200, "error": None, "data": {
            "positions": positions[int(q.get("start", "0")):int(q.get("start", "0")) + size],
            "count": total}})

    def test_nested_total_and_start_param_are_followed(self):
        from network_jobs.pagination import paginate
        url = "https://jobs.example.com/api/pcsx/search?domain=example.com&query=&start=0"
        result = paginate(url, fetch=self._eightfold(25, 10), max_pages=10)
        pag = result["pagination"]
        self.assertEqual(pag["pages"], 3)
        self.assertEqual(pag["scheme"], "start")
        self.assertTrue(pag["complete"])
        self.assertEqual(pag["status"], "complete")
        self.assertEqual(pag["reportedTotal"], 25)
        self.assertEqual(pag["rowsFetched"], 25)
        self.assertNotIn("deduped", pag)
        self.assertEqual(len(result["listings"]), 25)
        # positionUrl is a canonical page link, absolutized at the host root
        self.assertEqual(result["listings"][0]["url"],
                         "https://jobs.example.com/careers/job/893000000000")
        self.assertEqual(result["fetchLog"][1]["url"],
                         "https://jobs.example.com/api/pcsx/search?domain=example.com&query=&start=10")

    def test_cap_on_envelope_board_is_truncated_not_complete(self):
        from network_jobs.pagination import paginate
        url = "https://jobs.example.com/api/pcsx/search?domain=example.com&query=&start=0"
        result = paginate(url, fetch=self._eightfold(2725, 10), max_pages=2)
        pag = result["pagination"]
        self.assertFalse(pag["complete"])
        self.assertTrue(pag["truncated"])
        self.assertEqual(pag["status"], "truncated")
        self.assertEqual(len(result["listings"]), 20)

    def test_start_offset_bootstraps_from_the_url(self):
        # A URL that starts at row 20 only sees the tail (5 of 25). It must
        # never report `complete`: ingest would expire the 20 roles it never
        # fetched. The scheme is still start, and reportedTotal shows coverage.
        from network_jobs.pagination import paginate
        url = "https://jobs.example.com/api/pcsx/search?start=20"
        result = paginate(url, fetch=self._eightfold(25, 10), max_pages=10)
        pag = result["pagination"]
        self.assertEqual(len(result["listings"]), 5)
        self.assertEqual(pag["pages"], 1)
        self.assertEqual(pag["scheme"], "start")
        self.assertEqual(pag["reportedTotal"], 25)
        self.assertEqual(pag["rowsFetched"], 5)
        self.assertFalse(pag["complete"])
        self.assertTrue(pag["truncated"])
        self.assertEqual(pag["status"], "truncated")

    def test_top_level_envelope_pages_by_start(self):
        # Same shape with the rows and count at the top level: detect_next sees
        # the count and invents `offset=`. The crawl probes it once, sees the
        # duplicate page, then locks onto start= and finishes.
        from network_jobs.pagination import paginate
        rows = [{"id": i, "name": f"Engineer {i}", "location": "NY",
                 "positionUrl": f"/job/{i}"} for i in range(25)]

        def build(q):
            start = int(q.get("start", "0"))
            return {"positions": rows[start:start + 10], "count": 25}

        result = paginate("https://x.io/api/search?start=0", fetch=self._fetch(build), max_pages=10)
        pag = result["pagination"]
        self.assertEqual(len(result["listings"]), 25)
        self.assertEqual(pag["pages"], 4)
        self.assertEqual(pag["scheme"], "start")
        self.assertTrue(pag["complete"])
        self.assertEqual(pag["reportedTotal"], 25)
        self.assertEqual([e["url"] for e in result["fetchLog"]], [
            "https://x.io/api/search?start=0",
            "https://x.io/api/search?start=0&offset=10",
            "https://x.io/api/search?start=10",
            "https://x.io/api/search?start=20",
        ])

    def test_envelope_short_of_reported_total_is_truncated(self):
        # The board says 100 but stops serving rows after 20. "No next page"
        # is not evidence the other 80 roles closed.
        from network_jobs.pagination import paginate
        rows = self._rows(20)

        def build(q):
            page = int(q.get("page", "1"))
            return {"jobs": rows[(page - 1) * 10:page * 10], "total": 100}

        result = paginate("https://x.io/api/jobs?page=1", fetch=self._fetch(build),
                          max_pages=10, page_size=10)
        pag = result["pagination"]
        self.assertEqual(len(result["listings"]), 20)
        self.assertEqual(pag["reportedTotal"], 100)
        self.assertEqual(pag["rowsFetched"], 20)
        self.assertFalse(pag["complete"])
        self.assertTrue(pag["truncated"])
        self.assertEqual(pag["status"], "truncated")

    def test_complete_crawl_with_duplicate_rows_stays_complete(self):
        # The board reports 36 unique postings and serves 38 raw rows (two
        # repeats). Unique coverage meets the total, so the crawl is complete.
        from network_jobs.pagination import paginate
        base = self._rows(36)
        rows = base + [dict(base[0]), dict(base[1])]

        def build(q):
            page = int(q.get("page", "1"))
            return {"jobs": rows[(page - 1) * 10:page * 10], "total": 36}

        result = paginate("https://x.io/api/jobs?page=1", fetch=self._fetch(build),
                          max_pages=10, page_size=10)
        pag = result["pagination"]
        self.assertEqual(len(result["listings"]), 36)
        self.assertEqual(pag["reportedTotal"], 36)
        self.assertEqual(pag["rowsFetched"], 38)
        self.assertEqual(pag["deduped"], 2)
        self.assertTrue(pag["complete"])
        self.assertFalse(pag["truncated"])

    def test_duplicate_rows_short_of_reported_total_are_truncated(self):
        # Same raw rows, but the board says 38. Unique coverage is 36, so the
        # two roles that duplicates displaced must not look covered (#69
        # review: raw rows are not coverage).
        from network_jobs.pagination import paginate
        base = self._rows(36)
        rows = base + [dict(base[0]), dict(base[1])]

        def build(q):
            page = int(q.get("page", "1"))
            return {"jobs": rows[(page - 1) * 10:page * 10], "total": 38}

        result = paginate("https://x.io/api/jobs?page=1", fetch=self._fetch(build),
                          max_pages=10, page_size=10)
        pag = result["pagination"]
        self.assertEqual(len(result["listings"]), 36)
        self.assertEqual(pag["reportedTotal"], 38)
        self.assertFalse(pag["complete"])
        self.assertTrue(pag["truncated"])
        self.assertEqual(pag["status"], "truncated")

    def test_first_page_at_page_size_follows_start_not_page(self):
        # detect_next's generic full-page fallback can invent page=2 before the
        # envelope is consulted. The crawl probes it once, sees the duplicate
        # page, then locks onto start=.
        from network_jobs.pagination import paginate
        rows = [{"id": i, "name": f"Engineer {i}", "location": "NY",
                 "positionUrl": f"/job/{i}"} for i in range(100)]

        def build(q):
            start = int(q.get("start", "0"))
            return {"data": {"positions": rows[start:start + 50], "count": 100}}

        result = paginate("https://x.io/api/search?start=0",
                          fetch=self._fetch(build), max_pages=10)
        pag = result["pagination"]
        self.assertEqual(len(result["listings"]), 100)
        self.assertEqual(pag["pages"], 3)
        self.assertEqual(pag["scheme"], "start")
        self.assertTrue(pag["complete"])
        self.assertEqual([e["url"] for e in result["fetchLog"]], [
            "https://x.io/api/search?start=0",
            "https://x.io/api/search?start=0&page=2",
            "https://x.io/api/search?start=50",
        ])

    def test_page_paged_board_with_start_seed_is_not_hijacked(self):
        # A page= board with a leftover start=0 seed must keep its page pager;
        # the old override threw away page=2 and returned half the board.
        from network_jobs.pagination import paginate
        rows = [{"id": i, "name": f"Engineer {i}", "location": "NY",
                 "positionUrl": f"/job/{i}"} for i in range(100)]

        def build(q):
            if "page" in q:
                page = int(q["page"])
                start = (page - 1) * 50
            else:
                start = int(q.get("start", "0"))
            return {"data": {"positions": rows[start:start + 50], "count": 100}}

        result = paginate("https://x.io/api/search?start=0",
                          fetch=self._fetch(build), max_pages=10)
        pag = result["pagination"]
        self.assertEqual(len(result["listings"]), 100)
        self.assertTrue(pag["complete"])
        self.assertEqual(pag["scheme"], "page")
        self.assertNotIn("start=50", "".join(e["url"] for e in result["fetchLog"]))

    def test_offset_board_with_start_zero_is_not_hijacked(self):
        # offset=0 is a decoy, not proof the board pages by start.
        from network_jobs.pagination import paginate
        rows = [{"id": i, "name": f"Engineer {i}", "location": "NY",
                 "positionUrl": f"/job/{i}"} for i in range(100)]

        def build(q):
            offset = int(q.get("offset", "0"))
            return {"data": {"positions": rows[offset:offset + 50], "count": 100}}

        result = paginate("https://x.io/api/search?start=0&offset=0",
                          fetch=self._fetch(build), max_pages=10)
        pag = result["pagination"]
        self.assertEqual(len(result["listings"]), 100)
        self.assertTrue(pag["complete"])
        self.assertEqual(pag["scheme"], "offset")
        self.assertNotIn("start=50", "".join(e["url"] for e in result["fetchLog"]))

    def test_zero_page_decoys_do_not_disable_start(self):
        from network_jobs.pagination import paginate
        rows = [{"id": i, "name": f"Engineer {i}", "location": "NY",
                 "positionUrl": f"/job/{i}"} for i in range(20)]

        def build(q):
            start = int(q.get("start", "0"))
            return {"data": {"positions": rows[start:start + 10], "count": 20}}

        for query in ("pageNumber=0", "offset=00"):
            with self.subTest(query=query):
                result = paginate(f"https://x.io/api/search?start=0&{query}",
                                  fetch=self._fetch(build), max_pages=10)
                self.assertEqual(len(result["listings"]), 20)
                self.assertTrue(result["pagination"]["complete"])

    def test_explicit_total_beats_bare_count_and_accepts_strings(self):
        # A bare `count` is often the page size; an explicit total_results must
        # win. Digit-string totals are accepted too.
        from network_jobs.pagination import paginate
        rows = [{"id": i, "name": f"Engineer {i}", "location": "NY",
                 "positionUrl": f"/job/{i}"} for i in range(25)]

        def build(q):
            start = int(q.get("start", "0"))
            return {"data": {"positions": rows[start:start + 10],
                             "count": 10, "total_results": 25}}

        result = paginate("https://x.io/api/search?start=0",
                          fetch=self._fetch(build), max_pages=10)
        self.assertEqual(len(result["listings"]), 25)
        self.assertEqual(result["pagination"]["reportedTotal"], 25)
        self.assertTrue(result["pagination"]["complete"])

        def string_build(q):
            start = int(q.get("start", "0"))
            return {"data": {"positions": rows[start:start + 10],
                             "count": "25"}}

        string_result = paginate("https://x.io/api/search?start=0",
                                 fetch=self._fetch(string_build), max_pages=10)
        self.assertEqual(len(string_result["listings"]), 25)
        self.assertEqual(string_result["pagination"]["reportedTotal"], 25)
        self.assertTrue(string_result["pagination"]["complete"])

    def test_explicit_meta_total_beats_string_count(self):
        # A digit-string bare count must not shadow a real meta total.
        from network_jobs.pagination import paginate
        rows = [{"id": i, "name": f"Engineer {i}", "location": "NY",
                 "positionUrl": f"/job/{i}"} for i in range(100)]

        def build(q):
            start = int(q.get("start", "0"))
            return {"jobs": rows[start:start + 10],
                    "count": "10", "meta": {"total": 100}}

        result = paginate("https://x.io/api/search?start=0",
                          fetch=self._fetch(build), max_pages=20)
        pag = result["pagination"]
        self.assertEqual(len(result["listings"]), 100)
        self.assertEqual(pag["reportedTotal"], 100)
        self.assertTrue(pag["complete"])

    def test_envelope_count_does_not_shadow_larger_top_level_total(self):
        # data.count=50 is a page-sized count; the top-level total=100 must
        # win, or the crawl stops halfway and calls it complete.
        from network_jobs.pagination import paginate
        rows = [{"id": i, "name": f"Engineer {i}", "location": "NY",
                 "positionUrl": f"/job/{i}"} for i in range(100)]

        def build(q):
            start = int(q.get("start", "0"))
            return {"data": {"positions": rows[start:start + 10],
                             "count": "50"}, "total": 100}

        result = paginate("https://x.io/api/search?start=0",
                          fetch=self._fetch(build), max_pages=20)
        pag = result["pagination"]
        self.assertEqual(len(result["listings"]), 100)
        self.assertEqual(pag["reportedTotal"], 100)
        self.assertTrue(pag["complete"])

    def test_superscript_count_is_ignored_and_total_results_wins(self):
        # "²".isdigit() is true but int() raises; explicit totals still work.
        from network_jobs.pagination import paginate
        rows = [{"id": i, "name": f"Engineer {i}", "location": "NY",
                 "positionUrl": f"/job/{i}"} for i in range(25)]

        def build(q):
            start = int(q.get("start", "0"))
            return {"jobs": rows[start:start + 10],
                    "count": "²", "total_results": 25}

        result = paginate("https://x.io/api/search?start=0",
                          fetch=self._fetch(build), max_pages=10)
        self.assertEqual(len(result["listings"]), 25)
        self.assertEqual(result["pagination"]["reportedTotal"], 25)
        self.assertTrue(result["pagination"]["complete"])

    def test_total_smaller_than_page_is_ignored(self):
        from network_jobs.pagination import paginate
        rows = self._rows(10)
        fetch = self._fetch(lambda q: {"jobs": rows, "count": 3})
        result = paginate("https://x.io/api/jobs", fetch=fetch, max_pages=5)
        self.assertEqual(len(result["listings"]), 10)
        self.assertNotIn("reportedTotal", result["pagination"])

    def test_max_pages_exact_covered_board_is_complete(self):
        # 3 x 50 = 150 with total 150 is complete, not a cap truncation.
        from network_jobs.pagination import paginate
        rows = [{"id": i, "name": f"Engineer {i}", "location": "NY",
                 "positionUrl": f"/job/{i}"} for i in range(150)]

        def build(q):
            page = int(q.get("page", "1"))
            return {"jobs": rows[(page - 1) * 50:page * 50], "total": 150}

        result = paginate("https://x.io/api/jobs?page=1",
                          fetch=self._fetch(build), max_pages=3, page_size=50)
        pag = result["pagination"]
        self.assertEqual(len(result["listings"]), 150)
        self.assertTrue(pag["complete"])
        self.assertFalse(pag["truncated"])

    def test_a_later_smaller_total_does_not_lower_the_bar(self):
        # Page 1 says 25, page 2 says 15 after 20 unique rows. The larger
        # first total must stand, so the crawl stays truncated.
        from network_jobs.pagination import paginate
        rows = [{"id": i, "name": f"Engineer {i}", "location": "NY",
                 "positionUrl": f"/job/{i}"} for i in range(20)]

        def build(q):
            start = int(q.get("start", "0"))
            total = 25 if start == 0 else 15
            return {"data": {"positions": rows[start:start + 10], "count": total}}

        result = paginate("https://x.io/api/search?start=0",
                          fetch=self._fetch(build), max_pages=10)
        pag = result["pagination"]
        self.assertEqual(len(result["listings"]), 20)
        self.assertEqual(pag["reportedTotal"], 25)
        self.assertFalse(pag["complete"])
        self.assertTrue(pag["truncated"])

    def test_zero_offset_does_not_disable_start(self):
        # A start-paged URL with a zero offset decoy must still follow start.
        from network_jobs.pagination import paginate
        rows = [{"id": i, "name": f"Engineer {i}", "location": "NY",
                 "positionUrl": f"/job/{i}"} for i in range(20)]

        def build(q):
            start = int(q.get("start", "0"))
            return {"data": {"positions": rows[start:start + 10], "count": 20}}

        result = paginate("https://x.io/api/search?start=0&offset=0",
                          fetch=self._fetch(build), max_pages=10)
        self.assertEqual(len(result["listings"]), 20)
        self.assertEqual(result["pagination"]["scheme"], "start")
        self.assertTrue(result["pagination"]["complete"])

    def test_exact_max_listings_is_not_truncated_when_board_is_covered(self):
        # A board with exactly max_listings rows is complete, not capped.
        from network_jobs.pagination import paginate
        rows = self._rows(20)
        fetch = self._fetch(lambda q: {"jobs": rows, "total": 20})
        result = paginate("https://x.io/api/jobs", fetch=fetch,
                          max_pages=5, max_listings=20, page_size=20)
        pag = result["pagination"]
        self.assertEqual(len(result["listings"]), 20)
        self.assertTrue(pag["complete"])
        self.assertFalse(pag["truncated"])

    def test_mid_board_page_entry_is_not_complete(self):
        # No total to compare against; the page=2 entry itself is the proof
        # that page one was never read.
        from network_jobs.pagination import paginate
        rows = self._rows(30)

        def build(q):
            page = int(q.get("page", "1"))
            return {"jobs": rows[(page - 1) * 10:page * 10]}

        result = paginate("https://x.io/api/jobs?page=2", fetch=self._fetch(build),
                          max_pages=10, page_size=10)
        pag = result["pagination"]
        self.assertEqual(len(result["listings"]), 20)
        self.assertFalse(pag["complete"])
        self.assertTrue(pag["truncated"])
        self.assertEqual(pag["status"], "truncated")

    # --- shapes detect_next already handled must come out exactly as before ---

    def test_unrelated_nested_count_does_not_switch_a_page_board_to_offset(self):
        from network_jobs.pagination import paginate
        rows = self._rows(100)
        fetch = self._fetch(lambda q: {"jobs": rows[(int(q.get("page", "1")) - 1) * 50:int(q.get("page", "1")) * 50],
                                       "data": {"count": 1000, "facets": {}}})
        result = paginate("https://x.io/api/jobs", fetch=fetch, max_pages=10, page_size=50)
        self.assertEqual(len(result["listings"]), 100)
        self.assertTrue(result["pagination"]["complete"])
        self.assertEqual(result["pagination"]["scheme"], "page")

    def test_digit_valued_filter_beside_page_is_not_an_offset(self):
        from network_jobs.pagination import paginate
        rows = self._rows(20)
        fetch = self._fetch(lambda q: {"jobs": rows[(int(q.get("page", "1")) - 1) * 10:int(q.get("page", "1")) * 10],
                                       "total": 20})
        result = paginate("https://x.io/api/jobs?from=0&page=1", fetch=fetch, max_pages=10, page_size=10)
        self.assertEqual(len(result["listings"]), 20)
        self.assertTrue(result["pagination"]["complete"])
        self.assertNotIn("from=10", result["fetchLog"][1]["url"])

    def test_digit_valued_filter_without_page_still_pages_by_number(self):
        # `from=2020` is a year filter on a page-counting board with no page
        # param in the URL and no total: detect_next's full-page fallback must
        # keep winning. (With a top-level total upstream itself takes the offset
        # branch and stops at 50; that is pre-existing and not changed here.)
        from network_jobs.pagination import paginate
        rows = self._rows(100)
        fetch = self._fetch(lambda q: {"jobs": rows[(int(q.get("page", "1")) - 1) * 50:int(q.get("page", "1")) * 50]})
        result = paginate("https://x.io/api/jobs?from=2020", fetch=fetch, max_pages=10, page_size=50)
        self.assertEqual(len(result["listings"]), 100)
        self.assertTrue(result["pagination"]["complete"])
        self.assertNotIn("from=2070", result["fetchLog"][1]["url"])

    def test_start_filter_on_a_page_board_is_not_promoted(self):
        # Same board with `start=2020`: rows live at the top level, so there is
        # no envelope count beside them and the fallback declines.
        from network_jobs.pagination import paginate
        rows = self._rows(100)
        fetch = self._fetch(lambda q: {"jobs": rows[(int(q.get("page", "1")) - 1) * 50:int(q.get("page", "1")) * 50],
                                       "data": {"count": 999}})
        result = paginate("https://x.io/api/jobs?start=2020", fetch=fetch, max_pages=10, page_size=50)
        self.assertEqual(len(result["listings"]), 100)
        self.assertTrue(result["pagination"]["complete"])
        self.assertNotIn("start=2070", "".join(e["url"] for e in result["fetchLog"]))

    def test_first_link_only_then_page_and_total_keeps_counting(self):
        from network_jobs.pagination import paginate
        rows = self._rows(100)

        def build(q):
            page = int(q.get("page", "1"))
            body = {"jobs": rows[(page - 1) * 10:page * 10], "total": 100}
            if page == 1:
                body["next"] = "https://x.io/api/jobs?page=2"
            return body
        result = paginate("https://x.io/api/jobs", fetch=self._fetch(build), max_pages=20, page_size=10)
        self.assertEqual(len(result["listings"]), 100)
        self.assertTrue(result["pagination"]["complete"])

    def test_link_paged_board_with_unrelated_nested_count(self):
        from network_jobs.pagination import paginate
        rows = self._rows(150)

        def build(q):
            page = int(q.get("page", "1"))
            body = {"jobs": rows[(page - 1) * 10:page * 10], "data": {"count": 1000, "positions": []}}
            if page < 15:
                body["next"] = f"https://x.io/api/jobs?page={page + 1}"
            return body
        result = paginate("https://x.io/api/jobs?page=1", fetch=self._fetch(build), max_pages=15, page_size=50)
        self.assertEqual(len(result["listings"]), 150)
        self.assertTrue(result["pagination"]["complete"])
        self.assertFalse(result["pagination"]["truncated"])

    def test_start_strategy_survives_a_larger_later_page(self):
        # Second-reader counterexample: page 1 has 10 rows, later pages 50.
        # The fallback's scheme must not let detect_next invent offset= once
        # a page reaches page_size; the crawl stays on start= to the end.
        from network_jobs.pagination import paginate
        rows = [{"id": i, "name": f"Engineer {i}", "location": "NY", "positionUrl": f"/job/{i}"} for i in range(100)]

        def build(q):
            start = int(q.get("start", "0"))
            size = 10 if start == 0 else 50
            return {"data": {"positions": rows[start:start + size], "count": 100}}
        result = paginate("https://x.io/api/search?start=0", fetch=self._fetch(build), max_pages=15)
        self.assertEqual(len(result["listings"]), 100)
        self.assertTrue(result["pagination"]["complete"])
        self.assertEqual(result["pagination"]["scheme"], "start")
        urls = [e["url"] for e in result["fetchLog"]]
        self.assertEqual(urls, ["https://x.io/api/search?start=0", "https://x.io/api/search?start=10",
                                "https://x.io/api/search?start=60"])

    def test_envelope_count_must_sit_beside_the_extracted_rows(self):
        from network_jobs.pagination import detect_next_envelope, extract_job_list
        url = "https://x.io/api/jobs?start=0"
        beside = {"status": 200, "data": {"positions": self._rows(10), "count": 25}}
        rows = extract_job_list(beside)
        self.assertEqual(detect_next_envelope(url, beside, rows, last_count=10),
                         "https://x.io/api/jobs?start=10")
        top = {"positions": self._rows(10), "count": 25}
        self.assertEqual(detect_next_envelope(url, top, extract_job_list(top), last_count=10),
                         "https://x.io/api/jobs?start=10")
        elsewhere = {"jobs": self._rows(10), "data": {"positions": self._rows(3, "Other"), "count": 999}}
        rows = extract_job_list(elsewhere)
        self.assertIsNone(detect_next_envelope(url, elsewhere, rows, last_count=10))
        two_hops = {"data": {"payload": {"positions": self._rows(10), "count": 123}}}
        rows = extract_job_list(two_hops)
        self.assertIsNone(detect_next_envelope(url, two_hops, rows, last_count=10))
        self.assertIsNone(detect_next_envelope("https://x.io/api/jobs?start=2026-01-01", beside,
                                               extract_job_list(beside), last_count=10))
        self.assertIsNone(detect_next_envelope("https://x.io/api/jobs?start=20", beside,
                                               extract_job_list(beside), last_count=5))


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

    def test_expiry_refused_for_blocked_and_captured_status(self):
        # #39: blocked (unreadable) and captured (user-saved page) are never
        # evidence a role closed — even when the model passes
        # --pagination-complete, and even with force_expire.
        from network_jobs.corpus import merge_jobs

        existing = [
            self._job(externalId="1", url="https://fatboard.example/jobs/1"),
            self._job(title="Staff Engineer", externalId="2",
                      url="https://fatboard.example/jobs/2"),
        ]
        incoming = [self._job(externalId="1", url="https://fatboard.example/jobs/1")]
        for status in ("blocked", "captured"):
            with self.subTest(status=status):
                merged, info = merge_jobs(
                    existing, incoming, expire_company="FatBoard",
                    pagination_complete=True, pagination_status=status,
                    expected_total=1)
                self.assertTrue(info["expirySkipped"])
                self.assertEqual(info["expired"], 0)
                self.assertIn(status, info["expiryReason"])
                statuses = {j["externalId"]: j.get("status", "open") for j in merged}
                self.assertEqual(statuses["2"], "open")
                # force_expire is for filtered batches, not for unreadable
                # boards or single-page captures.
                merged, info = merge_jobs(
                    existing, incoming, expire_company="FatBoard",
                    pagination_complete=True, pagination_status=status,
                    expected_total=9, force_expire=True)
                self.assertEqual(info["expired"], 0)

    def test_expiry_refused_for_truncated_status(self):
        # A truncated crawl is partial. Even if a model wrongly passes
        # --pagination-complete, expiry must stay closed; force_expire is the
        # explicit override (unlike blocked/captured).
        from network_jobs.corpus import merge_jobs

        existing = [
            self._job(externalId="1", url="https://fatboard.example/jobs/1"),
            self._job(title="Staff Engineer", externalId="2",
                      url="https://fatboard.example/jobs/2"),
        ]
        incoming = [self._job(externalId="1", url="https://fatboard.example/jobs/1")]
        merged, info = merge_jobs(
            existing, incoming, expire_company="FatBoard",
            pagination_complete=True, pagination_status="truncated",
            expected_total=1)
        self.assertTrue(info["expirySkipped"])
        self.assertEqual(info["expired"], 0)
        self.assertIn("truncated", info["expiryReason"])
        statuses = {j["externalId"]: j.get("status", "open") for j in merged}
        self.assertEqual(statuses["2"], "open")

        merged, info = merge_jobs(
            existing, incoming, expire_company="FatBoard",
            pagination_complete=True, pagination_status="truncated",
            expected_total=1, force_expire=True)
        self.assertFalse(info["expirySkipped"])
        self.assertEqual(info["expired"], 1)

        # The normal truncated path passes --pagination-incomplete; it is a
        # silent no-op, not a "pass --force-expire" warning (force cannot
        # open a complete=false crawl).
        merged, info = merge_jobs(
            existing, incoming, expire_company="FatBoard",
            pagination_complete=False, pagination_status="truncated",
            expected_total=1)
        self.assertFalse(info["expirySkipped"])
        self.assertEqual(info["expired"], 0)

    def test_expiry_skipped_for_empty_when_company_had_open_roles(self):
        # An empty crawl with N>0 prior open roles must not wipe the company
        # (a filtered / region-limited URL can return []). Warning is loud.
        from network_jobs.corpus import merge_jobs

        existing = [
            self._job(externalId="1", url="https://fatboard.example/jobs/1"),
            self._job(title="Staff Engineer", externalId="2",
                      url="https://fatboard.example/jobs/2"),
        ]
        merged, info = merge_jobs(
            existing, [], expire_company="FatBoard", pagination_complete=True,
            pagination_status="empty", expected_total=0)
        self.assertTrue(info["expirySkipped"])
        self.assertEqual(info["expired"], 0)
        self.assertIn("2 open roles", info["expiryReason"])
        self.assertIn("force-expire", info["expiryReason"])
        statuses = {j["externalId"]: j.get("status", "open") for j in merged}
        self.assertEqual(statuses["1"], "open")
        self.assertEqual(statuses["2"], "open")

        # A normal complete crawl is unchanged.
        merged, info = merge_jobs(
            existing, [self._job(externalId="1",
                                 url="https://fatboard.example/jobs/1")],
            expire_company="FatBoard", pagination_complete=True,
            pagination_status="complete", expected_total=1)
        self.assertFalse(info["expirySkipped"])
        self.assertEqual(info["expired"], 1)

    def test_expiry_skipped_for_empty_when_status_omitted(self):
        # The guard keys on zero incoming rows, not on --pagination-status.
        from network_jobs.corpus import merge_jobs

        existing = [
            self._job(externalId="1", url="https://fatboard.example/jobs/1"),
            self._job(title="Staff Engineer", externalId="2",
                      url="https://fatboard.example/jobs/2"),
        ]
        merged, info = merge_jobs(
            existing, [], expire_company="FatBoard", pagination_complete=True,
            expected_total=0)
        self.assertTrue(info["expirySkipped"])
        self.assertEqual(info["expired"], 0)
        self.assertIn("2 open roles", info["expiryReason"])
        statuses = {j["externalId"]: j.get("status", "open") for j in merged}
        self.assertEqual(statuses["1"], "open")
        self.assertEqual(statuses["2"], "open")

    def test_expiry_skipped_for_empty_when_status_null(self):
        # jq -r '.status' on an older pagination.json with no status field
        # prints the string "null".
        from network_jobs.corpus import merge_jobs

        existing = [
            self._job(externalId="1", url="https://fatboard.example/jobs/1"),
            self._job(title="Staff Engineer", externalId="2",
                      url="https://fatboard.example/jobs/2"),
        ]
        merged, info = merge_jobs(
            existing, [], expire_company="FatBoard", pagination_complete=True,
            pagination_status="null", expected_total=0)
        self.assertTrue(info["expirySkipped"])
        self.assertEqual(info["expired"], 0)
        self.assertIn("2 open roles", info["expiryReason"])
        statuses = {j["externalId"]: j.get("status", "open") for j in merged}
        self.assertEqual(statuses["1"], "open")
        self.assertEqual(statuses["2"], "open")

    def test_expiry_proceeds_for_empty_with_force_expire(self):
        from network_jobs.corpus import merge_jobs

        existing = [
            self._job(externalId="1", url="https://fatboard.example/jobs/1"),
            self._job(title="Staff Engineer", externalId="2",
                      url="https://fatboard.example/jobs/2"),
        ]
        merged, info = merge_jobs(
            existing, [], expire_company="FatBoard", pagination_complete=True,
            pagination_status="empty", expected_total=0, force_expire=True)
        self.assertFalse(info["expirySkipped"])
        self.assertEqual(info["expired"], 2)
        self.assertEqual({j.get("status") for j in merged}, {"closed"})

    def test_expiry_allowed_for_empty_when_company_had_no_open_roles(self):
        from network_jobs.corpus import merge_jobs

        existing = [
            self._job(externalId="1", url="https://fatboard.example/jobs/1",
                      status="closed", closedAt="2026-01-01"),
        ]
        merged, info = merge_jobs(
            existing, [], expire_company="FatBoard", pagination_complete=True,
            pagination_status="empty", expected_total=0)
        self.assertFalse(info["expirySkipped"])
        self.assertEqual(info["expired"], 0)
        self.assertEqual(merged[0]["status"], "closed")

        # Brand-new company (nothing in the corpus) may also record empty.
        merged, info = merge_jobs(
            [], [], expire_company="FatBoard", pagination_complete=True,
            pagination_status="empty", expected_total=0)
        self.assertFalse(info["expirySkipped"])
        self.assertEqual(info["expired"], 0)
        self.assertEqual(merged, [])

    def test_rebuild_records_pagination_status(self):
        from network_jobs.corpus import rebuild

        existing = [self._job(externalId="keep"),
                    self._job(title="Old Role", externalId="gone",
                              url="https://x/gone")]
        (self.corpus / "jobs-all.json").write_text(json.dumps(existing) + "\n")
        batch = self.tmp / "batch.json"
        batch.write_text(json.dumps([self._job(externalId="keep")]) + "\n")
        summary = rebuild(batch, self.corpus, expire_company="FatBoard",
                          pagination_complete=True, pagination_status="captured")
        self.assertFalse(summary["expired"])
        self.assertEqual(summary["expiredCount"], 0)
        self.assertTrue(summary["expirySkipped"])
        self.assertEqual(summary["paginationStatus"], "captured")
        jobs = json.loads((self.corpus / "jobs-all.json").read_text())
        self.assertEqual([j for j in jobs if j.get("status") == "closed"], [])

    def test_rebuild_cli_warns_on_empty_sudden_drop(self):
        existing = [
            self._job(externalId="1", url="https://fatboard.example/jobs/1"),
            self._job(title="Staff Engineer", externalId="2",
                      url="https://fatboard.example/jobs/2"),
        ]
        (self.corpus / "jobs-all.json").write_text(json.dumps(existing) + "\n")
        batch = self.tmp / "empty-batch.json"
        batch.write_text("[]\n")
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = helper_main([
                "rebuild", str(batch), "--data", str(self.tmp),
                "--expire-company", "FatBoard",
                "--pagination-complete",
                "--pagination-status", "empty",
                "--expect-count", "0",
            ])
        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["expirySkipped"])
        self.assertEqual(payload["expiredCount"], 0)
        warning = stderr.getvalue()
        self.assertIn("warning:", warning)
        self.assertIn("2 open roles", warning)
        self.assertIn("force-expire", warning)
        jobs = json.loads((self.corpus / "jobs-all.json").read_text())
        self.assertEqual({j.get("status", "open") for j in jobs}, {"open"})

    def test_rebuild_cli_warns_on_empty_when_status_omitted(self):
        existing = [
            self._job(externalId="1", url="https://fatboard.example/jobs/1"),
        ]
        (self.corpus / "jobs-all.json").write_text(json.dumps(existing) + "\n")
        batch = self.tmp / "empty-batch.json"
        batch.write_text("[]\n")
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = helper_main([
                "rebuild", str(batch), "--data", str(self.tmp),
                "--expire-company", "FatBoard",
                "--pagination-complete",
                "--expect-count", "0",
            ])
        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["expirySkipped"])
        self.assertEqual(payload["expiredCount"], 0)
        self.assertIn("warning:", stderr.getvalue())
        jobs = json.loads((self.corpus / "jobs-all.json").read_text())
        self.assertEqual(jobs[0].get("status", "open"), "open")

    def test_rebuild_cli_force_expire_empty_drops_prior_roles(self):
        existing = [
            self._job(externalId="1", url="https://fatboard.example/jobs/1"),
            self._job(title="Staff Engineer", externalId="2",
                      url="https://fatboard.example/jobs/2"),
        ]
        (self.corpus / "jobs-all.json").write_text(json.dumps(existing) + "\n")
        batch = self.tmp / "empty-batch.json"
        batch.write_text("[]\n")
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = helper_main([
                "rebuild", str(batch), "--data", str(self.tmp),
                "--expire-company", "FatBoard",
                "--pagination-complete",
                "--pagination-status", "empty",
                "--expect-count", "0",
                "--force-expire",
            ])
        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["expirySkipped"])
        self.assertEqual(payload["expiredCount"], 2)
        self.assertNotIn("warning:", stderr.getvalue())
        jobs = json.loads((self.corpus / "jobs-all.json").read_text())
        self.assertEqual({j.get("status") for j in jobs}, {"closed"})

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

    def test_classify_in_place_keeps_matches_envelope(self):
        # No `--out` means `dest == --input`. On a matches.json object that
        # must stay an object with its summary keys: writing `result["jobs"]`
        # straight back would re-create the #32 bare-array flip.
        tmp = Path(tempfile.mkdtemp(prefix="nj-clf-inplace-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        matches = tmp / "index" / "matches.json"
        matches.parent.mkdir(parents=True)
        envelope = {
            "nListings": 42,
            "nMatches": 2,
            "showing": 2,
            "departments": {"Product": 1, "Engineering": 1},
            "ingestDefault": "matches",
            "jobs": [
                {"title": "Senior Product Manager", "location": "New York, NY"},
                {"title": "Engineering Manager", "location": "Remote"},
            ],
        }
        matches.write_text(json.dumps(envelope) + "\n")

        rc = helper_main(["classify", "--input", str(matches), "--company", "Stripe"])
        self.assertEqual(rc, 0)

        written = json.loads(matches.read_text())
        self.assertIsInstance(written, dict)
        self.assertEqual(written["nListings"], 42)
        self.assertEqual(written["nMatches"], 2)
        self.assertEqual(written["showing"], 2)
        self.assertEqual(written["departments"],
                         {"Product": 1, "Engineering": 1})
        self.assertEqual(written["ingestDefault"], "matches")
        self.assertEqual([j["title"] for j in written["jobs"]],
                         ["Senior Product Manager", "Engineering Manager"])
        self.assertEqual(written["jobs"][0]["category"], "product")
        self.assertEqual(written["jobs"][1]["track"], "manager")

    def test_classify_in_place_upgrades_legacy_bare_array(self):
        # A bare-array input has no summary keys to preserve, so an in-place
        # run upgrades it to the documented object, matching `normalize_matches`.
        tmp = Path(tempfile.mkdtemp(prefix="nj-clf-upgrade-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        matches = tmp / "index" / "matches.json"
        matches.parent.mkdir(parents=True)
        matches.write_text(json.dumps([
            {"title": "Senior Product Manager", "location": "New York, NY"},
        ]) + "\n")

        rc = helper_main(["classify", "--input", str(matches), "--company", "Stripe"])
        self.assertEqual(rc, 0)

        written = json.loads(matches.read_text())
        self.assertIsInstance(written, dict)
        self.assertEqual(written["nMatches"], 1)
        self.assertEqual(written["ingestDefault"], "matches")
        self.assertEqual(written["jobs"][0]["category"], "product")

    def test_classify_without_out_then_rebuild(self):
        # classify with no --out wraps a plain job list in the matches object.
        # rebuild-corpus used to reject that file with "must be a JSON array".
        tmp = Path(tempfile.mkdtemp(prefix="nj-clf-rebuild-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        batch = tmp / "batch.json"
        batch.write_text(json.dumps([
            {"title": "Senior Product Manager", "location": "New York, NY",
             "company": "Stripe", "url": "https://stripe.com/jobs/pm"},
        ]) + "\n")

        rc = helper_main(["classify", "--input", str(batch), "--company", "Stripe"])
        self.assertEqual(rc, 0)
        wrapped = json.loads(batch.read_text())
        self.assertIsInstance(wrapped, dict)
        self.assertIsInstance(wrapped.get("jobs"), list)

        rc = helper_main(["rebuild", str(batch), "--data", str(tmp)])
        self.assertEqual(rc, 0)
        corpus_jobs = json.loads((tmp / "corpus" / "jobs-all.json").read_text())
        self.assertEqual(len(corpus_jobs), 1)
        self.assertEqual(corpus_jobs[0]["title"], "Senior Product Manager")

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


class ProgramManagerClassificationTests(unittest.TestCase):
    """#57: a program manager's domain decides the category, not "product".

    The product row used to contain a bare `program manager`, so a compliance
    program manager was a *confirmed* product role and outranked actual PMs on
    a "product manager" search.
    """

    def _category(self, title):
        from network_jobs.classify import classify_job

        return classify_job({"title": title, "location": "Remote"})

    def test_domain_program_managers_are_not_product(self):
        for title, category in (
            ("Senior Compliance Program Manager, Political Law", "legal"),
            ("Legal Program Manager, Compute & Infrastructure", "legal"),
            ("Regulatory Program Manager", "legal"),
            ("Privacy Program Manager", "legal"),
            ("Security Program Manager", "it-security"),
            ("People Program Manager", "people"),
            ("Sales Program Manager", "sales"),
            ("Marketing Program Manager", "marketing"),
            ("Operations Program Manager", "operations"),
        ):
            out = self._category(title)
            self.assertEqual(out["category"], category, title)
            self.assertEqual(out["categoryConfidence"], "high", title)
            self.assertFalse(out["needsLlm"], title)

    def test_product_and_technical_program_managers_stay_product(self):
        for title in (
            "Product Manager, Claude",
            "Technical Product Manager",
            "Technical Program Manager, Launches",
            "TPM, Infrastructure",
            "Group PM",
            "Staff PM",
            "Principal PM",
            "Product Lead",
            "Product Owner",
        ):
            out = self._category(title)
            self.assertEqual(out["category"], "product", title)
            self.assertEqual(out["categoryConfidence"], "high", title)

    def test_bare_program_manager_is_ambiguous_not_product(self):
        for title in ("Program Manager", "Senior Program Manager"):
            out = self._category(title)
            self.assertEqual(out["category"], "other", title)
            self.assertEqual(out["categoryConfidence"], "low", title)
            self.assertTrue(out["needsLlm"], title)

        # No domain -> no category bonus on a product-only search. It also
        # hard-fails like any other unrelated category (#40: a weak guess may
        # fail to match; it must never earn points).
        scored = score_job({"title": "Senior Program Manager", "company": "Acme",
                            "location": "Remote"}, {"categories": ["product"]})
        self.assertNotIn("category", scored["matchReasons"])
        self.assertNotIn("category-affinity", scored["matchReasons"])
        self.assertIn("category-mismatch", scored["matchReasons"])
        self.assertFalse(scored["matched"])

    def test_unrelated_titles_unchanged(self):
        out = self._category("Senior Software Engineer")
        self.assertEqual(out["category"], "engineering")
        self.assertEqual(out["categoryConfidence"], "high")

    def test_compliance_program_manager_does_not_outrank_pms(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-program-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        corpus = tmp / "corpus"
        corpus.mkdir()
        jobs = [
            {
                "id": "compliance-pm",
                "title": "Senior Compliance Program Manager, Political Law",
                "company": "Anthropic",
                "location": "Remote",
                "lastSeen": "2026-10-20",
            },
            {
                "id": "pm-claude",
                "title": "Product Manager, Claude",
                "company": "Anthropic",
                "location": "Remote",
                "lastSeen": "2026-10-20",
            },
            {
                "id": "spm-growth",
                "title": "Senior Product Manager, Growth",
                "company": "Stripe",
                "location": "Remote",
                "lastSeen": "2026-10-20",
            },
            {
                "id": "staff-pm-platform",
                "title": "Staff Product Manager, Platform",
                "company": "Figma",
                "location": "Remote",
                "lastSeen": "2026-10-20",
            },
        ]
        (corpus / "jobs-all.json").write_text(json.dumps(jobs, indent=2) + "\n")
        result = rank_corpus(
            data_dir=tmp,
            k=3,
            query="product manager",
            prefs={"categories": ["product"], "seniority": ["senior"],
                   "workModes": ["remote"]},
            resume_text="",
        )
        titles = [j["title"] for j in result["jobs"]]
        self.assertEqual(len(titles), 3)
        self.assertNotIn("Senior Compliance Program Manager, Political Law",
                         titles)
        self.assertIn("Senior Product Manager, Growth", titles)


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

    @staticmethod
    def _http_error(url, code, msg="http error", headers=None):
        """HTTPError with no body, closed so it raises no ResourceWarning."""
        exc = HTTPError(url, code, msg, headers, None)
        exc.close()
        return exc

    @staticmethod
    def _retry_after_headers(value):
        headers = Message()
        headers["Retry-After"] = value
        return headers

    @staticmethod
    def _bot_wall_headers(retry_after=None):
        headers = Message()
        headers["Server"] = "CloudFront"
        headers["x-sigsci-request-id"] = "req-1"
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        return headers

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

    def test_fetch_job_description_reports_http_status(self):
        # #64: the status code used to be erased into `fetch-failed: HTTPError`,
        # so 429 (retryable) and 404 (gone) looked identical.
        from network_jobs.descriptions import fetch_job_description

        def fetch(url):
            code = 429 if url.endswith("/throttled") else 404
            raise self._http_error(url, code, "nope")

        self.assertEqual(
            fetch_job_description("https://x/throttled", fetch=fetch),
            {"error": "http-429"})
        self.assertEqual(
            fetch_job_description("https://x/gone", fetch=fetch),
            {"error": "http-404"})

    def _retry_triage(self, n=1):
        """Triage dir with `n` undescribed rows on one host."""
        tmp = Path(tempfile.mkdtemp(prefix="nj-desc-retry-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        index = tmp / "triage" / "t" / "index"
        index.mkdir(parents=True)
        (index / "matches.json").write_text(json.dumps([
            {"title": f"Senior PM {i}", "url": f"https://x/jobs/{i}"}
            for i in range(1, n + 1)
        ]) + "\n")
        return tmp / "triage" / "t"

    def _triage_for(self, urls):
        """Triage dir whose rows point at the given URLs, in order."""
        tmp = Path(tempfile.mkdtemp(prefix="nj-desc-web-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        index = tmp / "triage" / "t" / "index"
        index.mkdir(parents=True)
        (index / "matches.json").write_text(json.dumps([
            {"title": f"Senior PM {i}", "url": url}
            for i, url in enumerate(urls, 1)
        ]) + "\n")
        return tmp / "triage" / "t"

    def test_fetch_descriptions_retries_transient_failure(self):
        # #64: a throttled first pass must not lose the body. Stateful fetch
        # fails once with a network error, then succeeds; sleep is injected.
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage()
        calls = []
        sleeps = []

        def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                raise ConnectionError("throttled")
            return 200, "text/html", self.HTML.encode()

        result = fetch_descriptions(
            triage, fetch=fetch, sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(calls), 2)
        self.assertIn("payments infrastructure",
                      json.loads((triage / "index" / "matches.json")
                                 .read_text())["jobs"][0]["description"])
        self.assertTrue(sleeps)
        self.assertEqual(sleeps, [1.0])

    def test_fetch_descriptions_retries_429_then_backs_off(self):
        # #64: 429 is retryable and the backoff doubles per attempt.
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage()
        calls = []
        sleeps = []

        def fetch(url):
            calls.append(url)
            if len(calls) <= 2:
                raise self._http_error(url, 429, "slow down")
            return 200, "text/html", self.HTML.encode()

        result = fetch_descriptions(
            triage, fetch=fetch, sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_fetch_descriptions_gives_up_after_retries(self):
        # #64: retries are bounded; the last error is recorded exactly as
        # before, and the run still succeeds.
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage()
        calls = []
        sleeps = []

        def fetch(url):
            calls.append(url)
            return 503, "text/html", b"unavailable"

        result = fetch_descriptions(
            triage, fetch=fetch, sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(calls), 3)  # one try + retries=2
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("http-503", result["errors"][0])

    def test_fetch_descriptions_does_not_retry_404(self):
        # #64: a closed role is not transient — exactly one request, no sleep.
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage()
        calls = []
        sleeps = []

        def fetch(url):
            calls.append(url)
            raise self._http_error(url, 404, "not found")

        result = fetch_descriptions(
            triage, fetch=fetch, sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("http-404", result["errors"][0])
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_real_http_error_carries_retry_after(self):
        # The 429 path through the real urlopen, not an injected fetch: the
        # handler's Retry-After must reach the result.
        import http.server
        import threading
        from network_jobs.descriptions import fetch_job_description

        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(429)
                self.send_header("Retry-After", "2")
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"slow down")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]

        result = fetch_job_description(
            f"http://127.0.0.1:{port}/jobs/1", timeout=5)

        self.assertEqual(result["error"], "http-429")
        self.assertEqual(result["retryAfter"], 2.0)
        self.assertEqual(len(seen), 1)

    def test_retry_after_seconds_replaces_backoff(self):
        # #64 review: a throttled host that says "wait 5s" must be obeyed
        # instead of the 1s exponential guess.
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage()
        calls = []
        sleeps = []

        def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                raise self._http_error(
                    url, 429, "slow down", self._retry_after_headers("5"))
            return 200, "text/html", self.HTML.encode()

        result = fetch_descriptions(
            triage, fetch=fetch, sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(sleeps, [5.0])
        self.assertNotIn("retriesDisabled", result)

    def test_retry_after_applies_to_503(self):
        # 503 is the other status where the server knows better than we do.
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage()
        calls = []
        sleeps = []

        def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                raise self._http_error(
                    url, 503, "unavailable", self._retry_after_headers("4"))
            return 200, "text/html", self.HTML.encode()

        result = fetch_descriptions(
            triage, fetch=fetch, sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(sleeps, [4.0])

    def test_retry_after_http_date_is_used(self):
        # The header's other legal form: an absolute HTTP-date. Parse it
        # directly (no float() shortcut) and then end-to-end.
        import email.utils
        from datetime import datetime, timedelta, timezone
        from network_jobs.descriptions import (
            _retry_after_seconds, fetch_descriptions)

        when = email.utils.format_datetime(
            datetime.now(timezone.utc) + timedelta(seconds=7), usegmt=True)
        self.assertTrue(when.endswith("GMT"))
        self.assertFalse(when.replace(".", "", 1).isdigit(), when)
        parsed = _retry_after_seconds(self._retry_after_headers(when))
        self.assertIsNotNone(parsed)
        self.assertGreater(parsed, 4)
        self.assertLessEqual(parsed, 8)

        triage = self._retry_triage()
        calls = []
        sleeps = []
        # Snapshot the clock the same way the parser does: a date 2s ahead
        # must produce a positive wait well under the cap.
        soon = email.utils.formatdate(__import__("time").time() + 2,
                                       usegmt=True)

        def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                raise self._http_error(
                    url, 429, "slow down", self._retry_after_headers(soon))
            return 200, "text/html", self.HTML.encode()

        result = fetch_descriptions(
            triage, fetch=fetch, sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 0)
        self.assertLessEqual(sleeps[0], 3)

    def test_retry_after_over_cap_skips_the_retry(self):
        # A server asking for 16 minutes is not asking us to come back in 30s:
        # respect it by not retrying at all (`Retry-After: 120` likewise).
        from network_jobs.descriptions import (
            fetch_descriptions, fetch_job_description)

        for value in ("120", "999"):
            triage = self._retry_triage()
            sleeps = []
            calls = []

            def fetch(url, value=value):
                calls.append(url)
                raise self._http_error(
                    url, 429, "slow down",
                    self._retry_after_headers(value))

            direct = fetch_job_description("https://x/jobs/1", fetch=fetch)
            self.assertEqual(direct, {
                "error": "http-429", "retryAfter": 30.0,
                "retryAfterTooLong": True}, value)

            result = fetch_descriptions(
                triage, fetch=fetch, sleep=lambda s: sleeps.append(s))
            self.assertEqual(result["failed"], 1, value)
            self.assertEqual(result["errors"],
                             ["https://x/jobs/1: http-429"], value)
            self.assertEqual(len(calls), 2, value)  # one direct + one run
            self.assertEqual(sleeps, [], value)

    def test_host_pacing_after_429(self):
        # #64 review: after a 429, the next job on that host waits host_delay
        # — distinct from the retry backoff, and applied even though the
        # retried request eventually succeeded.
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage(2)
        attempts = {}
        calls = []
        sleeps = []

        def fetch(url):
            calls.append(url)
            attempts[url] = attempts.get(url, 0) + 1
            if url.endswith("/jobs/1") and attempts[url] == 1:
                raise self._http_error(url, 429, "slow down")
            return 200, "text/html", self.HTML.encode()

        result = fetch_descriptions(
            triage, fetch=fetch, host_delay=0.25,
            sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(calls, ["https://x/jobs/1", "https://x/jobs/1",
                                 "https://x/jobs/2"])
        # 1.0 is the retry backoff for job 1; 0.25 is the host pause before
        # job 2. The pacing sleep sits between the two jobs, not in a retry.
        self.assertEqual(sleeps, [1.0, 0.25])

    def test_bot_wall_signature_detection(self):
        # #67 + re-review: only the CDN-shaped 403 is a wall. `Server:
        # CloudFront` alone is too broad (geo-block/private object), so it
        # needs the CloudFront error cache; SigSci's request id is enough.
        from network_jobs.descriptions import fetch_job_description

        sigsci = Message()
        sigsci["x-sigsci-request-id"] = "abc123"
        cf_error = Message()
        cf_error["Server"] = "cloudfront"
        cf_error["x-cache"] = "Error from cloudfront"

        for headers in (sigsci, cf_error, self._bot_wall_headers()):
            def fetch(url, headers=headers):
                raise self._http_error(url, 403, "blocked", headers)

            self.assertEqual(
                fetch_job_description("https://x/jobs/1", fetch=fetch),
                {"error": "http-403", "botWall": True})

        cf_only = Message()
        cf_only["Server"] = "CloudFront"
        cf_ok_cache = Message()
        cf_ok_cache["Server"] = "CloudFront"
        cf_ok_cache["x-cache"] = "Hit from cloudfront"

        for headers in (cf_only, cf_ok_cache):
            def fetch(url, headers=headers):
                raise self._http_error(url, 403, "forbidden", headers)

            self.assertEqual(
                fetch_job_description("https://x/jobs/1", fetch=fetch),
                {"error": "http-403"}, headers.items())

        def bare(url):
            raise self._http_error(url, 403, "forbidden")

        self.assertEqual(
            fetch_job_description("https://x/jobs/1", fetch=bare),
            {"error": "http-403"})

    def test_bare_403_stays_single_shot(self):
        # The local bare-403 contract from the #65 review must not weaken.
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage()
        calls = []
        sleeps = []

        def fetch(url):
            calls.append(url)
            raise self._http_error(url, 403, "forbidden")

        result = fetch_descriptions(
            triage, fetch=fetch, sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["errors"], ["https://x/jobs/1: http-403"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])
        self.assertNotIn("botWalled", result)

    def test_bot_wall_honors_retry_after(self):
        # A 403 that does carry Retry-After uses it, not the 30s default.
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage()
        calls = []
        sleeps = []

        def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                raise self._http_error(
                    url, 403, "blocked",
                    self._bot_wall_headers(retry_after="7"))
            return 200, "text/html", self.HTML.encode()

        result = fetch_descriptions(
            triage, fetch=fetch, sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(sleeps, [7.0])

    def test_bot_wall_403_retries_through_real_urlopen(self):
        # #67: a CloudFront 403 from the real urlopen is retried after the
        # long cooldown, and the retried body is written. The next job on
        # that host is paced even though the retry succeeded.
        import http.server
        import threading
        from network_jobs.descriptions import fetch_descriptions

        hits = []
        html = self.HTML.encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = "CloudFront"
            sys_version = ""

            def do_GET(self):
                hits.append(self.path)
                if len(hits) == 1:
                    self.send_response(403)
                    self.send_header("x-sigsci-request-id", "req-1")
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html>blocked</html>")
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        triage = self._triage_for([f"{base}/jobs/1", f"{base}/jobs/2"])
        sleeps = []

        result = fetch_descriptions(
            triage, host_delay=0.25, sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertNotIn("botWalled", result)
        self.assertEqual(hits, ["/jobs/1", "/jobs/1", "/jobs/2"])
        # 30s is the first bot-wall cooldown; 0.25 is the host pause that
        # follows the wall even though the retried request succeeded.
        self.assertEqual(sleeps, [30.0, 0.25])
        rows = json.loads((triage / "index" / "matches.json").read_text())
        self.assertIn("payments infrastructure",
                      rows["jobs"][0]["description"])
        self.assertIn("payments infrastructure",
                      rows["jobs"][1]["description"])

    def test_sustained_bot_wall_blocks_the_host(self):
        # #67: a wall that survives both cooldowns must not cost 39 x 90s.
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage(3)
        calls = []
        sleeps = []

        def fetch(url):
            calls.append(url)
            raise self._http_error(
                url, 403, "blocked", self._bot_wall_headers())

        result = fetch_descriptions(
            triage, fetch=fetch, host_delay=0.25,
            sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 0)
        self.assertEqual(result["failed"], 3)
        self.assertTrue(result["botWalled"])
        self.assertNotIn("retriesDisabled", result)
        # job 1: original + two cooldown retries; jobs 2-3: one attempt each,
        # each behind the host pause.
        self.assertEqual(len(calls), 5)
        self.assertEqual(sleeps, [30.0, 60.0, 0.25, 0.25])
        self.assertEqual(set(result), {"fetched", "skipped", "failed",
                                       "errors", "out", "botWalled"})

    def test_all_failing_board_disables_retries(self):
        # #64 review: without a run-level budget an all-failing board makes
        # 3x the requests and sleeps per retry. The soft trip fires after
        # max_consecutive_failures jobs and the rest are single-shot.
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage(10)
        calls = []
        sleeps = []

        def fetch(url):
            calls.append(url)
            return 503, "text/html", b"unavailable"

        result = fetch_descriptions(
            triage, fetch=fetch, sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 0)
        self.assertEqual(result["failed"], 10)
        self.assertTrue(result["retriesDisabled"])
        self.assertEqual(set(result), {"fetched", "skipped", "failed",
                                       "errors", "out", "retriesDisabled"})
        # Five failing jobs pay the full 3 attempts, the last five get one.
        self.assertEqual(len(calls), 20)
        self.assertEqual(sum(sleeps), 15.0)
        written = json.loads((triage / "index" / "matches.json").read_text())
        self.assertNotIn("description", written["jobs"][0])

    def test_all_failing_board_stays_bounded_at_scale(self):
        # The down-board guarantee at the size that started this: 222 rows
        # must not become 666 requests and ~11 minutes of sleep.
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage(222)
        calls = []
        sleeps = []

        def fetch(url):
            calls.append(url)
            return 503, "text/html", b"unavailable"

        result = fetch_descriptions(
            triage, fetch=fetch, sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["failed"], 222)
        self.assertTrue(result["retriesDisabled"])
        self.assertEqual(len(calls), 5 * 3 + (222 - 5))  # 232, not 666
        self.assertEqual(sum(sleeps), 15.0)  # not ~666s

    def test_recovered_retries_do_not_consume_the_budget(self):
        # #64 re-review: a first-request 429 whose retry succeeds must not
        # starve later jobs. 222 rows, one successful retry each.
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage(222)
        attempts = {}
        calls = []

        def fetch(url):
            calls.append(url)
            attempts[url] = attempts.get(url, 0) + 1
            if attempts[url] == 1:
                raise self._http_error(url, 429, "slow down")
            return 200, "text/html", self.HTML.encode()

        result = fetch_descriptions(
            triage, fetch=fetch, sleep=lambda s: None)

        self.assertEqual(result["fetched"], 222)
        self.assertEqual(result["failed"], 0)
        self.assertNotIn("retriesDisabled", result)
        self.assertEqual(len(calls), 444)
        rows = json.loads((triage / "index" / "matches.json").read_text())
        self.assertTrue(all("description" in row for row in rows["jobs"]))

    def test_failing_jobs_do_not_disarm_other_hosts(self):
        # #64 re-review: two jobs that exhaust their retries are two failing
        # jobs, not five failing attempts. Twenty later jobs that each need
        # one successful retry must still fetch (was 0/20).
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage(22)
        attempts = {}
        calls = []
        sleeps = []

        def fetch(url):
            calls.append(url)
            attempts[url] = attempts.get(url, 0) + 1
            if url.endswith(("/jobs/1", "/jobs/2")):
                return 503, "text/html", b"unavailable"
            if attempts[url] == 1:
                raise self._http_error(url, 429, "slow down")
            return 200, "text/html", self.HTML.encode()

        result = fetch_descriptions(
            triage, fetch=fetch, sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 20)
        self.assertEqual(result["failed"], 2)
        self.assertNotIn("retriesDisabled", result)
        self.assertEqual(len(calls), 2 * 3 + 20 * 2)

    def test_soft_trip_reenables_after_a_success(self):
        # The consecutive trip is soft: a job that succeeds on its first
        # attempt clears it, so a host that recovers gets retries again.
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage(4)
        attempts = {}
        calls = []
        sleeps = []

        def fetch(url):
            calls.append(url)
            attempts[url] = attempts.get(url, 0) + 1
            if url.endswith(("/jobs/1", "/jobs/2")):
                return 503, "text/html", b"unavailable"
            if url.endswith("/jobs/3"):
                return 200, "text/html", self.HTML.encode()
            if attempts[url] == 1:
                raise self._http_error(url, 429, "slow down")
            return 200, "text/html", self.HTML.encode()

        result = fetch_descriptions(
            triage, fetch=fetch, max_consecutive_failures=2,
            sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 2)
        self.assertEqual(result["failed"], 2)
        self.assertNotIn("retriesDisabled", result)  # cleared at the end
        # jobs 1-2: 3 attempts each; job 3: one; job 4: retry is back on.
        self.assertEqual(len(calls), 3 + 3 + 1 + 2)

    def test_recurring_bot_wall_blocks_after_one_recovery(self):
        # #64 re-review: a wall that clears on the 30s retry and returns on
        # the next job must not cost 30s per job until the budget runs out.
        from network_jobs.descriptions import fetch_descriptions

        triage = self._retry_triage(3)
        attempts = {}
        calls = []
        sleeps = []

        def fetch(url):
            calls.append(url)
            attempts[url] = attempts.get(url, 0) + 1
            if url.endswith("/jobs/1") and attempts[url] == 1:
                raise self._http_error(
                    url, 403, "blocked", self._bot_wall_headers())
            if url.endswith("/jobs/2"):
                raise self._http_error(
                    url, 403, "blocked", self._bot_wall_headers())
            return 200, "text/html", self.HTML.encode()

        result = fetch_descriptions(
            triage, fetch=fetch, host_delay=0.25,
            sleep=lambda s: sleeps.append(s))

        self.assertEqual(result["fetched"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertTrue(result["botWalled"])
        # job 1: wall + one 30s cooldown; job 2: single attempt, no
        # cooldown; job 3: single attempt, success.
        self.assertEqual(len(calls), 2 + 1 + 1)
        self.assertEqual(sleeps, [30.0, 0.25, 0.25])
        self.assertNotIn("retriesDisabled", result)

    def test_url_errors_are_not_retried(self):
        # #64 review: DNS/TLS failures do not clear by retrying. urlopen wraps
        # them in URLError, so the reason's class is what gets classified.
        import socket
        from urllib.error import URLError
        from network_jobs.descriptions import fetch_descriptions

        for exc in (
            URLError(socket.gaierror(-2, "Name or service not known")),
            URLError("dns lookup failed"),
        ):
            triage = self._retry_triage()
            calls = []
            sleeps = []

            def fetch(url, exc=exc):
                calls.append(url)
                raise exc

            result = fetch_descriptions(
                triage, fetch=fetch, sleep=lambda s: sleeps.append(s))

            self.assertEqual(result["failed"], 1)
            self.assertEqual(len(calls), 1, exc)
            self.assertEqual(sleeps, [], exc)

    def test_retryable_classification(self):
        from network_jobs.descriptions import _is_retryable

        transient = (
            "http-429", "http-500", "http-503",
            "fetch-failed: TimeoutError", "fetch-failed: ConnectionError",
            "fetch-failed: ConnectionResetError",
            "fetch-failed: ConnectionRefusedError",
            "fetch-failed: RemoteDisconnected",
        )
        permanent = (
            "http-403", "http-404", "http-408", "http-410", "http-500x",
            "non-http-url", "too-large", "no-usable-body", "",
            "fetch-failed: URLError", "fetch-failed: SSLError",
            "fetch-failed: ValueError", "fetch-failed: InvalidURL",
            "fetch-failed: gaierror",
        )
        for error in transient:
            self.assertTrue(_is_retryable(error), error)
        for error in permanent:
            self.assertFalse(_is_retryable(error), error)

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
        self.assertIsInstance(rows, dict)
        rows = rows["jobs"]
        self.assertIn("payments infrastructure", rows[0]["description"])
        self.assertNotIn("description", rows[1])

    def test_fetch_descriptions_keeps_matches_object_shape(self):
        # #32: match-prefs writes the SCHEMA object; fetch-descriptions must
        # preserve the wrapper and every summary key it did not compute.
        from network_jobs.descriptions import fetch_descriptions

        tmp = Path(tempfile.mkdtemp(prefix="nj-desc-shape-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        index = tmp / "triage" / "t" / "index"
        index.mkdir(parents=True)
        envelope = {
            "nListings": 40,
            "nMatches": 2,
            "showing": "2 of 40 match prefs",
            "departments": {"Product": 3, "Engineering": 37},
            "matchDepartments": {"Product": 2},
            "ingestDefault": "matches",
            "jobs": [
                {"title": "Senior PM", "url": "https://x/jobs/1",
                 "matchScore": 9},
                {"title": "No URL Job", "matchScore": 4},
            ],
        }
        (index / "matches.json").write_text(json.dumps(envelope) + "\n")

        result = fetch_descriptions(
            tmp / "triage" / "t",
            fetch=self._fetch({"https://x/jobs/1": (200, "text/html", self.HTML)}))

        self.assertEqual(result["fetched"], 1)
        written = json.loads((index / "matches.json").read_text())
        self.assertIsInstance(written, dict)
        for key in ("nListings", "nMatches", "showing", "departments",
                    "matchDepartments", "ingestDefault"):
            self.assertEqual(written[key], envelope[key], key)
        self.assertEqual([j["title"] for j in written["jobs"]],
                         ["Senior PM", "No URL Job"])
        self.assertEqual(written["jobs"][0]["matchScore"], 9)
        self.assertIn("payments infrastructure",
                      written["jobs"][0]["description"])
        self.assertTrue(written["jobs"][0]["descriptionFetchedAt"])
        self.assertEqual(written["jobs"][0]["descriptionSource"],
                         "https://x/jobs/1")
        self.assertNotIn("description", written["jobs"][1])

    def test_fetch_descriptions_upgrades_legacy_array(self):
        # #32: triage dirs written by the old fetch-descriptions hold a bare
        # array. Reading one must work and writing it back must yield the
        # documented object, with the rows untouched.
        from network_jobs.descriptions import fetch_descriptions

        tmp = Path(tempfile.mkdtemp(prefix="nj-desc-legacy-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        index = tmp / "triage" / "t" / "index"
        index.mkdir(parents=True)
        legacy = [
            {"title": "Senior PM", "url": "https://x/jobs/1"},
            {"title": "No URL Job"},
        ]
        (index / "matches.json").write_text(json.dumps(legacy) + "\n")

        result = fetch_descriptions(
            tmp / "triage" / "t",
            fetch=self._fetch({"https://x/jobs/1": (200, "text/html", self.HTML)}))

        self.assertEqual(result["fetched"], 1)
        written = json.loads((index / "matches.json").read_text())
        self.assertIsInstance(written, dict)
        self.assertEqual(written["nMatches"], 2)
        self.assertEqual(written["ingestDefault"], "matches")
        self.assertEqual([j["title"] for j in written["jobs"]],
                         ["Senior PM", "No URL Job"])
        self.assertIn("payments infrastructure",
                      written["jobs"][0]["description"])

    def test_load_matches_normalizes_both_shapes(self):
        # #32: one loader for the shared artifact so no caller needs a
        # type conditional or ad-hoc ["jobs"] indexing.
        from network_jobs.matches import MatchesFormatError, load_matches

        tmp = Path(tempfile.mkdtemp(prefix="nj-matches-load-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        rows = [{"title": "Senior PM", "externalId": "1"},
                {"title": "Staff PM", "externalId": "2"}]

        legacy_path = tmp / "legacy.json"
        legacy_path.write_text(json.dumps(rows) + "\n")
        object_path = tmp / "object.json"
        object_path.write_text(json.dumps({
            "jobs": rows,
            "nMatches": 2,
            "ingestDefault": "matches",
        }) + "\n")

        legacy = load_matches(legacy_path)
        current = load_matches(object_path)
        self.assertIsInstance(legacy, dict)
        self.assertEqual(legacy, current)
        self.assertEqual(legacy["jobs"], rows)

        broken = tmp / "broken.json"
        broken.write_text(json.dumps({"results": rows}) + "\n")
        with self.assertRaises(MatchesFormatError):
            load_matches(broken)

    def test_review_matches_veto_keeps_descriptions(self):
        # #32/#24: --veto re-scores from listings.json. It must carry the
        # bodies across first, or the rewrite silently empties them.
        import contextlib
        import io
        from network_jobs.descriptions import fetch_descriptions

        tmp = Path(tempfile.mkdtemp(prefix="nj-review-desc-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        triage = tmp / "triage" / "careers-example-2026-09-16"
        (triage / "index").mkdir(parents=True)
        shutil.copy(FIXTURES / "listings-prefs.json",
                    triage / "index" / "listings.json")
        (triage / "INVENTORY.md").write_text(
            "# Careers triage — Example\n\n- **Slug:** example\n")
        data = tmp / "data"
        (data / "resume").mkdir(parents=True)
        shutil.copy(FIXTURES / "preferences.json", data / "preferences.json")
        shutil.copy(FIXTURES / "resume-keywords.md",
                    data / "resume" / "text.md")

        with contextlib.redirect_stdout(io.StringIO()):
            rc = helper_main(["match-prefs", "--triage-dir", str(triage),
                              "--data", str(data), "--company", "Example"])
        self.assertEqual(rc, 0)
        fetched = fetch_descriptions(
            triage, fetch=lambda url: (200, "text/html", self.HTML.encode()))
        self.assertGreater(fetched["fetched"], 0)

        # A veto that is already satisfied leaves the shortlist intact, so
        # there are rows whose bodies must survive the rewrite.
        with contextlib.redirect_stdout(io.StringIO()):
            rc = helper_main(["review-matches", "--triage-dir", str(triage),
                              "--data", str(data), "--company", "Example",
                              "--veto", "crypto trading", "--json"])
        self.assertEqual(rc, 0)

        written = json.loads((triage / "index" / "matches.json").read_text())
        self.assertIsInstance(written, dict)
        self.assertEqual(written["nMatches"], fetched["fetched"])
        described = [j for j in written["jobs"] if j.get("description")]
        self.assertEqual(len(described), fetched["fetched"])
        for job in described:
            self.assertIn("payments infrastructure", job["description"])
            self.assertTrue(job.get("descriptionSource"))

    def test_descriptions_survive_second_pipeline_pass(self):
        # #32 / #24: the documented order is match-prefs -> fetch-descriptions,
        # and users re-run it. A second pass must keep the object shape, keep
        # bodies byte-identical, and restore (not re-fetch) them.
        import contextlib
        import io
        from network_jobs.descriptions import fetch_descriptions

        tmp = Path(tempfile.mkdtemp(prefix="nj-desc-twice-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        triage = tmp / "triage" / "careers-example-2026-09-16"
        (triage / "index").mkdir(parents=True)
        shutil.copy(FIXTURES / "listings-prefs.json",
                    triage / "index" / "listings.json")
        (triage / "INVENTORY.md").write_text(
            "# Careers triage — Example\n\n- **Slug:** example\n")
        data = tmp / "data"
        (data / "resume").mkdir(parents=True)
        shutil.copy(FIXTURES / "preferences.json", data / "preferences.json")
        shutil.copy(FIXTURES / "resume-keywords.md",
                    data / "resume" / "text.md")

        def run_match_prefs():
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = helper_main([
                    "match-prefs", "--triage-dir", str(triage),
                    "--data", str(data), "--company", "Example",
                ])
            self.assertEqual(rc, 0)
            return json.loads(stdout.getvalue())

        always_html = lambda url: (200, "text/html", self.HTML.encode())  # noqa: E731

        first = run_match_prefs()
        n_matches = first["matches"]
        self.assertGreater(n_matches, 0)

        fetched = fetch_descriptions(triage, fetch=always_html)
        self.assertEqual(fetched["fetched"], n_matches)
        self.assertEqual(fetched["failed"], 0)

        after_first = json.loads((triage / "index" / "matches.json").read_text())
        self.assertIsInstance(after_first, dict)
        described = {j["title"]: j for j in after_first["jobs"]
                     if j.get("description")}
        self.assertEqual(len(described), n_matches)
        bodies = {t: j["description"] for t, j in described.items()}
        sources = {t: j["descriptionSource"] for t, j in described.items()}

        # Re-score: bodies must be carried forward from matches.json (#24),
        # not lost with the regenerated shortlist.
        second = run_match_prefs()
        self.assertEqual(second["matches"], n_matches)
        self.assertEqual(second["restoredDescriptions"], n_matches)

        after_second = json.loads((triage / "index" / "matches.json").read_text())
        self.assertIsInstance(after_second, dict)
        still = {j["title"]: j for j in after_second["jobs"]
                 if j.get("description")}
        self.assertEqual({t: j["description"] for t, j in still.items()}, bodies)
        self.assertEqual({t: j["descriptionSource"] for t, j in still.items()},
                         sources)
        for job in still.values():
            self.assertTrue(job.get("descriptionFetchedAt"))

        # Second fetch pass: nothing left to fetch, nothing rewritten to loss.
        again = fetch_descriptions(triage, fetch=always_html)
        self.assertEqual(again["fetched"], 0)
        self.assertEqual(again["failed"], 0)

        final = json.loads((triage / "index" / "matches.json").read_text())
        self.assertIsInstance(final, dict)
        self.assertEqual({j["title"]: j["description"] for j in final["jobs"]
                          if j.get("description")}, bodies)

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


class CategoryConfidenceTests(unittest.TestCase):
    """#40: a low-confidence category guess must not score as confirmed.

    The exact-category branch used to award +5 without consulting
    `categoryConfidence`, while the affinity branch two lines below required
    `high`. Both now go through `category_confirmed`.
    """

    def test_high_confidence_exact_match_keeps_bonus(self):
        job = {"title": "Senior Product Manager", "company": "Acme",
               "location": "Remote"}
        scored = score_job(job, {"categories": ["product"]})
        self.assertEqual(scored["category"], "product")
        self.assertEqual(scored["categoryConfidence"], "high")
        self.assertIn("category", scored["matchReasons"])
        self.assertNotIn("category-unconfirmed", scored["matchReasons"])
        # The +5 is the only difference from scoring without category prefs.
        baseline = score_job(job, {})
        self.assertEqual(scored["matchScore"] - baseline["matchScore"], 5)

    def test_low_confidence_exact_match_gets_no_bonus(self):
        # "Data Steward" lands in `data` on the weak "data" token, i.e.
        # exactly the low-confidence guess #40 is about. Deliberately not a
        # "product" title: #37 removes the weak `product` token, so a
        # "Product Analyst" fixture would classify `other`/low once both
        # branches merge. This one stays `data`/low either way.
        job = {"title": "Data Steward", "company": "Acme",
               "location": "Remote"}
        scored = score_job(job, {"categories": ["data"]})
        self.assertEqual(scored["category"], "data")
        self.assertEqual(scored["categoryConfidence"], "low")
        self.assertNotIn("category", scored["matchReasons"])
        self.assertIn("category-unconfirmed", scored["matchReasons"])
        # No bonus: same score as the same row with no category preference.
        self.assertEqual(scored["matchScore"], score_job(job, {})["matchScore"])
        # Still shortlisted — unconfirmed is a ranking signal, not a veto.
        self.assertTrue(scored["matched"])

    def test_missing_confidence_counts_as_high(self):
        from network_jobs.prefs import category_confirmed

        self.assertTrue(category_confirmed({"category": "product"}))
        self.assertTrue(category_confirmed({"categoryConfidence": None}))
        self.assertTrue(category_confirmed({"categoryConfidence": ""}))
        self.assertTrue(category_confirmed({"categoryConfidence": "high"}))
        self.assertFalse(category_confirmed({"categoryConfidence": "low"}))

        # And through the scorer: a row with no field still earns +5.
        from unittest import mock

        legacy = {"title": "Data Steward", "company": "Acme",
                  "location": "Remote"}
        classified = {
            "title": legacy["title"], "department": "", "company": "Acme",
            "category": "data",  # no categoryConfidence key at all
            "track": "ic", "seniority": "mid", "senioritySignals": [],
            "locations": [], "locationBuckets": ["remote"],
            "locationBucket": "remote",
        }
        with mock.patch("network_jobs.prefs.classify_job",
                        side_effect=lambda job, company=None: dict(classified)):
            scored = score_job(legacy, {"categories": ["data"]})
        self.assertIn("category", scored["matchReasons"])
        self.assertNotIn("category-unconfirmed", scored["matchReasons"])

    def test_low_confidence_ranks_below_high_confidence(self):
        # Identical rows apart from categoryConfidence: pin the classification
        # so the confidence value is the only variable.
        from unittest import mock

        def fake_classify(job, company=None):
            return {
                "title": job["title"], "department": "", "company": "Acme",
                "category": "product",
                "categoryConfidence": job["confidence"],
                "track": "ic", "seniority": "mid", "senioritySignals": [],
                "locations": [], "locationBuckets": ["remote"],
                "locationBucket": "remote", "url": job.get("url"),
            }

        rows = [
            {"title": "A Low", "confidence": "low", "url": "https://x/1"},
            {"title": "B High", "confidence": "high", "url": "https://x/2"},
        ]
        with mock.patch("network_jobs.prefs.classify_job",
                        side_effect=fake_classify):
            result = match_listings(rows, {"categories": ["product"]},
                                    company="Acme")
        self.assertEqual([j["title"] for j in result["jobs"]],
                         ["B High", "A Low"])
        self.assertGreater(result["jobs"][0]["matchScore"],
                           result["jobs"][1]["matchScore"])
        self.assertEqual(result["reasonCounts"].get("category-unconfirmed"), 1)


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

    def test_cap_reports_withheld_count(self):
        # #50: a shortlist that stops at the cap must say the cap did it, not
        # leave "3 of 5" reading as "only 3 match".
        result = rank_corpus(data_dir=self.tmp, k=10, today="2026-09-20",
                             prefs=self.prefs, company_cap=3)
        self.assertEqual(result["companyCap"], {"limit": 3, "skipped": 2})
        self.assertEqual(result["showing"], "3 of 5")

    def test_cap_absent_when_shortlist_fills(self):
        # The cap is not what limited the list, so stay quiet.
        jobs = json.loads((self.tmp / "corpus" / "jobs-all.json").read_text())
        for i in range(3):
            jobs.append({
                "title": f"Backend Engineer B{i}",
                "company": "Beta LLC",
                "department": "Engineering",
                "location": "Remote",
                "lastSeen": "2026-09-19",
                "url": f"https://beta.example/jobs/{i}",
            })
        (self.tmp / "corpus" / "jobs-all.json").write_text(json.dumps(jobs) + "\n")
        result = rank_corpus(data_dir=self.tmp, k=5, today="2026-09-20",
                             prefs=self.prefs, company_cap=3)
        self.assertEqual(len(result["jobs"]), 5)
        self.assertNotIn("companyCap", result)

    def test_search_prints_cap_note(self):
        from contextlib import redirect_stdout
        from io import StringIO

        (self.tmp / "preferences.json").write_text(json.dumps(self.prefs) + "\n")
        out = StringIO()
        with redirect_stdout(out):
            rc = helper_main(["search", "--data", str(self.tmp),
                              "--query", "engineer", "-k", "10"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("3 of 5 match prefs", text)
        self.assertIn("2 more held back by the 3-per-company cap", text)


class EmptySearchHintTests(unittest.TestCase):
    """#54: an empty shortlist must not print a bare "0 of 0" dead end."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nj-empty-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.tmp.joinpath("corpus").mkdir()
        self.tmp.joinpath("preferences.json").write_text(
            json.dumps({"categories": ["product"]}) + "\n")

    def _search(self, *argv):
        out = StringIO()
        with redirect_stdout(out):
            rc = helper_main(["search", "--data", str(self.tmp), *argv])
        self.assertEqual(rc, 0)
        return out.getvalue()

    def test_empty_corpus_points_at_discovery_and_ingest(self):
        text = self._search()
        self.assertIn("0 of 0 match prefs", text)
        self.assertIn("corpus is empty", text)
        self.assertIn("find open roles", text)
        self.assertIn("ingest", text)

    def test_query_with_no_matches_names_query_and_prefs(self):
        (self.tmp / "preferences.json").write_text(
            json.dumps({"dealBreakers": ["defense"]}) + "\n")
        (self.tmp / "corpus" / "jobs-all.json").write_text(json.dumps([{
            "title": "Product Manager",
            "company": "Defense Systems",
            "department": "Product",
            "location": "Remote",
            "lastSeen": "2026-10-20",
            "url": "https://example.com/1",
        }]) + "\n")
        text = self._search("--query", "product manager")
        self.assertIn("0 of 0 match prefs for 'product manager'", text)
        self.assertIn("no postings match 'product manager' in the current corpus",
                      text)
        self.assertIn("prefs filtering also applies", text)

    def test_json_output_stays_pure(self):
        text = self._search("--json")
        payload = json.loads(text)
        self.assertEqual(payload["showing"], "0 of 0")
        self.assertEqual(payload["jobs"], [])
        self.assertNotIn("corpus is empty", text)


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

    def test_staged_listing_keeps_extras(self):
        # A genuinely schema-shaped row may pass through verbatim, but the
        # ATS extras that came with it must survive (they used to, by accident
        # of the passthrough; the mapping path must not become lossy).
        from network_jobs.pagination import normalize_listing

        staged = {
            "title": "Eng", "url": "https://x/1", "location": "Remote",
            "externalId": "42", "compensation": {"min": 100000},
            "workplaceType": "Hybrid", "isRemote": True,
        }
        job = normalize_listing(staged, "https://src")
        assert job is not None
        self.assertEqual(job["url"], "https://x/1")
        self.assertEqual(job["sourceUrl"], "https://src")
        self.assertEqual(job["compensation"], {"min": 100000})
        self.assertEqual(job["workplaceType"], "Hybrid")
        self.assertIs(job["isRemote"], True)

    def test_ashby_job_url_normalized(self):
        # Ashby sends the link under jobUrl with a plain string location, so
        # the old passthrough guard returned the raw row and every row lost
        # its canonical url (0 of 812 in the reported run).
        from network_jobs.pagination import normalize_listing

        raw = {
            "id": "0f2a6d10-1c4e-4f0c-9d0b-8a7f6e5d4c3b",
            "title": "Member of Technical Staff",
            "location": "San Francisco, CA",
            "jobUrl": "https://jobs.ashbyhq.com/openai/0f2a6d10",
            "applyUrl": "https://jobs.ashbyhq.com/openai/0f2a6d10/apply",
            "department": "Research",
            "team": "Alignment",
            "workplaceType": "Hybrid",
            "compensation": {"compensationTierSummary": "$200k – $400k"},
            "descriptionHtml": "<p>Build things.</p>",
        }
        job = normalize_listing(raw, "https://api.ashbyhq.com/posting-api/job-board/openai")
        assert job is not None
        self.assertEqual(job["url"], "https://jobs.ashbyhq.com/openai/0f2a6d10")
        self.assertEqual(job["title"], "Member of Technical Staff")
        self.assertEqual(job["location"], "San Francisco, CA")
        self.assertEqual(job["externalId"], "0f2a6d10-1c4e-4f0c-9d0b-8a7f6e5d4c3b")
        # department beats team; the raw url aliases do not leak through.
        self.assertEqual(job["department"], "Research")
        self.assertNotIn("jobUrl", job)
        self.assertNotIn("applyUrl", job)
        # Extras ride along instead of being dropped by the mapping path.
        self.assertEqual(job["compensation"], {"compensationTierSummary": "$200k – $400k"})
        self.assertEqual(job["workplaceType"], "Hybrid")
        # ...but raw JD bodies do not: they are reserved for fetch-descriptions,
        # which writes a truncated plain-text description with provenance.
        self.assertNotIn("descriptionHtml", job)

    def test_raw_jd_bodies_not_carried_as_extras(self):
        # Lever ships the whole posting inline: `description` (HTML) plus
        # `descriptionPlain` and `lists`. Carried onto the listing it would be
        # untruncated HTML that fetch-descriptions skips (truthy `description`)
        # and that mustHave/veto matching runs over. Same for Greenhouse's
        # `content` with ?content=true.
        from network_jobs.pagination import normalize_listing

        lever = {
            "id": "8f0c1e2a-1111-2222-3333-444455556666",
            "text": "Senior Product Manager",
            "hostedUrl": "https://jobs.lever.co/acme/8f0c1e2a",
            "categories": {"location": "New York, NY", "team": "Product"},
            "description": "<div><p>Own the roadmap.</p><p>EEO boilerplate…</p></div>",
            "descriptionPlain": "Own the roadmap. EEO boilerplate…",
            "lists": [{"text": "Requirements", "content": "<li>5+ years</li>"}],
            "workplaceType": "hybrid",
        }
        job = normalize_listing(lever, "https://api.lever.co/v0/postings/acme")
        assert job is not None
        self.assertEqual(job["url"], "https://jobs.lever.co/acme/8f0c1e2a")
        self.assertEqual(job["title"], "Senior Product Manager")
        self.assertEqual(job["externalId"], "8f0c1e2a-1111-2222-3333-444455556666")
        self.assertEqual(job["workplaceType"], "hybrid")
        for key in ("description", "content", "descriptionHtml",
                    "descriptionPlain", "lists"):
            self.assertNotIn(key, job)
        # fetch-descriptions skips only rows whose description is truthy, so
        # this row must stay falsy and get the clean truncated fetch.
        self.assertFalse(job.get("description"))

        greenhouse = {
            "id": 8172487,
            "title": "Product Manager",
            "location": {"name": "San Francisco"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/8172487",
            "content": "&lt;div&gt;" + "escaped HTML " * 500 + "&lt;/div&gt;",
        }
        gh = normalize_listing(
            greenhouse, "https://boards-api.greenhouse.io/v1/boards/acme/jobs")
        assert gh is not None
        self.assertNotIn("content", gh)
        self.assertFalse(gh.get("description"))

    def test_amazon_url_next_step_normalized(self):
        # Amazon search.json puts the link under url_next_step and the
        # department under job_category (with team as a narrower secondary).
        from network_jobs.pagination import extract_job_list, normalize_listing

        raw = {
            "id": "2829431",
            "title": "Software Development Engineer II",
            "location": "Seattle, WA",
            "url_next_step": "https://www.amazon.jobs/en/jobs/2829431",
            "job_category": "Software Development",
            "team": "Amazon Web Services",
            "business_category": "Amazon Web Services",
        }
        self.assertEqual(extract_job_list({"jobs": [raw]}), [raw])
        job = normalize_listing(raw, "https://www.amazon.jobs/en/search.json")
        assert job is not None
        self.assertEqual(job["url"], "https://www.amazon.jobs/en/jobs/2829431")
        self.assertEqual(job["externalId"], "2829431")
        self.assertEqual(job["department"], "Software Development")
        self.assertNotIn("url_next_step", job)

    def test_amazon_job_path_preferred_over_apply_url(self):
        # #48: Amazon search.json also emits `job_path` (the canonical page)
        # alongside `url_next_step` (the apply endpoint, which 302s). Keying on
        # the apply link made every JD fetch fail, so those rows ingested
        # title-only. job_path must win, and be absolutized against the crawl.
        from network_jobs.pagination import normalize_listing

        raw = {
            "id": "10586855",
            "title": "Area Manager II",
            "location": "Salinas, California, USA",
            "url": None,
            "url_next_step": "https://account.amazon.jobs/jobs/10586855/apply",
            "job_path": "/en/jobs/10586855/area-manager-ii-salinas-ca",
        }
        job = normalize_listing(raw, "https://www.amazon.jobs/en/search.json?country=USA")
        assert job is not None
        self.assertEqual(
            job["url"], "https://www.amazon.jobs/en/jobs/10586855/area-manager-ii-salinas-ca")
        self.assertEqual(job["externalId"], "10586855")
        self.assertNotIn("job_path", job)
        self.assertNotIn("url_next_step", job)

    def test_relative_position_url_anchors_at_host_root(self):
        # Eightfold's positionUrl is normally root-relative. A slashless
        # variant must not resolve under /api/pcsx/ on the source URL.
        from network_jobs.pagination import normalize_listing

        raw = {
            "id": 893398340997,
            "name": "Senior Software QA Engineer",
            "location": "Bengaluru, India",
            "positionUrl": "careers/job/893398340997",
        }
        job = normalize_listing(
            raw, "https://jobs.example.com/api/pcsx/search?domain=example.com&start=0")
        assert job is not None
        self.assertEqual(
            job["url"], "https://jobs.example.com/careers/job/893398340997")

    def test_requisition_id_does_not_beat_posting_id(self):
        # #48: Greenhouse reuses one requisition_id across distinct postings.
        # The per-posting `id` must be the staged externalId so rows don't
        # collapse in the fingerprint dedupe.
        from network_jobs.pagination import normalize_listing

        raw = {
            "id": 5211305008,
            "requisition_id": "10225",
            "title": "Staff Software Engineer, Kubernetes Platform",
            "absolute_url": "https://job-boards.greenhouse.io/anthropic/jobs/5211305008",
            "location": {"name": "London, UK"},
        }
        job = normalize_listing(raw, "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs")
        assert job is not None
        self.assertEqual(job["externalId"], "5211305008")

    def test_short_or_slug_id_does_not_beat_requisition_id(self):
        # #49 review: normalize_listing wrote externalId with no shape check,
        # and ats_id() trusts externalId — so a junk `id` won and the guard in
        # plain_id() never ran on the staging path.
        from network_jobs.pagination import normalize_listing

        base = {
            "requisition_id": "R-12345",
            "title": "Staff Software Engineer",
            "absolute_url": "https://job-boards.greenhouse.io/x/jobs/9",
            "location": {"name": "London, UK"},
        }
        for junk in ("1", "senior-product-manager-nyc", "n/a"):
            job = normalize_listing({**base, "id": junk},
                                    "https://boards-api.greenhouse.io/v1/boards/x/jobs")
            assert job is not None
            self.assertEqual(job["externalId"], "R-12345", f"junk id {junk!r} won")

    def test_absolute_job_url_beats_job_path(self):
        # #49 review: job_path must not outrank an absolute jobUrl.
        from network_jobs.pagination import normalize_listing

        raw = {
            "title": "Product Manager",
            "job_path": "/en/jobs/1/pm",
            "jobUrl": "https://careers.x.com/jobs/1",
            "location": "Remote",
        }
        job = normalize_listing(raw, "https://api.x.com/v1/search.json")
        assert job is not None
        self.assertEqual(job["url"], "https://careers.x.com/jobs/1")

    def test_job_path_without_leading_slash_anchors_at_root(self):
        # #49 review nit: urljoin would nest this under the search path.
        from network_jobs.pagination import normalize_listing

        raw = {"title": "Product Manager", "job_path": "en/jobs/1/pm",
               "location": "Remote"}
        job = normalize_listing(raw, "https://www.amazon.jobs/en/search.json")
        assert job is not None
        self.assertEqual(job["url"], "https://www.amazon.jobs/en/jobs/1/pm")

    def test_greenhouse_departments_list_mapped(self):
        # Greenhouse nests departments as [{"name": ...}]. "departments" was
        # missing from DEPT_KEYS, so department was null on every row even
        # though the row already reached the mapping block.
        from network_jobs.pagination import normalize_listing

        raw = {
            "id": 8172487,
            "title": "Product Manager",
            "location": {"name": "San Francisco"},
            "absolute_url": "https://boards.greenhouse.io/anthropic/jobs/8172487",
            "departments": [{"id": 1, "name": "Product"}, {"id": 2, "name": "Growth"}],
            "updated_at": "2026-09-18T12:00:00-04:00",
        }
        job = normalize_listing(raw, "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs")
        assert job is not None
        self.assertEqual(job["url"], "https://boards.greenhouse.io/anthropic/jobs/8172487")
        self.assertEqual(job["externalId"], "8172487")
        self.assertEqual(job["location"], "San Francisco")
        self.assertEqual(job["department"], "Product")
        self.assertEqual(job["postedAt"], "2026-09-18")
        # The consumed alias must not also appear as an extra.
        self.assertNotIn("departments", job)

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


class SiteVersionTests(unittest.TestCase):
    """The landing page has no build step, so its version pill is hand-edited.

    It must track package.json: a stale pill is invisible to every other test.
    """

    def test_landing_page_pill_matches_package_version(self):
        import re

        version = json.loads((ROOT / "package.json").read_text())["version"]
        page = (ROOT / "site" / "public" / "index.html").read_text()
        expected = f"v{version} · MIT"

        pills = re.findall(r"v\d+\.\d+\.\d+ · MIT", page)
        self.assertEqual(
            pills.count(expected),
            1,
            f"site/public/index.html must show the {expected} pill exactly once; "
            f"found {pills!r}. Update the pill when bumping package.json.",
        )
        self.assertEqual(
            pills,
            [expected],
            f"site/public/index.html has a stale version pill: found {pills!r}, "
            f"expected only {expected!r}.",
        )


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


class AgentBrowserVersionFloorTests(unittest.TestCase):
    """The browser is optional, but a *stale* browser must be visible.

    A build can advertise a command in `--help` and reject it at runtime
    (agent-browser 0.38.1 listed `har`, then returned "Unknown command: har"),
    so a presence check is not enough — hence agentBrowser.minVersion in
    package.json and the shared helpers in lib/browser.sh.
    """

    def _shim(self, version: str) -> tuple[Path, Path]:
        tmp = Path(tempfile.mkdtemp(prefix="nj-ab-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        exe = bin_dir / "agent-browser"
        exe.write_text(f'#!/usr/bin/env bash\necho "agent-browser {version}"\n')
        exe.chmod(0o755)
        return tmp, bin_dir

    def _bash(self, script: str, env: dict | None = None):
        import subprocess

        e = dict(os.environ)
        if env:
            e.update(env)
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, env=e, timeout=120)

    def test_version_ge_semantics(self):
        cases = [
            ("0.39.1", "0.39.1", 0),
            ("0.40.0", "0.39.1", 0),
            ("0.39.10", "0.39.9", 0),
            ("1.0.0", "0.99.0", 0),
            ("0.38.1", "0.39.1", 1),
            ("0.39.0", "0.39.1", 1),
        ]
        for have, want, expected in cases:
            proc = self._bash(
                f'source "{ROOT}/lib/browser.sh"; ab_version_ge {have} {want}')
            self.assertEqual(proc.returncode, expected,
                             f"{have} >= {want} (stderr={proc.stderr})")

    def test_min_version_read_from_package_json(self):
        proc = self._bash(
            f'source "{ROOT}/lib/browser.sh"; ab_min_version "{ROOT}/package.json"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0.39.1")

    def test_installed_version_parsed_from_output(self):
        _tmp, bin_dir = self._shim("0.38.1")
        proc = self._bash(
            f'source "{ROOT}/lib/browser.sh"; ab_installed_version',
            env={"PATH": f"{bin_dir}:{os.environ['PATH']}"})
        self.assertEqual(proc.stdout.strip(), "0.38.1")

    def test_describe_version(self):
        cases = [
            ("0.39.1", "0.39.1", "0.39.1"),          # at floor
            ("0.40.0", "0.39.1", "0.40.0"),          # above floor
            ("0.38.1", "0.39.1", "0.38.1 < 0.39.1"),  # below floor
            ("", "0.39.1", "unknown"),               # absent/unparseable
            ("0.39.1", "", "0.39.1"),                # no floor declared
        ]
        for have, floor, expected in cases:
            proc = self._bash(
                f'source "{ROOT}/lib/browser.sh"; ab_describe_version "{have}" "{floor}"')
            self.assertEqual(proc.stdout.strip(), expected,
                             f"describe({have!r}, {floor!r})")

    @unittest.skipUnless(
        all(shutil.which(t) for t in ("jq", "curl", "unzip")),
        "doctor requires jq/curl/unzip")
    def test_doctor_warns_when_below_floor(self):
        tmp, bin_dir = self._shim("0.38.1")
        proc = self._bash(
            f'"{ROOT}/bin/network-jobs" doctor',
            env={"PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "NETWORK_JOBS_HOME": str(tmp / "data")})
        self.assertIn("WARN agent-browser 0.38.1 < 0.39.1", proc.stdout)
        self.assertIn("network-jobs update", proc.stdout)
        self.assertIn(f"npm i -g agent-browser@{self._min_version()}", proc.stdout)
        self.assertNotIn("@latest", proc.stdout)

    @unittest.skipUnless(
        all(shutil.which(t) for t in ("jq", "curl", "unzip")),
        "doctor requires jq/curl/unzip")
    def test_doctor_ok_at_floor(self):
        tmp, bin_dir = self._shim("0.39.1")
        proc = self._bash(
            f'"{ROOT}/bin/network-jobs" doctor',
            env={"PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "NETWORK_JOBS_HOME": str(tmp / "data")})
        self.assertIn("OK  agent-browser 0.39.1", proc.stdout)
        self.assertNotIn("WARN agent-browser", proc.stdout)

    def test_setup_reports_floor_without_installing(self):
        # --no-browser + --no-launcher keeps this offline; setup must still
        # surface the version gap rather than saying "ready".
        tmp, bin_dir = self._shim("0.38.1")
        proc = self._bash(
            f'HOME="{tmp / "home"}" bash "{ROOT}/setup" --no-browser --no-launcher --agent auto',
            env={"PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "NETWORK_JOBS_HOME": str(tmp / "data")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("agent-browser 0.38.1 < 0.39.1", proc.stdout)
        self.assertIn("network-jobs update", proc.stdout)

    def _min_version(self) -> str:
        return json.loads((ROOT / "package.json").read_text())["agentBrowser"]["minVersion"]

    def _fake_npm(self, tmp: Path, landed_version: str) -> Path:
        fake_bin = tmp / "fakebin"
        installed_bin = tmp / "prefix" / "bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        installed_bin.mkdir(parents=True, exist_ok=True)
        npm = fake_bin / "npm"
        npm.write_text(
            "#!/usr/bin/env bash\n"
            'case "$1" in\n'
            '  --version) echo 10.9.0 ;;\n'
            '  prefix) printf "%s\\n" "$FAKE_PREFIX" ;;\n'
            '  install) printf "%s\\n" "$*" > "$FAKE_PREFIX/install-args"\n'
            '    if [[ "$*" == *agent-browser* ]]; then\n'
            f'      printf \'#!/usr/bin/env bash\\necho "agent-browser {landed_version}"\\n\''
            ' > "$FAKE_PREFIX/bin/agent-browser";\n'
            '      chmod +x "$FAKE_PREFIX/bin/agent-browser"; fi ;;\n'
            "esac\nexit 0\n")
        npm.chmod(0o755)
        return fake_bin

    def test_setup_install_path_verifies_what_landed(self):
        # A stale registry mirror or proxy can serve a build below the floor;
        # setup must not report "installed" without checking the result.
        tmp = Path(tempfile.mkdtemp(prefix="nj-ab-install-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        fake_bin = self._fake_npm(tmp, "0.30.0")
        installed_bin = tmp / "prefix" / "bin"
        floor = self._min_version()
        proc = self._bash(
            f'HOME="{tmp / "home"}" bash "{ROOT}/setup" --agent auto --no-launcher',
            env={"PATH": f"{fake_bin}:{installed_bin}:/usr/bin:/bin:/usr/sbin",
                 "NETWORK_JOBS_HOME": str(tmp / "data"),
                 "FAKE_PREFIX": str(tmp / "prefix")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Installing agent-browser", proc.stdout)
        self.assertIn("below the floor", proc.stdout)
        spec = (tmp / "prefix" / "install-args").read_text()
        self.assertIn(f"agent-browser@{floor}", spec)
        self.assertNotIn("@latest", spec)

    def test_setup_upgrade_path_pins_min_version(self):
        # A below-floor install is upgraded to the package.json floor, not
        # @latest, and the post-upgrade check still reports what landed.
        tmp, stale_bin = self._shim("0.30.0")
        fake_bin = self._fake_npm(tmp, self._min_version())
        floor = self._min_version()
        # Leave prefix/bin off PATH so refresh_tool_path prepends it after
        # install, the same way a real npm global bin would land in front.
        proc = self._bash(
            f'HOME="{tmp / "home"}" bash "{ROOT}/setup" --agent auto --no-launcher',
            env={"PATH": f"{fake_bin}:{stale_bin}:/usr/bin:/bin:/usr/sbin",
                 "NETWORK_JOBS_HOME": str(tmp / "data"),
                 "FAKE_PREFIX": str(tmp / "prefix")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"Upgrading agent-browser 0.30.0 → {floor}", proc.stdout)
        self.assertIn(f"agent-browser {floor} (upgraded from 0.30.0)", proc.stdout)
        spec = (tmp / "prefix" / "install-args").read_text()
        self.assertIn(f"agent-browser@{floor}", spec)
        self.assertNotIn("@latest", spec)

    def test_setup_warns_when_min_version_missing(self):
        # No agentBrowser.minVersion: still install (browser is optional) but
        # say so — otherwise the floor check is a silent no-op.
        tmp = Path(tempfile.mkdtemp(prefix="nj-ab-nomin-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        suite = tmp / "suite"
        shutil.copytree(ROOT / "lib", suite / "lib")
        shutil.copytree(ROOT / "skills", suite / "skills")
        shutil.copy(ROOT / "setup", suite / "setup")
        pkg = json.loads((ROOT / "package.json").read_text())
        pkg.pop("agentBrowser", None)
        (suite / "package.json").write_text(json.dumps(pkg))
        fake_bin = self._fake_npm(tmp, "0.40.0")
        installed_bin = tmp / "prefix" / "bin"
        proc = self._bash(
            f'HOME="{tmp / "home"}" bash "{suite / "setup"}" --agent auto --no-launcher',
            env={"PATH": f"{fake_bin}:{installed_bin}:/usr/bin:/bin:/usr/sbin",
                 "NETWORK_JOBS_HOME": str(tmp / "data"),
                 "FAKE_PREFIX": str(tmp / "prefix")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Installing agent-browser", proc.stdout)
        self.assertIn("package.json has no agentBrowser.minVersion", proc.stdout)
        self.assertIn("installed unpinned", proc.stdout)
        spec = (tmp / "prefix" / "install-args").read_text()
        self.assertRegex(spec, r'(^| )agent-browser( |$)')
        self.assertNotIn("@latest", spec)
        self.assertNotIn("@0.", spec)

    @unittest.skipUnless(
        all(shutil.which(t) for t in ("jq", "curl", "unzip")),
        "doctor requires jq/curl/unzip")
    def test_doctor_missing_browser_hint_is_pinned(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-ab-doc-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        floor = self._min_version()
        # This asserts the "missing" branch, so drop any PATH entry that
        # provides agent-browser — otherwise the test depends on the host
        # happening not to have it installed.
        path = os.pathsep.join(
            d for d in os.environ["PATH"].split(os.pathsep)
            if d and not (Path(d) / "agent-browser").exists())
        proc = self._bash(
            f'"{ROOT}/bin/network-jobs" doctor',
            env={"PATH": path, "NETWORK_JOBS_HOME": str(tmp / "data")})
        self.assertIn("WARN agent-browser missing", proc.stdout)
        self.assertIn(f"npm i -g agent-browser@{floor}", proc.stdout)
        self.assertNotIn("@latest", proc.stdout)


class SetupNextStepsTests(unittest.TestCase):
    """`Next:` must point at the CLI setup itself just installed.

    setup writes the launcher to ~/.local/bin, which is usually not on PATH on
    a first run. A bare `command -v network-jobs` therefore missed it and the
    steps fell back to `npx --yes …@latest`, routing the user around the copy
    that had just been resolved.
    """

    def _run(self, home: Path, path: str):
        import subprocess

        env = dict(os.environ)
        env.update({"HOME": str(home),
                    "NETWORK_JOBS_HOME": str(home / "data"),
                    "PATH": path})
        return subprocess.run(
            ["bash", f"{ROOT}/setup", "--agent", "claude-code", "--no-browser"],
            capture_output=True, text=True, env=env, timeout=180)

    def test_launcher_off_path_is_called_by_absolute_path(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-setup-next-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        home = tmp / "home"
        home.mkdir()

        # ~/.local/bin is deliberately missing from PATH: the first-run case.
        proc = self._run(home, os.environ["PATH"])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        launcher = home / ".local" / "bin" / "network-jobs"
        self.assertTrue(launcher.exists(), proc.stdout)
        self.assertIn(f"{launcher} profile import", proc.stdout)
        self.assertNotIn("npx --yes @hirefrank/network-jobs@latest", proc.stdout)

    def test_launcher_on_path_uses_the_bare_command(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-setup-next-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        home = tmp / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        path = f"{home / '.local' / 'bin'}{os.pathsep}{os.environ['PATH']}"

        proc = self._run(home, path)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("network-jobs profile import", proc.stdout)
        self.assertNotIn(f"{home / '.local' / 'bin' / 'network-jobs'} profile import",
                         proc.stdout)


class SetupLauncherSuiteRootTests(unittest.TestCase):
    """#52: a recorded-but-missing suite must fail loudly, not run the registry.

    The launcher collapsed "no suite-root recorded" and "suite-root points at a
    deleted install" into one branch, so a stale install silently executed
    `pnpm dlx` / `npx @latest` — same version string, different code. These run
    the generated launcher by absolute path under a temp HOME, with stub
    runners, so nothing real is fetched or overwritten.
    """

    def _setup(self, tmp: Path, home_name: str = "home"):
        import subprocess

        home = tmp / home_name
        home.mkdir()
        fakebin = tmp / "bin"
        fakebin.mkdir()
        marker = tmp / "runner-called"
        for runner in ("pnpm", "npx"):
            stub = fakebin / runner
            stub.write_text("#!/usr/bin/env bash\n"
                            f'printf "%s\\n" "$*" >> "{marker}"\n'
                            "exit 0\n")
            stub.chmod(0o755)
        env = dict(os.environ)
        env.update({"HOME": str(home),
                    "NETWORK_JOBS_HOME": str(home / "data"),
                    "PATH": f"{fakebin}:/usr/bin:/bin"})
        proc = subprocess.run(
            ["bash", f"{ROOT}/setup", "--agent", "claude-code", "--no-browser"],
            capture_output=True, text=True, env=env, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return home, fakebin, marker, proc.stdout

    def _env(self, home: Path, fakebin: Path) -> dict[str, str]:
        env = dict(os.environ)
        env.update({"HOME": str(home),
                    "NETWORK_JOBS_HOME": str(home / "data"),
                    "PATH": f"{fakebin}:/usr/bin:/bin"})
        return env

    def _run_launcher(self, home: Path, fakebin: Path, *args: str):
        import subprocess

        launcher = home / ".local" / "bin" / "network-jobs"
        self.assertTrue(launcher.exists(), "setup did not write the launcher")
        return subprocess.run([str(launcher), *args], capture_output=True,
                              text=True, env=self._env(home, fakebin),
                              timeout=60)

    @staticmethod
    def _shell_quote(value: Path | str) -> str:
        """Exactly what `printf %q` prints, so paste-safety is asserted."""
        import subprocess

        return subprocess.run(
            ["bash", "-c", 'printf "%q" "$1"', "_", str(value)],
            capture_output=True, text=True, check=True).stdout

    def test_dangling_suite_root_fails_instead_of_running_registry_build(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-launcher-dangling-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        home, fakebin, marker, _ = self._setup(tmp)
        dead = tmp / "gone-suite"
        suite_root = home / "data" / "suite-root"
        suite_root.write_text(f"{dead}\n")

        proc = self._run_launcher(home, fakebin, "--version")

        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn(f"installed suite is gone: {self._shell_quote(dead)}",
                      proc.stderr)
        # Both recovery instructions must be runnable as printed.
        self.assertIn("Reinstall: npx --yes @hirefrank/network-jobs@latest "
                      "setup --agent auto", proc.stderr)
        self.assertIn(f"Or forget it: rm {self._shell_quote(suite_root)}",
                      proc.stderr)
        # No package runner may have been reached.
        self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")

    def test_pnpm_only_machine_gets_a_runnable_recovery_line(self):
        # A pnpm-only install has no npx; the printed reinstall command must
        # still be one this machine can run.
        tmp = Path(tempfile.mkdtemp(prefix="nj-launcher-pnpm-only-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        home, fakebin, marker, _ = self._setup(tmp)
        (fakebin / "npx").unlink()
        suite_root = home / "data" / "suite-root"
        suite_root.write_text(f"{tmp / 'gone-suite'}\n")

        proc = self._run_launcher(home, fakebin, "--version")

        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("Reinstall: pnpm dlx @hirefrank/network-jobs@latest "
                      "setup --agent auto", proc.stderr)
        self.assertNotIn("npx --yes", proc.stderr)
        self.assertFalse(marker.exists())

    def test_space_in_home_quotes_the_recovery_paths(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj launcher home "))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        home, fakebin, marker, _ = self._setup(tmp, home_name="home dir")
        dead = tmp / "gone suite"
        suite_root = home / "data" / "suite-root"
        suite_root.write_text(f"{dead}\n")

        proc = self._run_launcher(home, fakebin, "--version")

        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn(self._shell_quote(dead), proc.stderr)
        self.assertIn(f"rm {self._shell_quote(suite_root)}", proc.stderr)
        # The quoted rm line is a valid command.
        import subprocess

        rm_cmd = next(line.split("Or forget it: ", 1)[1]
                      for line in proc.stderr.splitlines()
                      if line.startswith("Or forget it: "))
        done = subprocess.run(["bash", "-c", rm_cmd], capture_output=True,
                              text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(suite_root.exists())
        self.assertFalse(marker.exists())

    def test_whitespace_only_suite_root_falls_back(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-launcher-blank-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        home, fakebin, marker, _ = self._setup(tmp)
        (home / "data" / "suite-root").write_text("  \n\t\n")

        proc = self._run_launcher(home, fakebin, "--version")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no suite-root recorded", proc.stderr)
        self.assertNotIn("installed suite is gone", proc.stderr)
        self.assertTrue(marker.exists(), proc.stderr)

    def test_absent_suite_root_falls_back_to_the_runner(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-launcher-fallback-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        home, fakebin, marker, _ = self._setup(tmp)
        # setup always records its suite; "absent" is the first-run / cleared case.
        (home / "data" / "suite-root").unlink()

        proc = self._run_launcher(home, fakebin, "--version")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(marker.exists(), proc.stderr)
        self.assertIn("@hirefrank/network-jobs@latest", marker.read_text())
        # The fallback is announced, not silent.
        self.assertIn("no suite-root recorded", proc.stderr)
        self.assertIn("registry build", proc.stderr)

    def test_which_warns_but_succeeds_when_the_executing_copy_is_fine(self):
        import subprocess

        tmp = Path(tempfile.mkdtemp(prefix="nj-which-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        data = tmp / "data"
        data.mkdir()
        dead = tmp / "gone-suite"
        suite_root = data / "suite-root"
        suite_root.write_text(f"{dead}\n")
        env = dict(os.environ)
        env.update({"NETWORK_JOBS_HOME": str(data), "HOME": str(tmp / "home")})

        proc = subprocess.run(
            ["bash", str(ROOT / "bin" / "network-jobs"), "which"],
            capture_output=True, text=True, env=env, timeout=60)

        # The executing copy is healthy, so this is a warning, not a failure.
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"suite={ROOT}", proc.stdout)
        self.assertIn("warning: suite-root points at a missing suite", proc.stderr)
        self.assertIn(self._shell_quote(dead), proc.stderr)
        self.assertIn(self._shell_quote(suite_root), proc.stderr)

    def test_reset_clears_dangling_suite_root_and_prints_runnable_relink(self):
        import subprocess

        # A space in HOME is part of the acceptance case.
        tmp = Path(tempfile.mkdtemp(prefix="nj reset home "))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        home, fakebin, marker, _ = self._setup(tmp, home_name="home dir")
        data = home / "data"
        suite_root = data / "suite-root"

        # Fake the npx cache layout the reset purge knows how to remove.
        cached = (home / ".npm" / "_npx" / "abc123" / "node_modules"
                  / "@hirefrank" / "network-jobs")
        shutil.copytree(ROOT, cached,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
        suite_root.write_text(f"{cached}\n")

        # (a) Simulate npx evicting the cache entry, then run the launcher.
        shutil.rmtree(cached)
        proc = self._run_launcher(home, fakebin, "--version")
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn(self._shell_quote(cached), proc.stderr)
        self.assertIn("Reinstall: npx --yes @hirefrank/network-jobs@latest "
                      "setup --agent auto", proc.stderr)
        self.assertFalse(marker.exists())

        # Restore the copy to drive the reset flow itself.
        shutil.copytree(ROOT, cached,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))

        # (b) reset --purge-cache removes that copy. It must clear suite-root
        # (else the printed next step dies at the launcher) and print an
        # explicit runner command.
        reset = subprocess.run(
            [str(cached / "bin" / "network-jobs"), "reset", "--yes",
             "--purge-cache", "--agent", "claude-code"],
            capture_output=True, text=True, env=self._env(home, fakebin),
            timeout=120)
        self.assertEqual(reset.returncode, 0, reset.stderr)
        self.assertFalse(cached.exists(), "purge did not remove the cached copy")
        self.assertFalse(suite_root.exists(), "suite-root was left dangling")
        self.assertIn(f"cleared dangling suite-root: {self._shell_quote(cached)}",
                      reset.stdout)
        relink = next(line.split("Re-link skills: ", 1)[1]
                      for line in reset.stdout.splitlines()
                      if line.startswith("Re-link skills: "))
        self.assertEqual(relink, "npx --yes @hirefrank/network-jobs@latest "
                                 "setup --agent auto")
        # reset prints the command; it must not have run it.
        self.assertFalse(marker.exists())

        # And the printed command actually runs (stub records the setup args).
        rerun = subprocess.run(["bash", "-c", relink], capture_output=True,
                               text=True, env=self._env(home, fakebin),
                               timeout=60)
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertIn("setup --agent auto", marker.read_text())

    def test_reset_keeps_a_suite_root_that_still_resolves(self):
        import subprocess

        tmp = Path(tempfile.mkdtemp(prefix="nj-reset-valid-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        home, fakebin, marker, _ = self._setup(tmp)
        suite_root = home / "data" / "suite-root"
        suite_root.write_text(f"{ROOT}\n")

        proc = subprocess.run(
            [str(ROOT / "bin" / "network-jobs"), "reset", "--yes",
             "--purge-cache", "--agent", "claude-code"],
            capture_output=True, text=True, env=self._env(home, fakebin),
            timeout=120)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(suite_root.exists(), proc.stdout)
        self.assertEqual(suite_root.read_text().strip(), str(ROOT))
        self.assertNotIn("cleared dangling suite-root", proc.stdout)

    def test_setup_offers_embeddings_with_value_and_cost(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-setup-embed-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        _, _, _, stdout = self._setup(tmp)

        self.assertIn("Optional:", stdout)
        self.assertIn("embed-setup", stdout)
        self.assertIn("Semantic resume<->job matching", stdout)
        self.assertIn("60MB", stdout)
        self.assertIn("pip-installs fastembed", stdout)
        self.assertIn("onnxruntime", stdout)
        self.assertIn("Ollama", stdout)
        self.assertIn("Keyword scoring works without it", stdout)


class InventoryPipelineTests(unittest.TestCase):
    """#33: the INVENTORY header is owned by the helpers, not by the model."""

    @staticmethod
    def _fields(text: str) -> dict[str, str]:
        """Every `- **Label:** value` line in the file, which lists each once.

        Listings/Pagination live in the summary block, not the header (#63),
        so the helper-owned state is read from the whole document.
        """
        import re

        return {
            label.strip(): value.strip()
            for label, value in re.findall(r"^- \*\*([^*]+):\*\* (.*)$", text, re.M)
        }

    def _stage(self, tmp: Path, name: str = "Example Co", slug: str = "example-co"):
        import subprocess

        data = tmp / "data"
        (data / "resume").mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / "preferences.json", data / "preferences.json")
        shutil.copy(FIXTURES / "resume-keywords.md", data / "resume" / "text.md")
        env = {**os.environ, "NETWORK_JOBS_HOME": str(data)}
        proc = subprocess.run(
            ["bash", str(ROOT / "skills" / "careers-discover" / "helpers" / "stage-company.sh"),
             name, slug],
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        triage = Path(proc.stdout.strip().splitlines()[-1])
        self.assertTrue((triage / "INVENTORY.md").is_file())
        return data, triage

    def test_header_agrees_with_index_after_paginate_and_match_prefs(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-inv-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        data, triage = self._stage(tmp)
        capture = tmp / "capture.json"
        capture.write_text(json.dumps([
            {"title": "Senior Product Manager, Foundations",
             "url": "https://example.com/jobs/1", "location": "Remote",
             "sourceUrl": "https://example.com/careers"},
            {"title": "Senior Backend Engineer",
             "url": "https://example.com/jobs/2", "location": "Remote",
             "sourceUrl": "https://example.com/careers"},
        ]))
        rc = helper_main(["paginate", "--triage-dir", str(triage),
                          "--input", str(capture), "--company", "Example Co"])
        self.assertEqual(rc, 0)
        rc = helper_main(["match-prefs", "--triage-dir", str(triage),
                          "--data", str(data), "--company", "Example Co"])
        self.assertEqual(rc, 0)

        listings = json.loads((triage / "index" / "listings.json").read_text())
        pagination = json.loads((triage / "index" / "pagination.json").read_text())
        inventory = (triage / "INVENTORY.md").read_text()
        header = self._fields(inventory)
        # #63: Listings/Pagination are canonical in the summary block only.
        head = inventory.split(MARKER_START)[0]
        self.assertNotIn("Listings", head)
        self.assertNotIn("Pagination", head)

        self.assertEqual(int(header["Listings"]), len(listings))
        self.assertEqual(header["Listings"], "2")
        # Careers URL comes from the rows' sourceUrl, never a placeholder.
        self.assertEqual(header["Careers URL"], "https://example.com/careers")
        self.assertNotIn("(fill)", header["Careers URL"])
        self.assertIn("Status", header)
        self.assertEqual(header["Status"], pagination["status"])
        # The rows came from `paginate --input`, so the helper stamps a user
        # capture, never a complete crawl (#39).
        self.assertEqual(header["Status"], "captured")
        self.assertFalse(pagination["complete"])
        self.assertIn(f"complete={str(pagination['complete']).lower()}",
                      header["Pagination"])
        # The human summary block agrees with the header.
        self.assertIn("- **Listings:** 2", inventory)

    def test_manual_card_extract_without_pagination_keeps_header_line(self):
        """#63 blocker: no pagination.json -> the block omits Pagination.

        The manual card-extract path (`stage-company.sh`, hand-written
        listings.json, no helper pagination) must keep the header Pagination
        line, including `status=captured`, and must not be reported as a
        complete crawl.
        """
        tmp = Path(tempfile.mkdtemp(prefix="nj-manual-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        data, triage = self._stage(tmp, name="Beta", slug="beta")
        (triage / "index" / "listings.json").write_text(json.dumps([
            {"title": "Senior Backend Engineer",
             "url": "https://example.com/jobs/1", "location": "Remote",
             "department": "Engineering"},
        ]))
        manual_pagination = (
            "pages=1, complete=false, truncated=false, "
            "status=captured (manual card extract)"
        )
        inventory = triage / "INVENTORY.md"
        inventory.write_text(
            inventory.read_text()
            .replace("- **Pagination:** (pending)",
                     f"- **Pagination:** {manual_pagination}")
            .replace("- **Status:** (pending)", "- **Status:** captured")
        )
        self.assertFalse((triage / "index" / "pagination.json").exists())

        rc = helper_main(["match-prefs", "--triage-dir", str(triage),
                          "--data", str(data), "--company", "Beta"])
        self.assertEqual(rc, 0)
        text = inventory.read_text()
        self.assertEqual(text.count("- **Pagination:**"), 1)
        self.assertIn(f"- **Pagination:** {manual_pagination}", text)
        self.assertIn("- **Status:** captured", text)
        self.assertNotIn("(pending)", text)
        # Listings lives only in the summary block now.
        head = text.split(MARKER_START)[0]
        self.assertNotIn("Listings", head)
        self.assertEqual(text.count("- **Listings:**"), 1)
        self.assertIn("- **Listings:** 1", text.split(MARKER_START)[1])

        # A second match-prefs run must not re-add or drop either line.
        rc = helper_main(["match-prefs", "--triage-dir", str(triage),
                          "--data", str(data), "--company", "Beta"])
        self.assertEqual(rc, 0)
        self.assertEqual(inventory.read_text(), text)

    def test_zero_row_manual_extract_gets_blocked_status(self):
        """#63: zero rows with no pagination.json is blocked, not `(pending)`."""
        tmp = Path(tempfile.mkdtemp(prefix="nj-zero-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        data, triage = self._stage(tmp, name="Beta", slug="beta")
        (triage / "index" / "listings.json").write_text("[]\n")
        inventory = triage / "INVENTORY.md"
        self.assertIn("- **Status:** (pending)", inventory.read_text())

        rc = helper_main(["match-prefs", "--triage-dir", str(triage),
                          "--data", str(data), "--company", "Beta"])
        self.assertEqual(rc, 0)
        text = inventory.read_text()
        self.assertIn("- **Status:** blocked", text)
        self.assertEqual(text.count("- **Status:**"), 1)
        head = text.split(MARKER_START)[0]
        self.assertNotIn("Listings", head)
        self.assertIn("- **Listings:** 0", text.split(MARKER_START)[1])

    def test_pagination_status_matrix(self):
        import re

        from network_jobs.pagination import paginate, write_pagination

        triple = Path(tempfile.mkdtemp(prefix="nj-status-"))
        self.addCleanup(lambda: shutil.rmtree(triple, ignore_errors=True))

        def triage_for(label: str) -> Path:
            triage = triple / label
            (triage / "index").mkdir(parents=True)
            (triage / "INVENTORY.md").write_text(
                f"# Careers triage — {label}\n\n- **Listings:** 0\n"
                "- **Careers URL:** (fill)\n- **Pagination:** (pending)\n"
                "- **Status:** (pending)\n"
            )
            return triage

        def staged(label: str, result: dict) -> dict:
            triage = triage_for(label)
            write_pagination(triage, result, company=label.title())
            pag = json.loads((triage / "index" / "pagination.json").read_text())
            text = (triage / "INVENTORY.md").read_text()
            header = self._fields(text)
            head = text.split(MARKER_START)[0]
            self.assertNotIn("Listings", head)
            self.assertNotIn("Pagination", head)
            self.assertEqual(pag["status"], header["Status"])
            self.assertEqual(int(header["Listings"]),
                             len(json.loads((triage / "index" / "listings.json").read_text())))
            # FIX 4: the Pagination line mirrors the same machine-readable state.
            self.assertTrue(re.match(r"^pages=\d+, complete=(true|false), "
                                     r"truncated=(true|false), status=",
                                     header["Pagination"]))
            self.assertIn(f"status={pag['status']}", header["Pagination"])
            self.assertIn(f"complete={str(pag['complete']).lower()}",
                          header["Pagination"])
            return pag

        # complete: clean crawl with rows.
        result = paginate(None, payload=[
            {"title": "Engineer", "url": "https://example.com/jobs/1",
             "location": "Remote", "sourceUrl": "https://example.com/careers"},
        ])
        self.assertEqual(result["pagination"]["status"], "complete")
        self.assertTrue(result["pagination"]["complete"])
        pag = staged("clean", result)
        self.assertEqual(pag["status"], "complete")

        # truncated: page cap hit with rows captured.
        jobs = [{"title": f"Engineer {i}", "url": f"https://example.com/jobs/{i}",
                 "location": "Remote"} for i in range(60)]
        payload = {"jobs": jobs}

        def fake_fetch(url):
            return payload, 200, json.dumps(payload).encode()

        result = paginate("https://example.com/api/jobs", fetch=fake_fetch,
                          max_pages=1, page_size=50)
        self.assertTrue(result["pagination"]["truncated"])
        self.assertEqual(result["pagination"]["status"], "truncated")
        self.assertFalse(result["pagination"]["complete"])
        pag = staged("capped", result)
        self.assertEqual(pag["status"], "truncated")

        # empty: a clean 200 with a parsed, empty board means every role closed.
        # `complete` stays true so `--expect-count 0` can expire the old rows.
        empty_payload = {"jobs": [], "total": 0}

        def empty_fetch(url):
            return empty_payload, 200, json.dumps(empty_payload).encode()

        result = paginate("https://example.com/api/jobs", fetch=empty_fetch)
        self.assertEqual(result["listings"], [])
        self.assertEqual(result["pagination"]["status"], "empty")
        self.assertTrue(result["pagination"]["complete"])
        self.assertFalse(result["pagination"]["truncated"])
        pag = staged("empty", result)
        self.assertEqual(pag["status"], "empty")

        # blocked: nothing parseable came back. Neither an unparseable body nor
        # a JSON object without a job list is evidence the company has no roles.
        result = paginate(None, payload=None)
        self.assertEqual(result["pagination"]["status"], "blocked")
        self.assertFalse(result["pagination"]["complete"])
        self.assertEqual(staged("blocked-body", result)["status"], "blocked")

        result = paginate(None, payload={"error": "forbidden", "html": "<html>"})
        self.assertEqual(result["pagination"]["status"], "blocked")
        self.assertEqual(staged("blocked-shape", result)["status"], "blocked")

        # blocked: the fetch itself failed (dead URL / refused connection).
        # `paginate()` still raises so `refresh` keeps its error reporting; the
        # `paginate` CLI records this shape instead of crashing.
        from network_jobs.pagination import blocked_result

        result = blocked_result("https://example.com/api/jobs",
                                "URLError: connection refused")
        self.assertEqual(result["pagination"]["status"], "blocked")
        self.assertFalse(result["pagination"]["complete"])
        self.assertEqual(result["fetchLog"][0]["disposition"], "error")
        self.assertEqual(staged("blocked-fetch", result)["status"], "blocked")

        # captured: a `paginate --input` run is a saved page, never a crawl —
        # complete is false so ingest cannot expire the rest of the board.
        result = paginate(None, payload=[
            {"title": "Engineer", "url": "https://example.com/jobs/1",
             "location": "Remote"},
        ], capture=True)
        self.assertEqual(result["pagination"]["status"], "captured")
        self.assertFalse(result["pagination"]["complete"])
        pag = staged("captured", result)
        self.assertEqual(pag["status"], "captured")
        self.assertFalse(pag["complete"])

        # A capture that yielded no row is a failed read, not an empty board.
        result = paginate(None, payload=[], capture=True)
        self.assertEqual(result["pagination"]["status"], "blocked")
        self.assertFalse(result["pagination"]["complete"])

    def test_paginate_input_cli_records_captured(self):
        import io
        from contextlib import redirect_stdout

        tmp = Path(tempfile.mkdtemp(prefix="nj-capture-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        capture = tmp / "capture.json"
        capture.write_text(json.dumps([
            {"title": "Engineer", "url": "https://example.com/jobs/1",
             "location": "Remote"},
        ]))
        triage = tmp / "triage"
        out = io.StringIO()
        with redirect_stdout(out):
            rc = helper_main(["paginate", "--input", str(capture),
                              "--triage-dir", str(triage), "--company", "Gated Co"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "captured")
        self.assertFalse(payload["complete"])
        pag = json.loads((triage / "index" / "pagination.json").read_text())
        self.assertEqual(pag["status"], "captured")
        self.assertFalse(pag["complete"])
        inventory = (triage / "INVENTORY.md").read_text()
        header = self._fields(inventory)
        self.assertEqual(header["Status"], "captured")
        # A fresh file carries Pagination once, in the summary block (#55).
        self.assertEqual(inventory.count("- **Pagination:**"), 1)
        self.assertIn("status=captured", inventory)

    def test_paginate_cli_records_blocked_when_fetch_fails(self):
        import io
        from contextlib import redirect_stdout
        from urllib.error import URLError

        import network_jobs.pagination as pag_mod

        tmp = Path(tempfile.mkdtemp(prefix="nj-dead-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        triage = tmp / "triage"

        def dead(url, timeout=60):
            raise URLError("connection refused")

        real = pag_mod.fetch_json
        pag_mod.fetch_json = dead
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                rc = helper_main(["paginate", "--url", "https://example.com/dead",
                                  "--triage-dir", str(triage), "--company", "Dead Co"])
        finally:
            pag_mod.fetch_json = real
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["complete"])
        pag = json.loads((triage / "index" / "pagination.json").read_text())
        self.assertEqual(pag["status"], "blocked")
        # Nothing captured is not an empty board: ingest must not expire it.
        self.assertEqual(json.loads((triage / "index" / "listings.json").read_text()), [])
        header = self._fields((triage / "INVENTORY.md").read_text())
        self.assertEqual(header["Status"], "blocked")
        logs = list((triage / "fetch-log").glob("*.json"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(json.loads(logs[0].read_text())["disposition"], "error")

    def test_fetch_descriptions_accepts_company_flag(self):
        import io
        from contextlib import redirect_stdout

        tmp = Path(tempfile.mkdtemp(prefix="nj-fd-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        triage = tmp / "triage"
        (triage / "index").mkdir(parents=True)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = helper_main(["fetch-descriptions", "--triage-dir", str(triage),
                              "--company", "Example Co", "--timeout", "1"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["company"], "Example Co")
        self.assertEqual(payload["fetched"], 0)  # no matches.json, no network


class InventoryOutputTests(unittest.TestCase):
    """#55: fresh INVENTORY.md shows each label once; no fake department."""

    SUMMARY = {
        "company": "Example Co",
        "nListings": 3,
        "nMatches": 2,
        "careersUrl": "https://example.com/careers",
        "pagination": {"pages": 1, "complete": True, "truncated": False,
                       "status": "complete"},
        "departments": {"(none)": 3},
    }

    @staticmethod
    def _counts(text: str, labels: tuple[str, ...]) -> dict[str, int]:
        import re

        return {
            label: len(re.findall(rf"^- \*\*{re.escape(label)}:\*\*", text, re.M))
            for label in labels
        }

    def test_fresh_file_has_each_label_once(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-fresh-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        path = tmp / "INVENTORY.md"

        upsert_inventory(path, self.SUMMARY)
        text = path.read_text()

        counts = self._counts(
            text, ("Listings", "Careers URL", "Pagination", "Status", "Departments")
        )
        self.assertEqual(set(counts.values()), {1}, counts)
        # The documented contract still holds: every header field is present,
        # along with the match line and the department histogram.
        self.assertIn("- **Careers URL:** https://example.com/careers", text)
        self.assertIn("- **Matches:** 2 of 3 match prefs", text)
        self.assertIn("status=complete", text)
        self.assertIn("## Pipeline summary", text)
        # Re-running a helper patches the same file instead of re-adding the
        # header fields the summary block already carries.
        upsert_inventory(path, self.SUMMARY)
        self.assertEqual(self._counts(path.read_text(), tuple(counts)), counts)

    def test_existing_template_is_patched_in_place(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-template-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        path = tmp / "INVENTORY.md"
        path.write_text(
            "# Careers triage — Example Co\n\n"
            "- **Slug:** example-co\n"
            "- **Careers URL:** (fill)\n"
            "- **Listings:** 0\n"
            "- **Pagination:** (pending)\n"
            "- **Status:** (pending)\n"
            "- **Caveats:**\n"
        )

        upsert_inventory(path, self.SUMMARY)
        first = path.read_text()
        head = first.split("## Pipeline summary")[0]
        self.assertIn("- **Slug:** example-co", head)
        self.assertIn("- **Careers URL:** https://example.com/careers", head)
        self.assertIn("- **Status:** complete", head)
        self.assertIn("- **Caveats:**", head)
        self.assertNotIn("(fill)", first)
        # #63: the summary block owns Listings/Pagination, so the header lines
        # the skeleton carried must be gone — one label per file, not per head.
        self.assertEqual(
            self._counts(first, ("Listings", "Careers URL", "Pagination", "Status",
                                 "Departments")),
            {"Listings": 1, "Careers URL": 1, "Pagination": 1, "Status": 1,
             "Departments": 1},
        )
        self.assertIn("- **Listings:** 3", first.split("## Pipeline summary")[1])

        # A second write updates the same lines; it does not append copies.
        upsert_inventory(path, {**self.SUMMARY, "nListings": 7, "nMatches": 4})
        second = path.read_text()
        self.assertEqual(second.count("## Pipeline summary"), 1)
        head = second.split("## Pipeline summary")[0]
        self.assertNotIn("Listings", head)
        self.assertEqual(
            self._counts(second, ("Listings", "Careers URL", "Pagination", "Status",
                                  "Departments")),
            {"Listings": 1, "Careers URL": 1, "Pagination": 1, "Status": 1,
             "Departments": 1},
        )
        self.assertIn("- **Listings:** 7", second.split("## Pipeline summary")[1])
        self.assertIn("- **Matches:** 4 of 7 match prefs", second)

    def test_stage_company_skeleton_heals_to_one_label_per_file(self):
        """#63: the real stage-company.sh template must not duplicate labels."""
        tmp = Path(tempfile.mkdtemp(prefix="nj-skeleton-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        path = tmp / "INVENTORY.md"
        # Copied verbatim from skills/careers-discover/helpers/stage-company.sh.
        path.write_text(
            "# Careers triage — Anthropic\n\n"
            "- **Slug:** anthropic\n"
            "- **Date:** 2026-10-22\n"
            "- **Careers URL:** (fill)\n"
            "- **Listings:** 0\n"
            "- **Pagination:** (pending)\n"
            "- **Status:** (pending)\n"
            "- **Caveats:**\n"
            "- **Next steps:** Review listings.json, then run jobs-ingest if OK.\n"
        )

        upsert_inventory(path, {**self.SUMMARY, "company": "Anthropic",
                                "nListings": 3, "nMatches": 1})
        first = path.read_text()
        labels = ("Listings", "Careers URL", "Pagination", "Status", "Departments")
        self.assertEqual(self._counts(first, labels),
                         {label: 1 for label in labels})

        upsert_inventory(path, {**self.SUMMARY, "company": "Anthropic",
                                "nListings": 7, "nMatches": 1})
        second = path.read_text()
        self.assertEqual(second.count("## Pipeline summary"), 1)
        self.assertEqual(self._counts(second, labels),
                         {label: 1 for label in labels})
        self.assertNotIn("(fill)", second)
        self.assertNotIn("(pending)", second)
        self.assertIn("- **Listings:** 7", second.split("## Pipeline summary")[1])
        self.assertIn("- **Careers URL:** https://example.com/careers", second)
        self.assertIn("- **Status:** complete", second)
        # Prose the skeleton added outside the header survives the heal.
        self.assertIn("- **Date:** 2026-10-22", second)
        self.assertIn("- **Next steps:** Review listings.json", second)

    def test_duplicate_header_occurrences_are_all_removed(self):
        """#63: strip every header copy, not just the first."""
        tmp = Path(tempfile.mkdtemp(prefix="nj-dupes-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        path = tmp / "INVENTORY.md"
        path.write_text(
            "# Careers triage — Example Co\n\n"
            "- **Listings:** 0\n"
            "- **Listings:** 1\n"
            "- **Pagination:** (pending)\n"
            "- **Pagination:** pages=1, complete=true, truncated=false, status=complete\n"
            "- **Careers URL:** (fill)\n"
            "- **Status:** (pending)\n"
        )

        upsert_inventory(path, self.SUMMARY)
        text = path.read_text()
        head = text.split(MARKER_START)[0]
        self.assertNotIn("Listings", head)
        self.assertNotIn("Pagination", head)
        self.assertEqual(text.count("- **Listings:**"), 1)
        self.assertEqual(text.count("- **Pagination:**"), 1)

    def test_placeholder_status_is_patched_without_pagination(self):
        """#63: a skeleton placeholder must not survive a helper write."""
        tmp = Path(tempfile.mkdtemp(prefix="nj-ph-status-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        path = tmp / "INVENTORY.md"
        path.write_text(
            "# Careers triage — Example Co\n\n"
            "- **Slug:** example-co\n"
            "- **Careers URL:** (fill)\n"
            "- **Listings:** 0\n"
            "- **Pagination:** (pending)\n"
            "- **Status:** (pending)\n"
        )
        summary = {k: v for k, v in self.SUMMARY.items() if k != "pagination"}
        summary["nListings"] = 2

        upsert_inventory(path, summary)
        text = path.read_text()
        self.assertIn("- **Status:** complete", text)
        self.assertNotIn("- **Status:** (pending)", text)
        self.assertEqual(self._counts(text, ("Status",)), {"Status": 1})

    def test_hand_filled_status_is_kept_without_pagination(self):
        """A real hand-filled Status is evidence, not a placeholder."""
        tmp = Path(tempfile.mkdtemp(prefix="nj-fill-status-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        path = tmp / "INVENTORY.md"
        path.write_text(
            "# Careers triage — Example Co\n\n"
            "- **Slug:** example-co\n"
            "- **Careers URL:** (fill)\n"
            "- **Listings:** 1\n"
            "- **Pagination:** (pending)\n"
            "- **Status:** captured (manual card extract)\n"
        )
        summary = {k: v for k, v in self.SUMMARY.items() if k != "pagination"}
        summary["nListings"] = 1

        upsert_inventory(path, summary)
        text = path.read_text()
        self.assertIn("- **Status:** captured (manual card extract)", text)
        self.assertEqual(self._counts(text, ("Status",)), {"Status": 1})

    def test_missing_status_is_added_without_pagination(self):
        """No placeholder to preserve: the computed Status is added."""
        tmp = Path(tempfile.mkdtemp(prefix="nj-no-status-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        path = tmp / "INVENTORY.md"
        path.write_text(
            "# Careers triage — Example Co\n\n"
            "- **Slug:** example-co\n"
            "- **Careers URL:** (fill)\n"
            "- **Listings:** 0\n"
            "- **Pagination:** (pending)\n"
        )
        summary = {k: v for k, v in self.SUMMARY.items() if k != "pagination"}
        summary["nListings"] = 2

        upsert_inventory(path, summary)
        text = path.read_text()
        self.assertIn("- **Status:** complete", text)
        self.assertEqual(self._counts(text, ("Status",)), {"Status": 1})

    def test_unknown_departments_say_so(self):
        self.assertEqual(
            department_histogram_line({"(none)": 679}),
            "not provided by this board (679)",
        )
        self.assertIn(
            "- **Departments:** not provided by this board (3)",
            render_summary_block(self.SUMMARY),
        )

    def test_real_histogram_is_unchanged(self):
        self.assertEqual(
            department_histogram_line({"Engineering": 12, "Sales": 4, "Design": 2}),
            "Engineering (12), Sales (4), Design (2)",
        )
        block = render_summary_block(
            {**self.SUMMARY, "departments": {"Engineering": 2, "(none)": 1}}
        )
        self.assertIn("- **Departments:** Engineering (2), (none) (1)", block)
        self.assertNotIn("not provided by this board", block)


class SkillDocConsistencyTests(unittest.TestCase):
    """#33/#34: the skill must not document flags that helpers reject."""

    SKILL = ROOT / "skills" / "careers-discover" / "SKILL.md"

    # Flags each helper accepts *after* its own script name (the root parser's
    # -v is documented nowhere and would have to precede the subcommand).
    HELPER_FLAGS = {
        "paginate-listings.py": {"--url", "--input", "--triage-dir", "--company",
                                 "--max-pages", "--max-listings"},
        "crawl-state.py": {"--triage-dir", "--listings", "--companies", "--data",
                           "--company", "--slug", "--stamp", "--source-url"},
        "match-prefs.py": {"--triage-dir", "--listings", "--prefs", "--resume",
                           "--data", "--company"},
        "fetch-descriptions.py": {"--triage-dir", "--all", "--company", "--timeout",
                                  "--max-chars"},
        "review-matches.py": {"--triage-dir", "--listings", "--prefs", "--resume",
                              "--data", "--company", "--min-cluster", "--veto",
                              "--json"},
    }

    def _text(self) -> str:
        return self.SKILL.read_text()

    def _invocations(self, helper: str) -> list[str]:
        lines = self._text().splitlines()
        blocks: list[str] = []
        for i, line in enumerate(lines):
            if helper not in line:
                continue
            block = [line]
            j = i
            while block[-1].rstrip().endswith("\\") and j + 1 < len(lines):
                j += 1
                block.append(lines[j])
            blocks.append("\n".join(block))
        return blocks

    def test_exactly_one_7b_section(self):
        import re

        self.assertEqual(len(re.findall(r"^7b\.", self._text(), re.M)), 1)

    def test_documented_helper_flags_are_accepted(self):
        import re

        for helper, allowed in self.HELPER_FLAGS.items():
            with self.subTest(helper=helper):
                invocations = self._invocations(helper)
                self.assertTrue(invocations, f"{helper} is never invoked in SKILL.md")
                for block in invocations:
                    used = set(re.findall(r"--[A-Za-z0-9-]+", block))
                    self.assertFalse(used - allowed,
                                     f"{helper}: doc uses {sorted(used - allowed)}")

    def test_skill_documents_budget_and_user_capture(self):
        text = self._text()
        self.assertIn("2 endpoint", text)
        self.assertIn("5 browser interactions", text)
        self.assertIn("user-capture", text)
        self.assertIn("captures/", text)
        self.assertIn("--input", text)
        self.assertIn("status: blocked", text)
        self.assertIn("Never invent", text)
        # #39: the capture caveat is machine-readable, not an INVENTORY note.
        self.assertIn("complete: false", text)
        self.assertIn('status: "captured"', text)
        self.assertIn("status: empty", text)

    def test_jobs_ingest_never_expires_captured_or_blocked(self):
        text = (ROOT / "skills" / "jobs-ingest" / "SKILL.md").read_text()
        # #39: expiry rule names captured alongside blocked. Empty with
        # prior open roles now needs --force-expire rather than auto-expiring.
        self.assertIn("never", text)
        self.assertIn("user-capture", text)
        self.assertIn("`captured`", text)
        self.assertIn("`blocked`", text)
        self.assertIn("status: empty", text)
        self.assertIn("--pagination-status", text)
        self.assertIn("--force-expire", text)
        self.assertIn("expirySkipped", text)
        self.assertIn("expiryReason", text)


class ResetRemovalHintTests(unittest.TestCase):
    """A full wipe removes skills and data — not the CLI itself.

    reset must print the remaining steps and never execute them: a CLI cannot
    safely uninstall the package it is running from. Leaving the launcher in
    place is also not inert, because it falls back to `npx …@latest`.
    """

    def _run(self, argv, home, data):
        import subprocess

        env = dict(os.environ)
        env["HOME"] = str(home)
        env["NETWORK_JOBS_HOME"] = str(data)
        return subprocess.run(["bash", *[str(a) for a in argv]],
                              capture_output=True, text=True, env=env, timeout=120)

    def _npm_layout(self, tmp: Path) -> Path:
        pkg = tmp / "prefix" / "lib" / "node_modules" / "@hirefrank" / "network-jobs"
        pkg.parent.mkdir(parents=True, exist_ok=True)
        pkg.symlink_to(ROOT, target_is_directory=True)
        binp = tmp / "prefix" / "bin"
        binp.mkdir(parents=True, exist_ok=True)
        entry = binp / "network-jobs"
        entry.symlink_to("../lib/node_modules/@hirefrank/network-jobs/bin/network-jobs")
        return entry

    def _home_with_launcher(self, tmp: Path, subdir: str = ".local/bin",
                            marker: bool = True) -> Path:
        launcher = tmp / "home" / subdir / "network-jobs"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        body = "#!/usr/bin/env bash\n"
        if marker:
            body += "# network-jobs launcher — resolves the installed suite\n"
        body += "echo shim\n"
        launcher.write_text(body)
        launcher.chmod(0o755)
        return launcher

    def test_npm_global_install_prints_uninstall_and_launcher_rm(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-reset-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        entry = self._npm_layout(tmp)
        launcher = self._home_with_launcher(tmp)
        data = tmp / "data"
        data.mkdir()

        proc = self._run([entry, "reset", "--yes", "--data", "--agent", "claude-code"],
                         tmp / "home", data)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("npm uninstall -g @hirefrank/network-jobs", proc.stdout)
        self.assertIn(f"rm -f {launcher}", proc.stdout)
        # Printed, never run.
        self.assertTrue(launcher.exists(), "reset must not delete the launcher")
        self.assertFalse(data.exists(), "reset --data should wipe the data dir")

    def test_purge_step_uses_resolved_path_not_the_launcher(self):
        # After --data the launcher would fall back to `npx @latest`, refilling
        # the cache the step is meant to purge.
        tmp = Path(tempfile.mkdtemp(prefix="nj-reset-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        entry = self._npm_layout(tmp)
        self._home_with_launcher(tmp)
        data = tmp / "data"
        data.mkdir()

        proc = self._run([entry, "reset", "--yes", "--data", "--agent", "claude-code"],
                         tmp / "home", data)

        resolved = tmp / "prefix" / "lib" / "node_modules" / "@hirefrank" / "network-jobs"
        self.assertIn(f"{resolved}/bin/network-jobs reset --yes --purge-cache", proc.stdout)
        self.assertNotIn("  network-jobs reset --yes --purge-cache", proc.stdout)

    def test_launcher_in_home_bin_is_found(self):
        # setup installs to ~/bin when ~/.local/bin is not on PATH.
        tmp = Path(tempfile.mkdtemp(prefix="nj-reset-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        entry = self._npm_layout(tmp)
        launcher = self._home_with_launcher(tmp, subdir="bin")
        data = tmp / "data"
        data.mkdir()

        proc = self._run([entry, "reset", "--yes", "--data", "--agent", "claude-code"],
                         tmp / "home", data)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"rm -f {launcher}", proc.stdout)

    def test_launcher_path_is_shell_quoted(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-reset-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        entry = self._npm_layout(tmp)
        home = tmp / "h b"
        home.mkdir()
        launcher = self._home_with_launcher(tmp, subdir="bin")
        # _home_with_launcher writes under tmp/home; rebuild under the spaced home.
        spaced = home / ".local" / "bin" / "network-jobs"
        spaced.parent.mkdir(parents=True)
        spaced.write_text("#!/usr/bin/env bash\n# network-jobs launcher\necho shim\n")
        spaced.chmod(0o755)
        data = tmp / "data"
        data.mkdir()

        proc = self._run([entry, "reset", "--yes", "--data", "--agent", "claude-code"],
                         home, data)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("h\\ b", proc.stdout, "path with a space must be shell-quoted")

    def test_foreign_file_named_network_jobs_is_not_removed(self):
        # A user wrapper without setup's marker must not be offered for deletion.
        tmp = Path(tempfile.mkdtemp(prefix="nj-reset-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        entry = self._npm_layout(tmp)
        self._home_with_launcher(tmp, marker=False)
        data = tmp / "data"
        data.mkdir()

        proc = self._run([entry, "reset", "--yes", "--data", "--agent", "claude-code"],
                         tmp / "home", data)

        self.assertNotIn("rm -f", proc.stdout)

    def test_plain_reset_does_not_print_removal_hint(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-reset-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        entry = self._npm_layout(tmp)
        self._home_with_launcher(tmp)
        data = tmp / "data"
        data.mkdir()

        proc = self._run([entry, "reset", "--yes", "--agent", "claude-code"],
                         tmp / "home", data)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("The CLI itself is still installed", proc.stdout)
        self.assertTrue(data.exists(), "a plain reset keeps the data dir")

    def test_source_checkout_does_not_suggest_npm_uninstall(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-reset-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self._home_with_launcher(tmp)
        data = tmp / "data"
        data.mkdir()

        proc = self._run([ROOT / "bin" / "network-jobs", "reset", "--yes", "--data",
                          "--agent", "claude-code"], tmp / "home", data)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("source checkout", proc.stdout)
        self.assertNotIn("npm uninstall -g", proc.stdout)

    def test_launcher_line_omitted_when_no_launcher(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-reset-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        entry = self._npm_layout(tmp)
        (tmp / "home").mkdir()
        data = tmp / "data"
        data.mkdir()

        proc = self._run([entry, "reset", "--yes", "--data", "--agent", "claude-code"],
                         tmp / "home", data)

        self.assertIn("npm uninstall -g @hirefrank/network-jobs", proc.stdout)
        self.assertNotIn("rm -f", proc.stdout)

    def test_purge_hint_suppressed_when_purge_cache_passed(self):
        tmp = Path(tempfile.mkdtemp(prefix="nj-reset-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        entry = self._npm_layout(tmp)
        self._home_with_launcher(tmp)
        data = tmp / "data"
        data.mkdir()

        proc = self._run([entry, "reset", "--yes", "--data", "--purge-cache",
                          "--agent", "claude-code"], tmp / "home", data)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("--purge-cache", proc.stdout)
        self.assertIn("npm uninstall -g @hirefrank/network-jobs", proc.stdout)


if __name__ == "__main__":
    unittest.main()