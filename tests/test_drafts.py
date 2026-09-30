from uriel_tools.drafts import Drafts
from uriel_tools.gating import Caller

MUM = Caller("mum", frozenset({"family"}), "r1")
DAD = Caller("dad", frozenset({"family"}), "r2")


def test_drafts_expire_and_are_bounded():
    now = [1000.0]
    drafts: Drafts[str] = Drafts(ttl_s=60, max_drafts=2, clock=lambda: now[0])
    first = drafts.add(MUM, "d")
    now[0] += 61
    assert drafts.get(MUM, first) is None  # expired
    a, b, c = drafts.add(MUM, "d"), drafts.add(MUM, "d"), drafts.add(MUM, "d")
    assert drafts.get(MUM, a) is None  # the oldest makes room
    assert drafts.get(MUM, b) and drafts.get(MUM, c)


def test_a_draft_belongs_to_whoever_made_it_and_remembers_its_result():
    drafts: Drafts[str] = Drafts()
    draft_id = drafts.add(MUM, "the draft")
    assert drafts.get(DAD, draft_id) is None
    stored = drafts.get(MUM, draft_id)
    assert (stored.draft, stored.result) == ("the draft", None)
    stored.result = {"done": True}
    assert drafts.get(MUM, draft_id).result == {"done": True}
