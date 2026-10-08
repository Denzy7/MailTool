"""The Sort job: assign emails to groups by keyword.

Input is either
  * the library (default) - every email is known exactly (UID), its body is in the
    index and its attachments are on disk, so nothing has to be searched for; or
  * a CSV (Sender, Email, Date Received, Subject, Body [, UID, Mailbox]) - the
    fallback for exports from elsewhere. Rows with a UID column are looked up
    exactly; others are found by subject/sender/time in IMAP or a .eml folder,
    and those lookups are cached.

Pass 1 matches subject + body. Pass 2 (only for misses) matches the text of
.pdf/.docx/.doc attachments, honouring the exclude/include filename filters.
Writes matched.csv / unmatched.csv (and *_uniq.csv when asked)."""
from __future__ import annotations

import csv
import os
import shutil
import tempfile
from datetime import datetime

from mailtool.core import timeutil
from mailtool.fetch.fetcher import account_key
from mailtool.library.db import Library, LookupCache
from mailtool.mail.imap import MailSession, closest_candidate
from mailtool.mail.localeml import LocalEmlSource
from mailtool.mail.mime import extract_full, get_reply_to_pair
from mailtool.sort import extract
from mailtool.sort.groups import compile_groups, match, whole_word_near_misses

MATCHED_FIELDS = ["Name", "Email", "Descriptive Name", "Group Name", "Date", "Subject"]
UNMATCHED_FIELDS = ["Name", "Email", "Subject", "Date Received", "Reason"]


class _Ctx:
    """Settings + lazily opened mail sources shared by both input modes."""

    def __init__(self, job, sopts, account, password, tz_text):
        self.job = job
        self.o = sopts
        self.account = account
        self.password = password
        self.tz = timeutil.resolve_timezone(tz_text)
        self.groups = compile_groups(sopts.get("groups") or [], sopts.get("whole_word", True),
                                     sopts.get("case_sensitive", False))
        self.precedence = sopts.get("precedence") or "keywords_first"
        self.exclude = sopts.get("excluded") or []
        self.include = sopts.get("included") or []
        self.offline = bool(sopts.get("offline"))
        self.search_attachments = bool(sopts.get("search_attachments", True))
        self.session = None
        self.session_error = None
        self.local = None
        self.local_error = None
        self.tmp = tempfile.mkdtemp(prefix="mailtool_sort_")

    def close(self):
        if self.session:
            self.session.close()
        if self.local:
            self.local.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- sources
    def imap(self, mailbox=None):
        """Connected MailSession with `mailbox` selected, or None (error remembered)."""
        if self.offline:
            return None
        if self.session is None and self.session_error is None:
            try:
                self.session = MailSession(self.account, self.password, job=self.job).connect(mailbox or False)
            except Exception as e:
                self.session_error = e
                self.session = None
                self.job.log("IMAP unavailable: %s - only cached/downloaded data will be used." % e, "error")
                return None
        if self.session is not None and mailbox and self.session.mailbox != mailbox:
            try:
                self.session.select(mailbox)
            except Exception as e:
                self.job.log("Could not open mailbox %s: %s" % (mailbox, e), "warn")
                return None
        return self.session

    def eml(self):
        if self.offline:
            return None
        if self.local is None and self.local_error is None:
            try:
                self.local = LocalEmlSource(self.o.get("local_folder"), job=self.job)
                self.job.log("Indexing local export folder %s ..." % self.o.get("local_folder"))
                self.local.connect()
            except Exception as e:
                self.local_error = e
                self.local = None
                self.job.log("Local folder unavailable: %s" % e, "error")
        return self.local

    # ---- matching
    def match_body(self, subject, body):
        return match("%s\n%s" % (subject or "", body or ""), self.groups, self.precedence)

    def texts_from_message(self, msg):
        """[(filename, text, decision)] for a downloaded email.message.Message."""
        _, _, files = extract_full(msg)
        out = []
        work = tempfile.mkdtemp(dir=self.tmp)
        for n, (name, payload) in enumerate(files):
            if not extract.searchable(name):
                continue
            decision = extract.filter_decision(name, self.exclude, self.include)
            if decision == "skip":
                out.append((name, None, decision))
                continue
            path = os.path.join(work, "%03d%s" % (n, os.path.splitext(name)[1].lower()))
            with open(path, "wb") as f:
                f.write(payload)
            try:
                text = extract.extract_text(path)
            except Exception as e:
                self.job.log("    ! %s: %s" % (name, e), "warn")
                text = ""
            out.append((name, text, decision))
        return out

    def match_texts(self, texts):
        """texts: [(filename, text, decision)] -> (group, term, stage, reason)."""
        skipped = [n for n, _, d in texts if d == "skip"]
        overridden = [n for n, _, d in texts if d == "override"]
        if overridden:
            self.job.log("    included despite exclude filter: %s" % ", ".join(overridden))
        if skipped:
            self.job.log("    excluded by filter: %s" % ", ".join(skipped))
        considered = [(n, t) for n, t, d in texts if d != "skip"]
        if not considered:
            return None, None, None, ("all attachments excluded by filter" if skipped
                                      else "no pdf/docx/doc attachments")
        for name, text in considered:
            g, term, stage = match(text or "", self.groups, self.precedence, scope="attach")
            if g:
                self.job.log("    + %s -> %s (%s: %s)" % (name, g.group_name, stage, term))
                return g, term, stage, None
            self.explain(name, text)
        return None, None, None, "no keyword or group name in attachment text"

    def explain(self, name, text):
        if not (text or "").strip():
            self.job.log("    no text extracted from %s (scanned / image-only file?)" % name, "warn")
            return
        if self.o.get("whole_word", True):
            near = whole_word_near_misses(text, self.groups, self.o.get("case_sensitive", False))
            if near:
                self.job.log("    hint: %s appear in %s only inside longer words - turn off 'Whole-word match' "
                             "to match them" % (", ".join(repr(k) for k in near), name), "warn")


