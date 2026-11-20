"""Review app: a reviewer sees each proposed change with its evidence and approves or rejects it.

Approving is the only path to a CRM write (apply.py). Rejections are remembered, so the daily run never
asks again. Runs locally: `python -m bellhaven_sync serve`.
"""
from __future__ import annotations

import json
import logging

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from . import config
from .apply import StaleProposal, apply_proposal
from .crm import CRMError
from .proposals import KIND_LABEL, KIND_ORDER
from .store import Store

log = logging.getLogger(__name__)


def _load(row) -> dict:
    d = dict(row)
    d["payload"] = json.loads(row["payload_json"])
    d["result"] = json.loads(row["result_json"]) if row["result_json"] else None
    d["kind_label"] = KIND_LABEL.get(row["kind"], row["kind"])
    return d


def create_app(store: Store | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = "local-review-app"
    app.config["store"] = store or Store()
    app.jinja_env.filters["fromjson"] = json.loads

    def st() -> Store:
        return app.config["store"]

    @app.context_processor
    def globals_():
        counts = st().counts()
        return {"counts": counts, "crm_url": config.crm_browser_url, "KIND_LABEL": KIND_LABEL,
                "pending_total": counts.get("pending", 0) + counts.get("failed", 0)}

    def next_pending(skip_id: int | None = None):
        """The next proposal to review, in queue order, skipping the one on screen."""
        rows = [_load(r) for r in st().list(("pending",)) if r["id"] != skip_id]
        rows.sort(key=lambda r: (KIND_ORDER.index(r["kind"]), r["id"]))
        return rows[0] if rows else None

    @app.get("/")
    def queue():
        rows = [_load(r) for r in st().list(("pending", "failed"))]
        rows.sort(key=lambda r: (r["status"] != "failed", KIND_ORDER.index(r["kind"]), r["id"]))
        by_kind = {k: sum(1 for r in rows if r["kind"] == k) for k in KIND_ORDER}
        last = st().last_run()
        return render_template("queue.html", rows=rows, by_kind=by_kind,
                               last=json.loads(last["stats_json"]) if last else None,
                               last_at=last["finished_at"] if last else None)

    @app.get("/decided")
    def decided():
        rows = [_load(r) for r in st().list(("applied", "rejected", "approved", "obsolete"))]
        rows.sort(key=lambda r: r["decided_at"] or "", reverse=True)
        return render_template("decided.html", rows=rows)

    @app.get("/matches")
    def matches():
        last = st().last_run()
        rows = json.loads(last["classification_json"]) if last else []
        return render_template("matches.html", rows=rows, last_at=last["finished_at"] if last else None)

    @app.get("/activity")
    def activity():
        return render_template("activity.html", writes=st().writes(), runs=st().runs())

    @app.get("/p/<int:pid>")
    def detail(pid: int):
        row = st().get(pid)
        if row is None:
            abort(404)
        p = _load(row)
        return render_template("detail.html", p=p, writes=st().writes(pid), nxt=next_pending(pid))

    @app.post("/p/<int:pid>/approve")
    def approve(pid: int):
        row = st().get(pid)
        if row is None:
            abort(404)
        if row["status"] not in ("pending", "failed"):
            flash(f"Already {row['status']}.", "info")
            return redirect(url_for("detail", pid=pid))
        comment = request.form.get("comment", "")
        st().decide(pid, "approved", comment, json.loads(row["result_json"] or "null"))
        try:
            result = apply_proposal(st(), pid)
            st().decide(pid, "applied", comment, result)
            flash("Approved and written to the CRM.", "ok")
        except (StaleProposal, CRMError) as e:
            partial = json.loads(st().get(pid)["result_json"] or "{}")
            st().decide(pid, "failed", comment, {**partial, "error": str(e)})
            flash(f"Not applied: {e}", "error")
        return redirect(url_for("detail", pid=pid))

    @app.post("/p/<int:pid>/reject")
    def reject(pid: int):
        row = st().get(pid)
        if row is None:
            abort(404)
        if row["status"] not in ("pending", "failed"):
            flash(f"Already {row['status']}.", "info")
            return redirect(url_for("detail", pid=pid))
        st().decide(pid, "rejected", request.form.get("comment", ""))
        flash("Rejected. Nothing was written, and the daily run won't propose this again.", "ok")
        nxt = next_pending(pid)
        return redirect(url_for("detail", pid=nxt["id"]) if nxt else url_for("queue"))

    @app.post("/run")
    def run_now():
        from .pipeline import run
        try:
            stats = run(store=st())
            q = stats["queue"]
            flash(f"Run {stats['run_id']} done: {q['new']} new, {q['still_pending']} still pending, "
                  f"{q['already_decided']} already decided (skipped), {q['obsolete']} obsolete.", "ok")
        except Exception as e:  # surface the problem in the UI instead of a 500 page
            log.exception("run failed")
            flash(f"Run failed: {e}", "error")
        return redirect(url_for("queue"))

    return app
