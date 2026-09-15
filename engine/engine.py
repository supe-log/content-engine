#!/usr/bin/env python3
"""content-lab engine — fetch, validate, and report on seeded source content.

Stdlib only, matching assessment-labs script conventions. See ../README.md for
the ground rules; the two that shape this file:

  * fetch is whitelist-only (seed manifests), ~1 req/s, identified User-Agent
  * the license gate is closed by default — a seed whose license is not in
    LICENSES simply cannot validate, and there is no "unknown" license

Commands:
  python3 engine.py fetch    --domain ela-writing [--band g11-12-ap-lang] [--only seedId] [--force]
  python3 engine.py validate --domain ela-writing [--band ...] [--only seedId]
  python3 engine.py report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

LAB_ROOT = Path(__file__).resolve().parent.parent
SEEDS_DIR = LAB_ROOT / "seeds"
RAW_DIR = LAB_ROOT / "raw"
VALIDATED_DIR = LAB_ROOT / "validated"
REPORTS_DIR = LAB_ROOT / "reports"
PROVENANCE_JSONL = LAB_ROOT / "provenance.jsonl"
PROVENANCE_MD = LAB_ROOT / "provenance.md"

USER_AGENT = "SuperBuilders-ContentLab/0.1 (curriculum research; low-volume seeded fetches)"
FETCH_DELAY_S = 1.0

# Shippable licenses ONLY. NC and unknown are unrepresentable by design:
# Alpha is tuition-funded (NC-unsafe today) and commercial release is possible
# later. `requires` names the extra seed field each license demands.
LICENSES = {
    "public-domain-pre1931": {"requires": "year<=1930"},
    "public-domain-us-gov": {"requires": "gov-host-or-rationale"},
    "public-domain-expired": {"requires": "rationale"},
    "cc-by-4.0": {"requires": "attribution"},
    "licensed-explicit": {"requires": "licenseEvidence"},
}

TARGET_USES = {
    # min words the RAW cleaned text must carry to be worth reviewing.
    # Excerpting to app length is a later, reviewed stage.
    "passage": 200,
    "instructional": 300,
    "synthesis_source": 80,
    "facts_source": 80,
}

LANDING_SHAPES = {
    "passage_box",
    "annotated_passage",
    "source_texts",
    "topic_facts",
    "instructional_prose",
    "synthesis_source",
    "vocab_term",
}

SEED_REQUIRED = [
    "seedId", "source", "sourceRef", "title", "author", "license",
    "licenseRationale", "targetUse", "domain", "gradeBand", "skills",
    "landingShapes",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get(url: str, timeout: int = 30) -> tuple[bytes, str]:
    """GET with the lab UA. Returns (body, final_url)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.geturl()


# ---------------------------------------------------------------- seeds

