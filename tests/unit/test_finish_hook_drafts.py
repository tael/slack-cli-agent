from slack_cli_agent.guard import finish_hook
from slack_cli_agent.guard.finish_review import carried_over, draft_key


def test_drafts_round_trip_and_lookup(tmp_path):
    path = finish_hook._drafts_path(tmp_path, "s-1")
    assert finish_hook._read_drafts(path) == ()

    finish_hook._write_drafts(path, (draft_key("원천은 DATAMART.ORDERS 이고\n건수는 12건임"),))

    drafts = finish_hook._read_drafts(path)
    assert carried_over("원천은 DATAMART.ORDERS 이고 건수는 12건임 (x)", drafts)


def test_bad_drafts_file_reads_empty(tmp_path):
    path = finish_hook._drafts_path(tmp_path, "s-2")
    path.write_text("{", encoding="utf-8")

    assert finish_hook._read_drafts(path) == ()
