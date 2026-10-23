"""Release closure: a shipped build becomes per-reporter notifications plus one announcement.

`release()` drafts proposals (nothing is sent). After a human approved and `send` ran,
`finalize()` marks the notified threads `released` and records `fixed_in`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from hearback import templates
from hearback.config import Config
from hearback.ledger import Ledger
from hearback.queue import Proposal, Queue
from hearback.util import HearbackError, short, sid6

NOTIFY_KIND = "reply"
ALL_BUILDS = {"en": "Fixes", "zh": "修复内容"}
DEFAULT_DOWNLOAD = {"en": "the usual download location", "zh": "常用的下载位置"}


@dataclass
class ReleasePlan:
    version: str
    variants: list[str]
    proposals: list[Proposal] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = ["release %s: %d proposal(s) drafted for variants: %s"
               % (self.version, len(self.proposals), ", ".join(self.variants) or "(all)")]
        out += ["  " + p.id + "  " + p.kind + "  " + (sid6(p.thread) if p.thread else "-") for p in self.proposals]
        out += ["  skipped %s: %s" % (sid6(t), why) for t, why in self.skipped]
        out += ["  note: " + n for n in self.notes]
        return out


@dataclass
class FinalizeReport:
    version: str
    finalized: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)    # drafted, not sent yet
    problems: list[str] = field(default_factory=list)   # failed or rejected notifications

    def lines(self) -> list[str]:
        out = ["finalize %s: %d marked released, %d already, %d waiting to be sent, %d problem(s)"
               % (self.version, len(self.finalized), len(self.already), len(self.waiting), len(self.problems))]
        out += ["  released: " + sid6(t) for t in self.finalized]
        out += ["  waiting:  " + sid6(t) for t in self.waiting]
        out += ["  problem:  " + p for p in self.problems]
        return out


def variant_info(rec: dict, cfg: Config) -> list[str]:
    """Variants we actually know a report concerns: triage.variants, else inferred from the thread's tags.
    Empty when there is no information at all."""
    vs = [v for v in (rec.get("triage") or {}).get("variants") or []]
    if not vs:
        tags = {t.lower() for t in rec.get("tags", [])}
        vs = [n for n, v in cfg.variants.items() if v.tag and v.tag.lower() in tags]
    return vs


def thread_variants(rec: dict, cfg: Config) -> list[str]:
    """Variants a report concerns: what variant_info knows, else every configured variant."""
    return variant_info(rec, cfg) or list(cfg.variants)


def propose_status_tags(queue: Queue, cfg: Config, rec: dict, thread: str, old: str | None, new: str,
                        actor: str, why: str = "", meta: dict | None = None) -> Proposal | None:
    """Mirror a status change as a tag swap using [status_tags]. None if the config maps nothing."""
    tags = rec.get("tags", [])
    add = [cfg.status_tags[new]] if new in cfg.status_tags and cfg.status_tags[new] not in tags else []
    remove = [cfg.status_tags[old]] if old and old != new and cfg.status_tags.get(old) in tags else []
    if not add and not remove:
        return None
    return queue.propose("tags", actor=actor, thread=thread, add=add, remove=remove,
                         why=why or "status %s -> %s" % (old, new), meta=meta)


def _label(cfg: Config, name: str) -> str:
    v = cfg.variants.get(name)
    return (v.tag if v and v.tag else name)


def _download(cfg: Config, name: str) -> str:
    v = cfg.variants.get(name)
    return v.download if v and v.download else DEFAULT_DOWNLOAD.get(cfg.language, DEFAULT_DOWNLOAD["en"])


def release(version: str, variants: list[str], cfg: Config, ledger: Ledger, queue: Queue, actor: str) -> ReleasePlan:
    """Draft one notification per fixed-pending-release thread in these variants, plus an announcement."""
    version = version.strip()
    if not version:
        raise HearbackError("release needs a version")
    unknown = [v for v in variants if cfg.variants and v not in cfg.variants]
    if unknown:
        raise HearbackError("unknown variant(s): %s. Configured: %s" % (", ".join(unknown),
                                                                         ", ".join(cfg.variants) or "(none)"))
    lang, tdir = cfg.language, cfg.templates_dir
    plan = ReleasePlan(version, list(variants))
    data = ledger.load()
    already = {p.thread for p in queue.list() if p.meta.get("release") == version and p.kind == NOTIFY_KIND
               and p.state != "rejected"}
    selected: list[tuple[str, dict, list[str]]] = []
    unknown_variant: list[str] = []
    for tid in sorted(data["threads"], key=int):
        rec = data["threads"][tid]
        tri = rec.get("triage") or {}
        if tri.get("status") != "fixed-pending-release":
            continue
        if variants and not variant_info(rec, cfg):
            # --variant X was given: never guess which build a report concerns
            plan.skipped.append((tid, "no variant info"))
            unknown_variant.append(tid)
            continue
        affected = [v for v in thread_variants(rec, cfg) if not variants or v in variants]
        if variants and not affected:
            plan.skipped.append((tid, "fix is for other variants"))
            continue
        if tid in already:
            plan.skipped.append((tid, "already drafted for %s" % version))
            continue
        selected.append((tid, rec, affected))
    if unknown_variant:
        plan.notes.append("%d thread(s) have no variant info and were NOT notified (set triage `variants`, "
                          "then run the release again): %s" % (len(unknown_variant), ", ".join(unknown_variant)))

    for tid, rec, affected in selected:
        tri = rec.get("triage") or {}
        note = tri.get("release_note") or tri.get("fix") or ""
        blocks = "\n".join(templates.render("variant_block", lang, tdir, variant_name=templates.trusted(_label(cfg, v)),
                                            download=templates.trusted(_download(cfg, v))) for v in affected)
        text = templates.render("release_notify", lang, tdir, author=rec.get("author", ""), title=rec.get("title", ""),
                                version=version,
                                fix_line=templates.trusted(templates.sanitize(note, 300) + "\n") if note else "",
                                variant_blocks=templates.trusted("\n" + blocks + "\n") if blocks else "")
        meta = {"release": version, "variants": affected}
        plan.proposals.append(queue.propose(
            NOTIFY_KIND, actor=actor, thread=tid, text=text, meta=meta,
            why="release %s: cluster %s is fixed" % (version, tri.get("cluster") or "-")))
        tag_p = propose_status_tags(queue, cfg, rec, tid, "fixed-pending-release", "released", actor,
                                    why="release %s shipped" % version, meta=meta)
        if tag_p:
            plan.proposals.append(tag_p)

    if not selected:
        plan.notes.append("no fixed-pending-release threads match; nothing to announce")
        return plan
    if not cfg.announce_channel_id:
        plan.notes.append("announce_channel_id not set: no announcement drafted")
    else:
        groups: dict[str, dict[str, list]] = {}  # variant -> cluster -> [summary, count]
        for tid, rec, affected in selected:
            tri = rec.get("triage") or {}
            cluster = tri.get("cluster") or short(rec.get("title", ""), 40)
            for v in affected or [""]:
                g = groups.setdefault(v, {})
                entry = g.setdefault(cluster, [tri.get("release_note") or tri.get("fix") or "", 0])
                entry[1] += 1
        parts = []
        for v, clusters in groups.items():
            items = "\n".join(templates.render("release_announce_item", lang, tdir, cluster=c,
                                               summary=templates.safe(s or "-"), count=n)
                              for c, (s, n) in clusters.items())
            parts.append(templates.render(
                "release_announce_group", lang, tdir,
                variant_name=templates.trusted(_label(cfg, v) if v else ALL_BUILDS.get(lang, "Fixes")),
                download=templates.trusted(_download(cfg, v) if v else ""), items=templates.trusted(items)))
        text = templates.render("release_announce", lang, tdir, version=version,
                                groups=templates.trusted("\n\n".join(parts)))
        plan.proposals.append(queue.propose(
            "post", actor=actor, thread=cfg.announce_channel_id, text=text,
            meta={"release": version, "announce": True}, why="announce release %s" % version))
    if ledger:
        ledger.event(actor, "release", None, {"version": version, "proposals": len(plan.proposals)})
    return plan


def finalize(version: str, ledger: Ledger, queue: Queue, actor: str) -> FinalizeReport:
    """After the notifications were sent: threads become `released` with fixed_in = version. Idempotent."""
    rep = FinalizeReport(version)
    notes = [p for p in queue.list() if p.meta.get("release") == version and p.kind == NOTIFY_KIND]
    if not notes:
        raise HearbackError("no notifications were drafted for release %s (run `hearback release %s` first)"
                            % (version, version))
    data = ledger.load()
    for p in notes:
        rec = data["threads"].get(p.thread) or {}
        tri = rec.get("triage") or {}
        if p.state == "sent":
            if tri.get("status") == "released" and tri.get("fixed_in") == version:
                rep.already.append(p.thread)
            else:
                ledger.update_triage(p.thread, {"status": "released", "fixed_in": version}, actor=actor,
                                     force_claim=True)   # a system step after the notification went out
                rep.finalized.append(p.thread)
        elif p.state in ("pending", "approved"):
            rep.waiting.append(p.thread)
        else:
            rep.problems.append("%s %s: notification is %s%s" % (sid6(p.thread), p.id, p.state,
                                                                 " (" + short(p.error, 80) + ")" if p.error else ""))
    ledger.event(actor, "release_finalize", None, {"version": version, "released": len(rep.finalized)})
    return rep
