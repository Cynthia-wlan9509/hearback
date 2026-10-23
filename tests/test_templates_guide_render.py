"""Templates, the reporter guide, and the markdown dashboard."""
from __future__ import annotations

import pytest

from conftest import make_env
from hearback import templates
from hearback.guide import make_steps, parse_step, propose_guide
from hearback.render import render_dashboard
from hearback.util import HearbackError


def test_every_template_exists_in_both_languages():
    assert set(templates.EN) == set(templates.ZH)
    for name in templates.names():
        assert templates.render(name, "en").strip() and templates.render(name, "zh").strip()


def test_render_fills_placeholders_and_leaves_unknown_ones():
    out = templates.render("ack", "en", author="Mira", title="Oven")
    assert "Hi Mira" in out and '"Oven"' in out
    assert templates.render("duplicate", "en", author="X") .count("$original") == 1      # safe_substitute


def test_overrides_prefer_language_specific_files(tmp_path):
    (tmp_path / "ack.txt").write_text("generic $author", encoding="utf-8")
    assert templates.render("ack", "en", tmp_path, author="A") == "generic A"
    (tmp_path / "ack.zh.txt").write_text("中文 $author\n", encoding="utf-8")
    assert templates.render("ack", "zh", tmp_path, author="A") == "中文 A"
    assert templates.render("ack", "en", tmp_path, author="A") == "generic A"


def test_unknown_language_falls_back_to_english_and_unknown_template_is_an_error():
    assert templates.render("ack", "xx", author="A").startswith("Hi A")
    with pytest.raises(HearbackError, match="unknown template"):
        templates.render("nope", "en")


# ---------------------------------------------------------------- guide

def test_guide_is_a_pinned_newpost_proposal_from_the_template(tmp_path):
    e = make_env(tmp_path)
    p = propose_guide(e.queue, e.cfg, "agent:test", tags=["UI"])
    assert p.kind == "newpost" and p.pin is True and p.thread == e.forum and p.add == ["UI"]
    assert p.title == "How to report a bug (read this first)"
    assert "Free, Full" in p.text and "Steps to reproduce" in p.text and "Ctrl+V" in p.text
    e.cfg.language = "zh"
    q = propose_guide(e.queue, e.cfg, "agent:test", pin=False)
    assert q.pin is False and "复现步骤" in q.text and q.title.startswith("如何反馈问题")


def test_guide_can_be_sent_and_lands_in_the_ledger(tmp_path):
    e = make_env(tmp_path)
    e.sync()
    p = propose_guide(e.queue, e.cfg, "agent:test")
    rep = e.approve_and_send(p)
    assert not rep.failed and not rep.mismatches, rep.lines()
    tid = e.queue.get(p.id).result["thread_id"]
    assert e.fake.channels[tid]["flags"] & 2
    assert e.ledger.load()["threads"][tid]["triage"]["status"] == "guide"


def test_parse_step_handles_windows_paths_and_quotes():
    s = parse_step(r'C:\shots\a.png:120,80,30:"Tap the oven"')
    assert s == {"path": r"C:\shots\a.png", "x": 120, "y": 80, "r": 30, "caption": "Tap the oven"}
    assert parse_step("a.png:1,2,3:caption: with colon")["caption"] == "caption: with colon"
    with pytest.raises(HearbackError, match="bad step"):
        parse_step("a.png:12,80:no radius")


def test_make_steps_draws_a_red_circle_when_pillow_is_available(tmp_path):
    pil = pytest.importorskip("PIL.Image")
    from hearback.fake import make_png
    img = tmp_path / "s.png"
    img.write_bytes(make_png(200, 120, (255, 255, 255)))
    out = make_steps([{"path": str(img), "x": 100, "y": 60, "r": 20, "caption": "Tap here"},
                      {"path": str(img), "x": 20, "y": 20, "r": 10, "caption": "Then here"}],
                     tmp_path / "out" / "steps.png", cell_w=200, columns=2)
    im = pil.open(out).convert("RGB")
    assert im.width > 400 and im.height > 120
    reds = [(x, y) for x in range(im.width) for y in range(0, 130) if im.getpixel((x, y)) == (230, 30, 30)]
    assert reds, "the annotation circle should be drawn in red"


def test_make_steps_errors(tmp_path):
    pytest.importorskip("PIL")
    with pytest.raises(HearbackError, match="no steps"):
        make_steps([], tmp_path / "x.png")
    with pytest.raises(HearbackError, match="cannot open"):
        make_steps([{"path": str(tmp_path / "missing.png"), "x": 1, "y": 1, "r": 1, "caption": ""}], tmp_path / "x.png")


def test_guide_with_steps_attaches_the_strip(tmp_path):
    pytest.importorskip("PIL")
    from hearback.fake import make_png
    e = make_env(tmp_path)
    img = tmp_path / "s.png"
    img.write_bytes(make_png(100, 80))
    p = propose_guide(e.queue, e.cfg, "agent:test", steps=["%s:50,40,15:Press the button" % img])
    assert len(p.attachments) == 1 and p.attachments[0].endswith("steps.png")
    rep = e.approve_and_send(p)
    assert not rep.failed and not rep.mismatches


# ---------------------------------------------------------------- render

def test_dashboard_groups_by_status_with_cluster_table(seeded):
    e = seeded
    e.triage("oven", status="fixed-pending-release", cluster="oven-timer", root_cause="a | b", fix="reset", fixed_in=None)
    e.triage("flicker", status="fixed-pending-release", cluster="oven-timer")
    e.triage("dark", status="wontfix")
    md = render_dashboard(e.ledger.load(), e.cfg)
    assert md.startswith("# Bug desk") and "**4 threads**" in md
    assert "## Clusters" in md and "| oven-timer | 2 | fixed-pending-release |" in md
    assert "## fixed-pending-release (2)" in md and "## wontfix (1)" in md and "## new (1)" in md
    assert "a \\| b / reset" in md                                   # pipes escaped
    assert "https://discord.com/channels/%s/%s" % (e.fake.guild_id, e.threads["oven"]) in md
    assert md.index("## new") < md.index("## fixed-pending-release") < md.index("## wontfix")
    assert "| free |" in md                                            # variants inferred from the Free tag
