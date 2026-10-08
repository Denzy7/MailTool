"""The Fetch job: one pass over the mailbox for a date range that can
  * save every attachment into <library>/<date>/<sender> - <subject>/
  * merge an email's PDF attachments into MERGED_<sender>.pdf
  * render an EMAILINFO_<sender>.pdf print of the email
  * export a CSV of the messages
and records every message in the library index (exact UID), so sorting and
printing later never have to search the mailbox again.

Only this job's own thread talks IMAP (a connection can't be shared). EMAILINFO
rendering runs on N worker threads, each with its own headless browser."""
from __future__ import annotations

import csv
import os
import queue
import threading
from datetime import datetime, timezone

from mailtool.core import timeutil
from mailtool.core.util import sanitize_for_path, touch_path
from mailtool.fetch import emailinfo as ei
from mailtool.library.db import Library, now_iso
from mailtool.library.folders import claim_message_dir, unique_name
from mailtool.mail import bodystructure as bs
from mailtool.mail.imap import MailSession
from mailtool.mail.mime import (address_list, body_text_of, decode_mime_words, decode_part_payload,
                                extract_full, resolve_sender)
from email.utils import parseaddr

try:
    from pypdf import PdfWriter
    PYPDF = True
except Exception:
    PYPDF = False

CSV_FIELDS = ["Sender", "Email", "Date Received", "Subject", "Body", "UID", "Mailbox", "Message-ID"]


def account_key(acc):
    return ("%s@%s" % ((acc.get("username") or "").strip(), (acc.get("server") or "").strip())).lower()