def load_seeds(domain: str | None, band: str | None, only: str | None) -> list[dict]:
    seeds: list[dict] = []
    errors: list[str] = []
    for path in sorted(SEEDS_DIR.rglob("*.jsonl")):
        for line_no, line in enumerate(path.read_text().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            seed = json.loads(line)
            missing = [k for k in SEED_REQUIRED if k not in seed]
            if missing:
                errors.append(f"{path.name}:{line_no} missing {missing}")
                continue
            seeds.append(seed)
    if errors:
        sys.exit("FAIL seed manifest errors:\n  " + "\n  ".join(errors))
    ids = [s["seedId"] for s in seeds]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        sys.exit(f"FAIL duplicate seedIds: {sorted(dupes)}")
    if domain:
        seeds = [s for s in seeds if s["domain"] == domain]
    if band:
        seeds = [s for s in seeds if s["gradeBand"] == band]
    if only:
        seeds = [s for s in seeds if s["seedId"] == only]
    return seeds


# ---------------------------------------------------------------- html→text

class _TextExtractor(HTMLParser):
    """Crude but dependency-free: keeps block-level text, drops chrome."""

    SKIP = {"script", "style", "nav", "header", "footer", "aside", "form",
            "button", "noscript", "svg", "select", "iframe"}
    BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
             "figcaption", "td", "div", "section", "article", "br"}
    # Page chrome by class/id: Wikisource header/footer/license banners,
    # MediaWiki navboxes and edit links, generic no-print wrappers.
    SKIP_CLASSES = {"ws-header", "ws-footer", "ws-noexport", "headertemplate",
                    "noprint", "navbox", "catlinks", "printfooter",
                    "mw-editsection", "licensecontainer", "mw-references-wrap",
                    "sistersitebox", "usa-banner", "usa-nav", "breadcrumb"}
    SKIP_IDS = {"headertemplate", "headercontainer", "footercontainer", "catlinks"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skip_depth = 0
        # Class-based skip: remember the opening tag and count same-tag
        # nesting so we exit exactly at its matching close tag.
        self._cskip_tag: str | None = None
        self._cskip_depth = 0

    def handle_starttag(self, tag, attrs):
        if self._cskip_tag:
            if tag == self._cskip_tag:
                self._cskip_depth += 1
            return
        a = dict(attrs)
        classes = {c.lower() for c in (a.get("class") or "").split()}
        if classes & self.SKIP_CLASSES or (a.get("id") or "").lower() in self.SKIP_IDS:
            self._cskip_tag, self._cskip_depth = tag, 1
            return
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if self._cskip_tag:
            if tag == self._cskip_tag:
                self._cskip_depth -= 1
                if self._cskip_depth == 0:
                    self._cskip_tag = None
            return
        if tag in self.SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self.BLOCK:
            self.chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0 and self._cskip_tag is None:
            self.chunks.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = "".join(p.chunks)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(ln.strip() for ln in text.splitlines()).strip()


# ---------------------------------------------------------------- adapters

@dataclass
class Fetched:
    text: str
    url_used: str
    adapter_meta: dict


GUTENBERG_START = re.compile(r"\*\*\* ?START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\*\*\*")
GUTENBERG_END = re.compile(r"\*\*\* ?END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*\*\*\*")


def fetch_gutenberg(seed: dict) -> Fetched:
    url = f"https://www.gutenberg.org/ebooks/{seed['sourceRef']}.txt.utf-8"
    body, final_url = http_get(url)
    raw = body.decode("utf-8", errors="replace")

    # Verify against the PG header before stripping it — a wrong ebook id must
    # fail loudly, not ship the wrong book.
    header_match = GUTENBERG_START.search(raw)
    footer_match = GUTENBERG_END.search(raw)
    if not header_match or not footer_match:
        raise ValueError("gutenberg START/END markers not found — not a PG plain-text file?")
    header = raw[: header_match.start()]
    title_line = next((ln for ln in header.splitlines() if ln.lower().startswith("title:")), "")
    pg_title = title_line.split(":", 1)[1].strip() if ":" in title_line else ""
    want = seed["title"].lower()
    if pg_title and want not in pg_title.lower() and pg_title.lower() not in want:
        raise ValueError(f"title mismatch: seed={seed['title']!r} vs PG={pg_title!r}")

    text = raw[header_match.end(): footer_match.start()].strip()
    return Fetched(text=text, url_used=final_url,
                   adapter_meta={"pgTitle": pg_title, "boilerplateStripped": True})


def fetch_wikisource(seed: dict) -> Fetched:
    # action=parse, not TextExtracts: Wikisource mainspace pages are usually
    # transcluded from proofread Page: namespace, and extracts returns empty
    # for those. parse expands transclusions to full HTML.
    params = urllib.parse.urlencode({
        "action": "parse", "format": "json", "redirects": 1,
        "prop": "text|revid|displaytitle", "page": seed["sourceRef"],
    })
    url = f"https://en.wikisource.org/w/api.php?{params}"
    body, final_url = http_get(url)
    data = json.loads(body)
    if "error" in data:
        raise ValueError(f"wikisource: {data['error'].get('info', 'page not found')}")
    parsed = data["parse"]
    text = html_to_text(parsed["text"]["*"])
    if not text:
        raise ValueError(f"wikisource page parsed empty: {seed['sourceRef']!r}")
    return Fetched(
        text=text,
        url_used=f"https://en.wikisource.org/wiki/{urllib.parse.quote(parsed.get('title', seed['sourceRef']).replace(' ', '_'))}",
        adapter_meta={"revId": parsed.get("revid"),
                      "note": "source text only; Wikisource annotations (CC BY-SA) not licensed here"},
    )


def fetch_url(seed: dict) -> Fetched:
    url = seed["sourceRef"]
    host = urllib.parse.urlparse(url).hostname or ""
    if seed["license"] == "public-domain-us-gov" and not (
        host.endswith(".gov") or host.endswith(".mil")
    ):
        raise ValueError(f"us-gov license requires a .gov/.mil host, got {host!r}")
    body, final_url = http_get(url)
    raw = body.decode("utf-8", errors="replace")
    looks_html = "<html" in raw[:2000].lower() or "<!doctype" in raw[:200].lower()
    text = html_to_text(raw) if looks_html else raw.strip()
    return Fetched(text=text, url_used=final_url,
                   adapter_meta={"htmlExtracted": looks_html, "host": host})


ADAPTERS = {"gutenberg": fetch_gutenberg, "wikisource": fetch_wikisource, "url": fetch_url}


# ---------------------------------------------------------------- provenance

def upsert_provenance(row: dict) -> None:
    rows: dict[str, dict] = {}
    if PROVENANCE_JSONL.exists():
        for line in PROVENANCE_JSONL.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                rows[r["seedId"]] = r
    rows[row["seedId"]] = row
    ordered = [rows[k] for k in sorted(rows)]
    PROVENANCE_JSONL.write_text("".join(json.dumps(r) + "\n" for r in ordered))

    lines = [
        "# Provenance — content-lab (regenerated by engine.py, do not hand-edit)",
        "",
        "Every fetched source, one row per seed, written at download time.",
        "",
        "| seedId | title | author | year | source | license | retrieved | sha256 (12) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in ordered:
        lines.append(
            f"| {r['seedId']} | {r['title']} | {r['author']} | {r.get('year', '—')} "
            f"| {r['source']} | {r['license']} | {r['retrievedAt'][:10]} | {r['sha256'][:12]} |"
        )
    PROVENANCE_MD.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------- commands

def cmd_fetch(seeds: list[dict], force: bool) -> int:
    failures = 0
    for seed in seeds:
        out_dir = RAW_DIR / seed["domain"] / seed["seedId"]
        raw_path = out_dir / "raw.txt"
        if raw_path.exists() and not force:
            print(f"skip  {seed['seedId']} (raw exists; --force to refetch)")
            continue
        adapter = ADAPTERS.get(seed["source"])
        if adapter is None:
            print(f"FAIL  {seed['seedId']}: unknown source {seed['source']!r}")
            failures += 1
            continue
        try:
            fetched = adapter(seed)
        except Exception as error:  # noqa: BLE001 — per-seed isolation is the point
            print(f"FAIL  {seed['seedId']}: {error}")
            failures += 1
            time.sleep(FETCH_DELAY_S)
            continue
        # Normalize line endings BEFORE hashing: read_text() applies universal
        # newlines, so a CRLF source (Gutenberg) would hash differently on
        # re-read and trip the parity check.
        fetched.text = fetched.text.replace("\r\n", "\n").replace("\r", "\n")
        # Optional human-set trim markers for pages whose chrome survives
        # extraction: textStart keeps from its first occurrence, textEnd cuts
        # at its last. A marker that fails to match is a hard per-seed error —
        # silent full-page text must not masquerade as a trimmed transcript.
        if seed.get("textStart"):
            idx = fetched.text.find(seed["textStart"])
            if idx == -1:
                print(f"FAIL  {seed['seedId']}: textStart marker not found")
                failures += 1
                time.sleep(FETCH_DELAY_S)
                continue
            fetched.text = fetched.text[idx:]
        if seed.get("textEnd"):
            idx = fetched.text.rfind(seed["textEnd"])
            if idx == -1:
                print(f"FAIL  {seed['seedId']}: textEnd marker not found")
                failures += 1
                time.sleep(FETCH_DELAY_S)
                continue
            fetched.text = fetched.text[:idx].rstrip()
        sha = hashlib.sha256(fetched.text.encode("utf-8")).hexdigest()
        out_dir.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(fetched.text)
        record = {
            "seedId": seed["seedId"], "urlUsed": fetched.url_used, "sha256": sha,
            "retrievedAt": now_iso(), "adapterMeta": fetched.adapter_meta,
        }
        (out_dir / "fetch.json").write_text(json.dumps(record, indent=2) + "\n")
        upsert_provenance({
            "seedId": seed["seedId"], "title": seed["title"], "author": seed["author"],
            "year": seed.get("year"), "source": seed["source"], "sourceRef": seed["sourceRef"],
            "license": seed["license"], "licenseRationale": seed["licenseRationale"],
            "urlUsed": fetched.url_used, "sha256": sha, "retrievedAt": record["retrievedAt"],
        })
        words = len(fetched.text.split())
        print(f"ok    {seed['seedId']}: {words} words, sha {sha[:12]}")
        time.sleep(FETCH_DELAY_S)
    return failures


_VOWELS = re.compile(r"[aeiouy]+")


def _syllables(word: str) -> int:
    w = word.lower().strip(".,;:!?\"'()[]")
    if not w:
        return 0
    count = len(_VOWELS.findall(w))
    if w.endswith("e") and count > 1:
        count -= 1
    return max(count, 1)


def text_metrics(text: str) -> dict:
    words = text.split()
    sentences = max(len(re.findall(r"[.!?]+(?:\s|$)", text)), 1)
    syllables = sum(_syllables(w) for w in words)
    n = max(len(words), 1)
    fk = 0.39 * (n / sentences) + 11.8 * (syllables / n) - 15.59
    return {
        "wordCount": len(words),
        "sentenceCount": sentences,
        "fleschKincaidGrade": round(fk, 1),
        "replacementChars": text.count("�"),
        "excerptRequired": len(words) > 1200,
    }


def license_errors(seed: dict) -> list[str]:
    errs: list[str] = []
    lic = seed["license"]
    if lic not in LICENSES:
        return [f"license {lic!r} is not shippable (allowed: {sorted(LICENSES)})"]
    if not str(seed.get("licenseRationale", "")).strip():
        errs.append("licenseRationale is required")
    if lic == "public-domain-pre1931":
        year = seed.get("year")
        if not isinstance(year, int) or year > 1930:
            errs.append(f"pre-1931 license requires integer year <= 1930, got {year!r}")
    if lic == "cc-by-4.0" and not str(seed.get("attribution", "")).strip():
        errs.append("cc-by-4.0 requires an attribution string (surfaced in-app)")
    if lic == "licensed-explicit" and not str(seed.get("licenseEvidence", "")).strip():
        errs.append("licensed-explicit requires licenseEvidence (where the license lives)")
    return errs


def cmd_validate(seeds: list[dict]) -> int:
    failures: list[dict] = []
    passed = 0
    for seed in seeds:
        raw_path = RAW_DIR / seed["domain"] / seed["seedId"] / "raw.txt"
        fetch_path = RAW_DIR / seed["domain"] / seed["seedId"] / "fetch.json"
        errs = license_errors(seed)
        if seed["targetUse"] not in TARGET_USES:
            errs.append(f"unknown targetUse {seed['targetUse']!r}")
        unknown_shapes = set(seed["landingShapes"]) - LANDING_SHAPES
        if unknown_shapes:
            errs.append(f"unknown landingShapes {sorted(unknown_shapes)}")
        if not raw_path.exists():
            errs.append("no raw fetch on disk — run fetch first")
        if not errs:
            text = raw_path.read_text()
            fetch_record = json.loads(fetch_path.read_text())
            metrics = text_metrics(text)
            min_words = TARGET_USES[seed["targetUse"]]
            if metrics["wordCount"] < min_words:
                errs.append(f"too thin: {metrics['wordCount']} words < {min_words} for {seed['targetUse']}")
            if metrics["replacementChars"] > 5:
                errs.append(f"{metrics['replacementChars']} replacement chars — encoding problem")
            if hashlib.sha256(text.encode()).hexdigest() != fetch_record["sha256"]:
                errs.append("raw.txt no longer matches fetch.json sha256 — refetch, never hand-edit raw")
        if errs:
            failures.append({"seedId": seed["seedId"], "errors": errs})
            print(f"FAIL  {seed['seedId']}: " + "; ".join(errs))
            continue
        artifact = {
            "schema": "sourced-text.v1",
            "seedId": seed["seedId"],
            "title": seed["title"],
            "author": seed["author"],
            "year": seed.get("year"),
            "domain": seed["domain"],
            "gradeBand": seed["gradeBand"],
            "targetUse": seed["targetUse"],
            "skills": seed["skills"],
            "landingShapes": seed["landingShapes"],
            # Synthesis sources come in topic-clustered SETS (one FRQ = one
            # topic, 6-7 sources); the set id is how a set is reassembled.
            **({"sourceSet": seed["sourceSet"]} if seed.get("sourceSet") else {}),
            "provenance": {
                "source": seed["source"], "sourceRef": seed["sourceRef"],
                "urlUsed": fetch_record["urlUsed"], "sha256": fetch_record["sha256"],
                "retrievedAt": fetch_record["retrievedAt"],
                "license": seed["license"], "licenseRationale": seed["licenseRationale"],
                **({"attribution": seed["attribution"]} if seed.get("attribution") else {}),
            },
            "metrics": text_metrics(raw_path.read_text()),
            "text": raw_path.read_text(),
            # Same discipline as the curriculum map: nothing inferred or pulled
            # is promotable without a named human decision.
            "review": {"status": "unreviewed"},
        }
        out = VALIDATED_DIR / seed["domain"]
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{seed['seedId']}.json").write_text(json.dumps(artifact, indent=2) + "\n")
        passed += 1
        m = artifact["metrics"]
        print(f"ok    {seed['seedId']}: {m['wordCount']}w, FK {m['fleschKincaidGrade']}"
              + (", needs excerpting" if m["excerptRequired"] else ""))
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / "validation-failures.json").write_text(json.dumps(failures, indent=2) + "\n")
    print(f"\n{passed} validated, {len(failures)} failed → validated/ + reports/validation-failures.json")
    return len(failures)


def cmd_report() -> None:
    artifacts = [json.loads(p.read_text()) for p in sorted(VALIDATED_DIR.rglob("*.json"))]
    failures = []
    fp = REPORTS_DIR / "validation-failures.json"
    if fp.exists():
        failures = json.loads(fp.read_text())
    lines = [f"# Coverage report — {date.today().isoformat()}", ""]
    lines.append(f"{len(artifacts)} validated artifact(s), {len(failures)} validation failure(s).")
    by = {}
    for a in artifacts:
        key = (a["domain"], a["gradeBand"])
        by.setdefault(key, []).append(a)
    for (domain, band), items in sorted(by.items()):
        lines += ["", f"## {domain} / {band}", "",
                  "| seed | use | words | FK | license | skills |", "|---|---|---|---|---|---|"]
        for a in items:
            m = a["metrics"]
            lines.append(f"| {a['seedId']} | {a['targetUse']} | {m['wordCount']} "
                         f"| {m['fleschKincaidGrade']} | {a['provenance']['license']} "
                         f"| {', '.join(a['skills'])} |")
        uses = {}
        skills = {}
        for a in items:
            uses[a["targetUse"]] = uses.get(a["targetUse"], 0) + 1
            for s in a["skills"]:
                skills[s] = skills.get(s, 0) + 1
        lines.append("")
        lines.append("Coverage: " + ", ".join(f"{k}={v}" for k, v in sorted(uses.items())))
        lines.append("Skills: " + ", ".join(f"{k}={v}" for k, v in sorted(skills.items())))
    if failures:
        lines += ["", "## Failures", ""]
        for f in failures:
            lines.append(f"- **{f['seedId']}**: " + "; ".join(f["errors"]))
    REPORTS_DIR.mkdir(exist_ok=True)
    out = REPORTS_DIR / f"coverage-{date.today().isoformat()}.md"
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out}")