def _write_outputs(job, out_dir, matched, unmatched, dedupe):
    os.makedirs(out_dir, exist_ok=True)
    paths = {}

    def write(name, fields, rows):
        p = os.path.join(out_dir, name)
        with open(p, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        paths[name] = p
        return p

    job.log("Wrote %d row(s) -> %s" % (len(matched), write("matched.csv", MATCHED_FIELDS, matched)), "ok")
    job.log("Wrote %d row(s) -> %s" % (len(unmatched), write("unmatched.csv", UNMATCHED_FIELDS, unmatched)), "ok")
    if dedupe:
        seen, mu = set(), []
        for r in matched:
            k = (r["Group Name"], (r["Email"] or "").strip().lower())
            if k not in seen:
                seen.add(k)
                mu.append(r)
        job.log("Wrote %d unique-per-group row(s) -> %s" % (len(mu), write("matched_uniq.csv", MATCHED_FIELDS, mu)),
                "ok")
        seen, uu = set(), []
        for r in unmatched:
            k = (r["Email"] or "").strip().lower()
            if k not in seen:
                seen.add(k)
                uu.append(r)
        job.log("Wrote %d unique unmatched row(s) -> %s" % (
            len(uu), write("unmatched_uniq.csv", UNMATCHED_FIELDS, uu)), "ok")
    return paths


# ============================================================================== library input
def run_sort_library(job, *, library_root, sopts, account, password, tz_text, ids=None, out_dir=None):
    ctx = _Ctx(job, sopts, account, password, tz_text)
    if not ctx.groups:
        raise RuntimeError("Add at least one group first.")
    lib = Library(library_root)
    try:
        if ids:
            rows = [r for r in (lib.message(i) for i in ids) if r is not None]
        else:
            start = end = None
            if sopts.get("library_from"):
                start = datetime.strptime(sopts["library_from"].strip(), timeutil.DATE_FMT).strftime("%Y-%m-%d 00:00:00")
            if sopts.get("library_to"):
                end = datetime.strptime(sopts["library_to"].strip(), timeutil.DATE_FMT).strftime("%Y-%m-%d 23:59:59")
            rows = lib.query(start=start, end=end, limit=10 ** 7)
        rows = sorted(rows, key=lambda r: (r["received_local"] or "", r["id"]))
        total = len(rows)
        job.log("Sorting %d email(s) from the library (%s first)." % (
            total, "group name" if ctx.precedence == "groupname_first" else "keywords"))
        job.progress(0, total)
        matched, unmatched = [], []
        need = []
        for i, r in enumerate(rows, 1):
            job.check()
            g, term, stage = ctx.match_body(r["subject"], r["body"])
            if g:
                lib.set_group(r["id"], g.group_name, term, stage)
                matched.append(_mrow(r, g))
                job.log("[%d/%d] %s | %r -> %s (%s: %s)" % (i, total, r["sender_email"], (r["subject"] or "")[:60],
                                                           g.group_name, stage, term))
            else:
                need.append(r)
            job.progress(i, total, "subject/body ")
        job.log("Subject/body: %d matched, %d need an attachment search." % (len(matched), len(need)))

        for i, r in enumerate(need, 1):
            job.check()
            reason = "no keyword in subject/body"
            g = term = stage = None
            atts = lib.attachments(r["id"])
            if ctx.search_attachments and atts:
                texts = []
                missing = []
                for a in atts:
                    if not extract.searchable(a["filename"]):
                        continue
                    decision = extract.filter_decision(a["filename"], ctx.exclude, ctx.include)
                    if decision == "skip":
                        texts.append((a["filename"], None, decision))
                        continue
                    if a["text_state"]:
                        texts.append((a["filename"], a["text"] or "", decision))
                        continue
                    path = lib.abs(a["path"]) if a["path"] else None
                    if path and os.path.isfile(path):
                        try:
                            text, state = extract.extract_text(path), "ok"
                        except Exception as e:
                            job.log("    ! %s: %s" % (a["filename"], e), "warn")
                            text, state = "", "error:%s" % str(e)[:100]
                        lib.set_attachment_text(a["id"], text, state if text.strip() or state != "ok" else "empty")
                        texts.append((a["filename"], text, decision))
                    else:
                        missing.append(a)
                if missing:
                    texts += _fetch_missing(ctx, lib, r, missing)
                if texts:
                    job.log("[attach %d/%d] %s - %d attachment(s)" % (i, len(need), r["sender_email"], len(texts)))
                    g, term, stage, why = ctx.match_texts(texts)
                    reason = why or reason
                else:
                    reason = "no pdf/docx/doc attachments"
            elif ctx.search_attachments:
                reason = "no keyword in subject/body, no attachments"
            if g:
                lib.set_group(r["id"], g.group_name, term, stage)
                matched.append(_mrow(r, g))
            else:
                lib.set_group(r["id"], None, None, None, reason)
                unmatched.append(_urow(r, reason))
                job.log("[attach %d/%d] %s | %r -> UNMATCHED (%s)" % (i, len(need), r["sender_email"],
                                                                     (r["subject"] or "")[:50], reason))
            job.progress(i, len(need), "attachments ")
        out = out_dir or sopts.get("out_dir") or os.path.join(library_root, "reports")
        paths = _write_outputs(job, out, matched, unmatched, sopts.get("dedupe"))
        job.log("Done: %d matched, %d unmatched." % (len(matched), len(unmatched)), "ok")
        return {"matched": len(matched), "unmatched": len(unmatched), "out_dir": out, "paths": paths}
    finally:
        lib.close()
        ctx.close()


def _fetch_missing(ctx, lib, r, missing):
    """Attachments the library knows about but never downloaded (EMAILINFO-only
    fetch): get the message by its exact UID and extract text. Cached in the library."""
    if ctx.offline or not ctx.search_attachments:
        return []
    if r["account"] != account_key(ctx.account):
        ctx.job.log("    attachments of %s are from another account - not downloaded" % r["sender_email"], "warn")
        return []
    s = ctx.imap(r["mailbox"])
    if s is None:
        return []
    if r["uidvalidity"] and s.uidvalidity and r["uidvalidity"] != s.uidvalidity:
        ctx.job.log("    mailbox UIDs changed since this email was fetched - re-fetch it to sort its attachments",
                    "warn")
        return []
    ctx.job.log("    downloading UID %s to read %d attachment(s) ..." % (r["uid"], len(missing)))
    msg = s.fetch_message(r["uid"])
    if msg is None:
        return []
    got = ctx.texts_from_message(msg)
    by_name = {}
    for name, text, decision in got:
        by_name.setdefault(name, (text, decision))
    out = []
    for a in missing:
        text, decision = by_name.get(a["filename"], (None, None))
        if decision is None:
            continue
        lib.set_attachment_text(a["id"], text or "", "ok" if (text or "").strip() else "empty")
        out.append((a["filename"], text or "", decision))
    return out


def _mrow(r, g):
    return {"Name": r["sender_name"] or r["sender_email"], "Email": r["sender_email"],
            "Descriptive Name": g.descriptive_name, "Group Name": g.group_name,
            "Date": r["received_local"] or "", "Subject": r["subject"] or ""}


def _urow(r, reason):
    return {"Name": r["sender_name"] or r["sender_email"], "Email": r["sender_email"],
            "Subject": r["subject"] or "", "Date Received": r["received_local"] or "", "Reason": reason}


# ============================================================================== CSV input
def _col(row, *names):
    for n in names:
        for key in row:
            if key and key.strip().lower() == n.lower():
                return (row[key] or "").strip()
    return ""


def run_sort_csv(job, *, csv_path, out_dir, sopts, account, password, tz_text, library_root=None):
    ctx = _Ctx(job, sopts, account, password, tz_text)
    if not ctx.groups:
        raise RuntimeError("Add at least one group first.")
    lib = Library(library_root) if library_root and os.path.isdir(library_root) else None
    use_cache = bool(sopts.get("use_cache", True))
    cache = LookupCache() if use_cache else None
    source = sopts.get("fallback_source") or "imap"
    src_label = "local export folder" if source == "local" else "IMAP"
    window_days = int(sopts.get("date_window_days") or 1)
    ts_window = float(sopts.get("timestamp_window_seconds") or 60)
    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        total = len(rows)
        job.log("Loaded %d row(s) from %s" % (total, os.path.basename(csv_path)))
        job.progress(0, total)
        matched, unmatched, need = [], [], []
        for idx, row in enumerate(rows, 1):
            job.check()
            item = {"name": _col(row, "Sender", "Name", "From"),
                    "email": _col(row, "Email", "Email Address", "From Email"),
                    "date": _col(row, "Date Received", "Date", "Received"),
                    "subject": _col(row, "Subject"),
                    "body": _col(row, "Body", "Message", "Content"),
                    "uid": _col(row, "UID"), "mailbox": _col(row, "Mailbox")}
            g, term, stage = ctx.match_body(item["subject"], item["body"])
            if g:
                matched.append(_crow_m(item, g, item["name"], item["email"]))
                job.log("[%d/%d] %s | %r -> %s (%s: %s)" % (idx, total, item["email"] or "(no email)",
                                                           item["subject"][:60], g.group_name, stage, term))
            else:
                need.append(item)
            job.progress(idx, total, "subject/body ")
        job.log("Subject/body: %d matched, %d need an attachment search." % (len(matched), len(need)))

        searchable = ctx.search_attachments and (not ctx.offline or use_cache)
        if ctx.offline and need:
            job.log("Offline mode: the %s is never contacted - %s." % (
                src_label, "matching from the cache only" if use_cache else "cache disabled, rest are unmatched"),
                "warn")
        hits = 0
        for i, item in enumerate(need, 1):
            job.check()
            name, addr = item["name"], item["email"]
            g = term = stage = None
            reason = "no keyword in subject/body" + ("" if searchable else " (attachment search off)")
            if searchable:
                texts, reply, why, cached = _csv_lookup(ctx, lib, cache, item, source, src_label, window_days,
                                                        ts_window)
                hits += cached
                if reply and reply[1]:
                    name, addr = reply[0] or name, reply[1]
                if texts is not None:
                    job.log("[attach %d/%d] %s - %d attachment(s)%s" % (i, len(need), addr, len(texts),
                                                                       " (cached)" if cached else ""))
                    g, term, stage, why2 = ctx.match_texts(texts) if texts else (None, None, None,
                                                                                "no pdf/docx/doc attachments")
                    reason = why2 or reason
                else:
                    reason = why
            if g:
                matched.append(_crow_m(item, g, name, addr))
                job.log("[attach %d/%d] -> %s" % (i, len(need), g.group_name))
            else:
                unmatched.append({"Name": name, "Email": addr, "Subject": item["subject"],
                                  "Date Received": item["date"], "Reason": reason})
                job.log("[attach %d/%d] %s | %r -> UNMATCHED (%s)" % (i, len(need), addr or "(no email)",
                                                                     item["subject"][:50], reason))
            job.progress(i, len(need), "attachments ")
        if cache is not None and need:
            job.log("Cache: %d/%d lookups served from cache." % (hits, len(need)))
        out = out_dir or sopts.get("out_dir") or os.path.dirname(os.path.abspath(csv_path))
        paths = _write_outputs(job, out, matched, unmatched, sopts.get("dedupe"))
        job.log("Done: %d matched, %d unmatched." % (len(matched), len(unmatched)), "ok")
        return {"matched": len(matched), "unmatched": len(unmatched), "out_dir": out, "paths": paths}
    finally:
        if lib:
            lib.close()
        if cache:
            cache.close()
        ctx.close()


def _crow_m(item, g, name, addr):
    return {"Name": name, "Email": addr, "Descriptive Name": g.descriptive_name, "Group Name": g.group_name,
            "Date": item["date"], "Subject": item["subject"]}


def _csv_lookup(ctx, lib, cache, item, source, src_label, window_days, ts_window):
    """-> (texts or None, (reply_name, reply_email) or None, reason_if_none, served_from_cache)"""
    job = ctx.job
    mailbox = item["mailbox"] or ctx.account.get("mailbox") or "INBOX"

    # 1. exact: a MailTool CSV carries the UID, and the library may already have the email
    if item["uid"] and lib is not None:
        r = lib.find(account_key(ctx.account), mailbox, None, item["uid"])
        if r is not None:
            texts = []
            for a in lib.attachments(r["id"]):
                if not extract.searchable(a["filename"]):
                    continue
                d = extract.filter_decision(a["filename"], ctx.exclude, ctx.include)
                if d == "skip":
                    texts.append((a["filename"], None, d))
                elif a["text_state"]:
                    texts.append((a["filename"], a["text"] or "", d))
                elif a["path"] and os.path.isfile(lib.abs(a["path"])):
                    try:
                        t = extract.extract_text(lib.abs(a["path"]))
                    except Exception:
                        t = ""
                    lib.set_attachment_text(a["id"], t, "ok" if t.strip() else "empty")
                    texts.append((a["filename"], t, d))
                else:
                    texts = None
                    break
            if texts is not None:
                rp = (r["reply_name"], r["reply_email"]) if r["reply_email"] else None
                return texts, rp, None, False

    key = LookupCache.key(source if not item["uid"] else "uid:" + mailbox, item["email"], item["subject"],
                          item["uid"] or item["date"])
    entry = cache.get(key) if cache is not None else None
    if entry is not None:
        rp = (entry.get("reply_name"), entry.get("reply_email")) if entry.get("reply_email") else None
        if entry.get("not_found"):
            return None, rp, (entry.get("reason") or "message not found") + " (cached)", True
        texts = []
        for a in entry.get("attachments", []):
            d = extract.filter_decision(a["filename"], ctx.exclude, ctx.include)
            texts.append((a["filename"], None if d == "skip" else a.get("text", ""), d))
        return texts, rp, None, True
    if ctx.offline:
        return None, None, "not in cache (offline mode - %s skipped)" % src_label, False

    msg = None
    reason = None
    found_uid = None
    if item["uid"] and source == "imap":
        s = ctx.imap(mailbox)
        if s is None:
            return None, None, "IMAP unavailable: %s" % ctx.session_error, False
        msg = s.fetch_message(item["uid"])
        found_uid = item["uid"]
        if msg is None:
            reason = "UID %s not found in %s" % (item["uid"], mailbox)
    else:
        src = ctx.imap(mailbox) if source == "imap" else ctx.eml()
        if src is None:
            err = ctx.session_error if source == "imap" else ctx.local_error
            return None, None, "%s unavailable: %s" % (src_label, err), False
        target = timeutil.parse_loose(item["date"])
        uids = src.search_fuzzy(item["subject"], item["email"], target, window_days)
        if not uids:
            reason = "message not found in %s" % src_label
        elif target is None:
            reason = "CSV date could not be parsed; refusing to guess between candidates"
        else:
            if len(uids) > 25:
                job.log("    %d candidates with that subject/sender; checking the first 25" % len(uids), "warn")
            found_uid, when, delta = closest_candidate(uids, target, ts_window, ctx.tz, src.message_times)
            if found_uid is None:
                reason = "no message within ±%gs of the CSV time" % ts_window
            else:
                job.log("    matched message at %s (%gs from CSV)" % (when, delta))
                msg = src.fetch_message(found_uid)
    if msg is None:
        if cache is not None:
            cache.put(key, {"not_found": True, "reason": reason or "message could not be read", "attachments": []})
        return None, None, reason or "message could not be read", False
    rp = get_reply_to_pair(msg)
    if rp:
        job.log("    Reply-To found: using %s <%s>" % (rp[0] or "(no name)", rp[1]))
    texts = ctx.texts_from_message(msg)
    if cache is not None:
        # store every attachment's text (even filtered ones) so filter changes don't need the server
        _, _, files = extract_full(msg)
        store = []
        known = {n: t for n, t, d in texts if t is not None}
        for n, _p in files:
            if not extract.searchable(n):
                continue
            if n not in known:
                known[n] = _text_of(ctx, n, _p)
            store.append({"filename": n, "text": known[n]})
        cache.put(key, {"attachments": store, "uid": str(found_uid),
                        "reply_name": rp[0] if rp else "", "reply_email": rp[1] if rp else ""})
    return texts, rp, None, False


def _text_of(ctx, name, payload):
    path = os.path.join(tempfile.mkdtemp(dir=ctx.tmp), "f" + os.path.splitext(name)[1].lower())
    with open(path, "wb") as f:
        f.write(payload)
    try:
        return extract.extract_text(path)
    except Exception:
        return ""