def run_fetch(job, *, account, password, library_root, tz_text, start, end, opts, info):
    """opts: save_attachments, merge_pdfs, emailinfo, export_csv, csv_path, log_without_attachments.
    info: the emailinfo settings section. Returns a summary dict."""
    tz = timeutil.resolve_timezone(tz_text)
    lib = Library(library_root)
    acc_key = account_key(account)
    mailbox = (account.get("mailbox") or "INBOX").strip()
    save_atts = bool(opts.get("save_attachments"))
    want_info = bool(opts.get("emailinfo")) and ei.REPORTLAB
    if opts.get("emailinfo") and not ei.REPORTLAB:
        job.log("EMAILINFO PDFs need reportlab (pip install reportlab) - skipped.", "warn")

    mode = info.get("body_mode") or "print"
    if not ei.GRAPHICAL and mode != "text":
        if want_info:
            job.log("Printed/image email bodies need playwright + pillow - using plain text layout.", "warn")
        mode = "text"
    if mode == "print" and not ei.PYPDF:
        job.log("Printed mode needs pypdf - using image snapshot instead.", "warn")
        mode = "image"
    graphical = want_info and mode in ("print", "image")

    summary = {"messages": 0, "attachments": 0, "pdfs": 0, "pdf_failed": 0, "csv_rows": 0, "date_fallbacks": 0,
               "reply_to": 0, "library": library_root, "message_ids": []}
    stamp = datetime.now(tz).strftime("%m/%d/%Y, %H:%M")
    username = (account.get("username") or "").strip()
    left_tpl = (info.get("header_left") or "").strip() or username
    right_tpl = (info.get("header_right") or "").strip()

    # ---------------------------------------------------------------- render workers
    workers, n_workers = [], 0
    jobs_q = None
    lock = threading.Lock()
    browser_error = {"shown": False}
    if want_info:
        n_workers = max(1, int(info.get("workers") or 1))
        if graphical:
            avail = ei.available_memory_mb()
            per = ei.PRINT_WORKER_MEMORY_MB if mode == "print" else ei.WORKER_MEMORY_MB.get(int(info.get("quality") or 2), 700)
            if avail is not None:
                fits = max(1, int(avail * 0.7) // per)
                if n_workers > fits:
                    job.log("Only ~%d MB of memory free: using %d parallel render(s) instead of %d." % (
                        avail, fits, n_workers), "warn")
                    n_workers = fits
        else:
            n_workers = 1
        jobs_q = queue.Queue(maxsize=n_workers * 2)

        def worker():
            renderer = None
            if graphical:
                try:
                    renderer = ei.HtmlRenderer(ei.RENDER_WIDTH_PX, allow_remote_images=bool(info.get("remote_images")))
                    renderer.__enter__()
                except Exception as e:
                    renderer = None
                    with lock:
                        first = not browser_error["shown"]
                        browser_error["shown"] = True
                    if first:
                        job.log("Browser failed to start: %s" % str(e).strip().splitlines()[0][:200], "warn")
                        job.log("Using the text layout. Fix: run 'playwright install chromium' with the same "
                                "Python that runs MailTool.", "warn")
            try:
                while True:
                    item = jobs_q.get()
                    if item is None:
                        return
                    if job.cancelled:
                        continue
                    try:
                        ei.render_job(item, renderer, mode, int(info.get("quality") or 2), job.log)
                        if item["sent_dt"] is not None:
                            touch_path(item["pdf_path"], item["sent_dt"])
                        lib.set_fields(item["mid"], emailinfo=lib.rel(item["pdf_path"]))
                        with lock:
                            summary["pdfs"] += 1
                        job.log("  Email info: %s/%s" % (item["label"], os.path.basename(item["pdf_path"])))
                    except Exception as e:
                        with lock:
                            summary["pdf_failed"] += 1
                        job.log("  EMAILINFO failed for UID %s: %s" % (item["uid"], e), "error")
            finally:
                if renderer is not None:
                    try:
                        renderer.__exit__(None, None, None)
                    except Exception:
                        pass

    csv_rows = []
    dir_times = {}
    session = MailSession(account, password, job=job)
    try:
        session.connect(mailbox)
        uidvalidity = session.uidvalidity or ""
        job.log("Looking for mail from %s to %s (%s) ..." % (
            start.strftime("%d-%b-%Y %H:%M"), end.strftime("%d-%b-%Y %H:%M"), tz_text))
        uids = session.uids_in_range(start, end, tz)
        total = len(uids)
        job.log("%d message(s) in range." % total)
        job.progress(0, total)
        if total and want_info:
            if graphical:
                job.log("Starting %d headless browser(s) ..." % n_workers)
            for _ in range(n_workers):
                t = threading.Thread(target=worker, daemon=True)
                t.start()
                workers.append(t)

        for i, uid in enumerate(uids, 1):
            job.check()
            try:
                item = _download_one(job, session, lib, uid, uidvalidity, acc_key, mailbox, tz, library_root,
                                     save_atts, bool(opts.get("merge_pdfs")), graphical, want_info, opts, summary)
            except Exception as e:
                if job.cancelled:
                    raise
                job.log("  UID %s skipped: %s" % (uid, e), "error")
                item = None
            if item is not None:
                if item["sent_dt"] is not None and item["folder"]:
                    for d in (item["folder"], os.path.dirname(item["folder"])):
                        if d not in dir_times or item["sent_dt"] > dir_times[d]:
                            dir_times[d] = item["sent_dt"]
                csv_rows.append(item["csv"])
                if want_info:
                    values = {"UID": uid, "EMAIL": item["from_email"], "SUBJECT": item["subject"], "MAILBOX": mailbox,
                              "DATE": item["sent_dt"].strftime("%Y-%m-%d") if item["sent_dt"] else ""}
                    folder = item["folder"] or claim_message_dir(
                        library_root, item["date_folder"], item["folder_email"], item["subject"], mailbox, uid)
                    if not item["folder"]:
                        lib.set_fields(item["mid"], folder=lib.rel(folder))
                        if item["sent_dt"] is not None:
                            for d in (folder, os.path.dirname(folder)):
                                if d not in dir_times or item["sent_dt"] > dir_times[d]:
                                    dir_times[d] = item["sent_dt"]
                    jobs_q.put({
                        "mid": item["mid"], "uid": uid, "sent_dt": item["sent_dt"], "subject": item["subject"],
                        "from_display": item["from_display"], "to_display": item["to_display"],
                        "date_str": timeutil.format_long(item["sent_dt"]), "html": item["html"],
                        "body": item["body"], "attachments": item["att_list"], "stamp": stamp,
                        "pdf_path": os.path.join(folder, "EMAILINFO_%s.pdf" % sanitize_for_path(item["from_email"])),
                        "header_left": ei.expand_template(left_tpl, values),
                        "header_right": ei.expand_template(right_tpl, values),
                        "label": "%s/%s" % (item["date_folder"], os.path.basename(folder)),
                    })
            job.progress(i, total, "downloaded ")
        session.close()
    finally:
        session.close()
        if jobs_q is not None:
            for _ in workers:
                jobs_q.put(None)
            if workers:
                job.log("Waiting for email info PDFs to finish ...")
            for t in workers:
                t.join()
        for d, dt in dir_times.items():
            if os.path.isdir(d):
                touch_path(d, dt)
        if opts.get("export_csv") and opts.get("csv_path") and csv_rows:
            try:
                with open(opts["csv_path"], "w", newline="", encoding="utf-8-sig") as f:
                    w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                    w.writeheader()
                    w.writerows(csv_rows)
                summary["csv_rows"] = len(csv_rows)
                job.log("CSV: %d row(s) -> %s" % (len(csv_rows), opts["csv_path"]), "ok")
            except OSError as e:
                job.log("Could not write CSV: %s" % e, "error")
        lib.close()

    parts = ["%d message(s)" % summary["messages"]]
    if save_atts:
        parts.append("%d attachment(s) saved" % summary["attachments"])
    if want_info:
        parts.append("%d email info PDF(s)" % summary["pdfs"] + (", %d failed" % summary["pdf_failed"]
                                                                 if summary["pdf_failed"] else ""))
    if summary["date_fallbacks"]:
        parts.append("%d without a Received: header (used Date:)" % summary["date_fallbacks"])
    job.log("Done: " + ", ".join(parts) + ".", "ok")
    return summary


def _download_one(job, s, lib, uid, uidvalidity, acc_key, mailbox, tz, root, save_atts, merge, graphical,
                  want_info, opts, summary):
    hdr = s.fetch_headers(uid)
    if hdr is None:
        job.log("  UID %s: headers unavailable, skipped." % uid, "warn")
        return None
    rec, source = timeutil.get_received_datetime(hdr)
    sent_dt = timeutil.to_local(rec, tz)
    if source != "received":
        summary["date_fallbacks"] += 1
    subject = decode_mime_words(hdr.get("Subject", ""))
    from_name, from_email = parseaddr(decode_mime_words(hdr.get("From", "")))
    reply_name, reply_email = parseaddr(decode_mime_words(hdr.get("Reply-To", "")))
    s_name, s_email, used_reply = resolve_sender(hdr.get("From", ""), hdr.get("Reply-To", ""))
    if used_reply:
        summary["reply_to"] += 1
    from_email = from_email or "unknown_sender"
    from_display = "%s <%s>" % (from_name, from_email) if from_name else from_email
    to_addrs = address_list(hdr.get("To", ""))
    to_display = ", ".join(to_addrs)
    folder_email = s_email or from_email
    date_folder = sent_dt.strftime("%Y-%m-%d") if sent_dt is not None else "unknown_date"

    html = None
    plain = None
    files = []
    att_meta = []         # (name, size, part)
    if save_atts:
        msg = s.fetch_message(uid)
        if msg is None:
            job.log("  UID %s: could not download, skipped." % uid, "warn")
            return None
        plain, html, files = extract_full(msg)
        att_meta = [(n, len(p), None) for n, p in files]
    else:
        struct = s.fetch_structure(uid)
        body_parts = []
        inlined = set()
        if struct is not None:
            target = bs.pick_body_part(struct)
            if target is not None and target[1] == "PLAIN":
                raw = s.fetch_part(uid, target[0])
                if raw is not None:
                    plain = decode_part_payload(raw, target[3], target[2])
                    body_parts.append(target[0])
            hp = bs.pick_html_part(struct)
            if hp is not None and (graphical or plain is None):
                raw = s.fetch_part(uid, hp[0])
                if raw is not None:
                    html = decode_part_payload(raw, hp[3], hp[2])
                    body_parts.append(hp[0])
                    if graphical:
                        html, inlined = bs.inline_cid_images(html, struct, lambda p: s.fetch_part(uid, p))
            att_meta = [(n, sz, p) for n, sz, p in bs.find_attachment_parts(struct, tuple(body_parts),
                                                                             skip_parts=inlined)]
    body = body_text_of(plain, html)

    # ---- write attachments
    folder = None
    saved = []
    if files:
        folder = claim_message_dir(root, date_folder, folder_email, subject, mailbox, uid)
        taken = set()
        for name, payload in files:
            path = unique_name(folder, name, taken)
            with open(path, "wb") as f:
                f.write(payload)
            if sent_dt is not None:
                touch_path(path, sent_dt)
            saved.append((name, len(payload), path))
            summary["attachments"] += 1
            job.log("  Saved: %s/%s/%s" % (date_folder, os.path.basename(folder), os.path.basename(path)))
        if merge and PYPDF:
            pdfs = [p for _, _, p in saved if p.lower().endswith(".pdf")]
            if len(pdfs) >= 2:
                merged = os.path.join(folder, "MERGED_%s.pdf" % sanitize_for_path(folder_email))
                try:
                    w = PdfWriter()
                    for p in pdfs:
                        w.append(p)
                    with open(merged, "wb") as f:
                        w.write(f)
                    w.close()
                    if sent_dt is not None:
                        touch_path(merged, sent_dt)
                    job.log("  Merged %d PDF(s) -> %s" % (len(pdfs), os.path.basename(merged)))
                except Exception as e:
                    job.log("  PDF merge failed: %s" % e, "warn")
    elif opts.get("log_without_attachments") and save_atts:
        job.log("  (no attachments) [%s] %s - %s" % (date_folder, subject, folder_email))

    received_utc = rec.astimezone(timezone.utc).isoformat() if rec is not None and rec.tzinfo else (
        rec.isoformat() if rec else None)
    mid = lib.upsert_message({
        "account": acc_key, "mailbox": mailbox, "uidvalidity": uidvalidity, "uid": str(uid),
        "message_id": (hdr.get("Message-ID") or "").strip() or None,
        "received_local": sent_dt.strftime(timeutil.STORE_FMT) if sent_dt else None,
        "received_utc": received_utc, "time_source": source,
        "from_name": from_name or None, "from_email": from_email,
        "reply_name": reply_name or None, "reply_email": reply_email or None,
        "sender_name": s_name or None, "sender_email": s_email or from_email,
        "to_addrs": ", ".join(to_addrs) or None, "subject": subject, "body": body,
        "folder": lib.rel(folder) if folder else None,
        "attachments_saved": 1 if save_atts else None, "fetched_at": now_iso(),
    })
    if save_atts:
        lib.set_attachments(mid, [{"filename": n, "size": sz, "path": p} for n, sz, p in saved])
    elif att_meta:
        # keep any files a previous full fetch saved
        existing = {a["filename"]: a for a in lib.attachments(mid)}
        lib.set_attachments(mid, [{"filename": n, "size": sz, "part": p,
                                   "path": lib.abs(existing[n]["path"]) if n in existing and existing[n]["path"] else None}
                                  for n, sz, p in att_meta])
        if folder is None:
            row = lib.message(mid)
            if row and row["folder"]:
                folder = lib.abs(row["folder"])
    summary["messages"] += 1
    summary["message_ids"].append(mid)
    return {
        "mid": mid, "sent_dt": sent_dt, "subject": subject, "from_email": from_email, "from_display": from_display,
        "to_display": to_display or (s.acc.get("username") or ""), "folder_email": folder_email,
        "date_folder": date_folder, "folder": folder, "html": html if graphical else None, "body": body,
        "att_list": [(n, sz) for n, sz, _ in att_meta],
        "csv": {"Sender": s_name or s_email or "(unknown)", "Email": s_email or "(unknown)",
                "Date Received": sent_dt.strftime(timeutil.STORE_FMT) if sent_dt else "", "Subject": subject,
                "Body": body, "UID": uid, "Mailbox": mailbox, "Message-ID": (hdr.get("Message-ID") or "").strip()},
    }