CURRICULUM_DIR = LAB_ROOT / "curriculum"


def cmd_gaps() -> int:
    """Measure the curriculum ladder: unresolved dependencies + content deficits.

    The ladder is every curriculum/*.json band ordered by ladderIndex. A skill
    may depend only on skills defined at its own rung or below; each skill's
    contentNeeds are checked against the validated corpus (matched on
    contentTags x targetUse). The output is a work list, largest deficit
    first — the same shape the consuming app's own audit produces.
    """
    bands = [json.loads(p.read_text()) for p in sorted(CURRICULUM_DIR.glob("*.json"))]
    if not bands:
        sys.exit("FAIL no curriculum bands in curriculum/")
    bands.sort(key=lambda b: b.get("ladderIndex", 0))
    band_of: dict[str, str] = {}
    index_of_band: dict[str, int] = {}
    for band in bands:
        index_of_band[band["bandId"]] = band.get("ladderIndex", 0)
        for s in band["skills"]:
            if s["skillId"] in band_of:
                sys.exit(f"FAIL skill {s['skillId']} defined in two bands")
            band_of[s["skillId"]] = band["bandId"]

    dep_issues: list[str] = []
    for band in bands:
        for s in band["skills"]:
            for dep in list(s.get("dependsOn", [])) + list(s.get("prerequisites", [])):
                if dep not in band_of:
                    dep_issues.append(
                        f"{band['bandId']}/{s['skillId']} depends on UNDEFINED skill {dep!r}")
                elif index_of_band[band_of[dep]] > band.get("ladderIndex", 0):
                    dep_issues.append(
                        f"{band['bandId']}/{s['skillId']} depends on {dep!r} defined ABOVE it "
                        f"({band_of[dep]}) — the ladder is out of order")

    corpus = [json.loads(p.read_text()) for p in sorted(VALIDATED_DIR.rglob("*.json"))]

    def have(tags: list[str], use: str) -> int:
        return sum(1 for a in corpus
                   if a["targetUse"] == use and set(a["skills"]) & set(tags))

    rows: list[dict] = []
    for band in bands:
        for s in band["skills"]:
            for need in s.get("contentNeeds", []):
                got = have(s.get("contentTags", []), need["targetUse"])
                rows.append({
                    "band": band["bandId"], "skill": s["skillId"],
                    "use": need["targetUse"], "need": need["minSources"],
                    "have": got, "gap": max(0, need["minSources"] - got),
                })
    rows.sort(key=lambda r: (-r["gap"], r["band"], r["skill"]))

    lines = [f"# Ladder gap report — {date.today().isoformat()}", ""]
    lines.append("Ladder: " + " → ".join(
        f"{b['bandId']} ({len(b['skills'])} skills, {b['status']})" for b in bands))
    lines += ["", "## Dependency integrity", ""]
    if dep_issues:
        lines += [f"- ❌ {i}" for i in dep_issues]
    else:
        lines.append("- ✅ every dependsOn/prerequisite resolves at or below its own rung "
                     f"({sum(len(b['skills']) for b in bands)} skills across {len(bands)} bands)")
    lines += ["", "## Content coverage vs. targets (largest deficit first)", "",
              "| band | skill | needs | have | gap |", "|---|---|---|---|---|"]
    for r in rows:
        mark = "❌" if r["gap"] else "✅"
        lines.append(f"| {r['band']} | {r['skill']} | {r['need']} × {r['use']} "
                     f"| {r['have']} | {mark} {r['gap']} |")
    open_gaps = [r for r in rows if r["gap"]]
    lines += ["", f"**{len(open_gaps)} open content gap(s)**, "
              f"{sum(r['gap'] for r in open_gaps)} source(s) short in total. "
              "G9 skills carry no contentNeeds here: their assessment gaps are the "
              "consuming app's own audit (bottleneck_skill findings), not source-text gaps."]
    REPORTS_DIR.mkdir(exist_ok=True)
    out = REPORTS_DIR / f"ladder-gaps-{date.today().isoformat()}.md"
    out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines[2:6]))
    print(f"...\n{len(open_gaps)} open gap(s), {len(dep_issues)} dependency issue(s) → {out}")
    return len(dep_issues)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fetch", "validate", "report", "gaps"])
    parser.add_argument("--domain")
    parser.add_argument("--band")
    parser.add_argument("--only")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.command == "report":
        cmd_report()
        return
    if args.command == "gaps":
        sys.exit(1 if cmd_gaps() else 0)
    seeds = load_seeds(args.domain, args.band, args.only)
    if not seeds:
        sys.exit("FAIL no seeds matched the filters")
    print(f"{len(seeds)} seed(s) selected")
    failures = cmd_fetch(seeds, args.force) if args.command == "fetch" else cmd_validate(seeds)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
