"""Groups and keyword matching.

Each group has a name (e.g. 'G1/1/1/2026/01') and keyword lines:
    line 1   descriptive name - written to the CSVs; matched against subject/body AND attachments
    line 2   comma-separated keywords - subject/body AND attachments   (e.g. jan, january, jaanuary)
    line 3+  one keyword per line - subject/body only
The group name itself is a fallback match everywhere; precedence decides which goes first."""
from __future__ import annotations

import re


def build_pattern(keyword, whole_word=True, case_sensitive=False):
    kw = (keyword or "").strip()
    if not kw:
        return None
    escaped = re.escape(kw)
    if whole_word:
        # \b only works next to word characters; guard each end separately
        escaped = (r"\b" if re.match(r"\w", kw[0]) else "") + escaped + (r"\b" if re.match(r"\w", kw[-1]) else "")
    return re.compile(escaped, 0 if case_sensitive else re.IGNORECASE)


def split_keyword_lines(lines):
    """-> (descriptive_name, line2_keywords, line3plus_keywords)"""
    lines = [ln.strip() for ln in (lines or []) if ln and ln.strip()]
    if not lines:
        return "", [], []
    csv_kws = [k.strip() for k in lines[1].split(",") if k.strip()] if len(lines) > 1 else []
    return lines[0], csv_kws, lines[2:]


class Group:
    def __init__(self, group_name, keywords):
        self.group_name = group_name
        self.raw_keywords = [k for k in keywords or [] if k.strip()]
        self.descriptive, self.csv_keywords, self.extra_keywords = split_keyword_lines(self.raw_keywords)
        self.attach_patterns = []
        self.body_patterns = []
        self.group_name_pattern = None

    @property
    def descriptive_name(self):
        return self.descriptive

    @staticmethod
    def _compile_terms(terms, whole_word, case_sensitive):
        out, seen = [], set()
        for kw in terms:
            key = kw if case_sensitive else kw.lower()
            if not kw or key in seen:
                continue
            seen.add(key)
            pat = build_pattern(kw, whole_word, case_sensitive)
            if pat:
                out.append((kw, pat))
        return out

    def compile(self, whole_word, case_sensitive):
        self.group_name_pattern = build_pattern(self.group_name, whole_word, case_sensitive)
        desc = [self.descriptive] if self.descriptive else []
        self.attach_patterns = self._compile_terms(desc + self.csv_keywords, whole_word, case_sensitive)
        self.body_patterns = self._compile_terms(desc + self.csv_keywords + self.extra_keywords, whole_word,
                                                 case_sensitive)
        return self


def compile_groups(group_dicts, whole_word=True, case_sensitive=False):
    return [Group(g.get("group_name", ""), g.get("keywords", [])).compile(whole_word, case_sensitive)
            for g in group_dicts if g.get("group_name")]


def match_by_keywords(text, groups, scope="body"):
    if not text:
        return None, None
    attr = "attach_patterns" if scope == "attach" else "body_patterns"
    for g in groups:
        for kw, pat in getattr(g, attr):
            if pat.search(text):
                return g, kw
    return None, None


def match_by_group_name(text, groups):
    if not text:
        return None, None
    for g in groups:
        if g.group_name_pattern and g.group_name_pattern.search(text):
            return g, g.group_name
    return None, None


def match(text, groups, precedence="keywords_first", scope="body"):
    """-> (group, matched_term, stage) or (None, None, None).
    stage: 'keyword' | 'attachment keyword' | 'group name'"""
    kw_stage = "attachment keyword" if scope == "attach" else "keyword"
    order = [(lambda t, gs: match_by_keywords(t, gs, scope), kw_stage), (match_by_group_name, "group name")]
    if precedence == "groupname_first":
        order.reverse()
    for fn, stage in order:
        g, term = fn(text, groups)
        if g:
            return g, term, stage
    return None, None, None


def whole_word_near_misses(text, groups, case_sensitive=False, limit=5):
    """Attachment keywords that appear in text only inside longer words
    ('jaan' in 'jaan2026') - shown as a hint when whole-word matching fails."""
    if not text:
        return []
    flags = 0 if case_sensitive else re.IGNORECASE
    found = []
    for g in groups:
        for kw, _ in g.attach_patterns:
            if kw not in found and re.search(re.escape(kw), text, flags):
                found.append(kw)
                if len(found) >= limit:
                    return found
    return found
