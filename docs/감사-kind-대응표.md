# 마메치 감사 kind 대응표 (G7)

마메치 `~/.mametchi/logs/audit.jsonl` 의 kind 를 이 코어가 남기는 kind 와 대응시킨다.
원본은 `src/slack_cli_agent/observability/mametchi_kinds.py` 이고, `tests/unit/test_mametchi_kinds.py` 가 누락 0건을 검사한다.
마메치 kind 집합은 2026-11-07 audit.jsonl 과 마메치 코드의 `audit(kind=...)` 호출에서 집계했다.

## 대응하는 kind

| 마메치 kind | 코어 kind |
|---|---|
| `(kind 없음, 요청 행)` | `request` |
| `awaiting_reply` | `awaiting_reply` |
| `blocks_rejected` | `blocks_rejected` |
| `channel_join` | `channel_join` |
| `control_tag_leak` | `control_tag_leak` |
| `debug_trace_format` | `review_format` |
| `delegation_accepted` | `request` |
| `delegation_recorded` | `delegation_recorded` |
| `dm_probe` | `dm_probe` |
| `dm_redirect` | `dm_reply` |
| `dm_redirect_capped` | `dm_reply_capped` |
| `dm_redirect_failed` | `dm_reply_failed` |
| `empty_after_tags` | `empty_after_tags` |
| `empty_body` | `empty_after_tags` |
| `figurative_fix` | `figurative_fix` |
| `file_permalink_stripped` | `file_permalink_stripped` |
| `file_post_duplicate` | `tool_post_merged` |
| `format_review_format` | `review_format` |
| `gate_skipped` | `gate_skipped` |
| `generated_image_autopost` | `generated_image_autopost` |
| `generated_image_autopost_failed` | `generated_image_autopost_failed` |
| `late_addendum` | `late_addendum` |
| `late_addendum_shrank` | `rewrite_loss` |
| `missing_recipient` | `missing_recipient` |
| `missing_recipient_retry` | `missing_recipient_retry` |
| `notion_link_fixed` | `notion_link_fixed` |
| `origin_deleted` | `origin_deleted` |
| `peer_skipped` | `peer_skipped` |
| `peer_turn_limit` | `peer_turn_limit` |
| `plain_mention` | `plain_mention` |
| `post_failed` | `post_failed` |
| `postmortem_five_why` | `review_check_failed` |
| `postmortem_format` | `review_format` |
| `promise_without_watch` | `promise_without_watch` |
| `promise_without_watch_retry_failed` | `promise_without_watch_retry_failed` |
| `render_without_post` | `render_without_post` |
| `shadow_answered` | `shadow_answered` |
| `shadow_judged` | `shadow_judged` |
| `snippet_failed` | `csv_upload_failed` |
| `split` | `split` |
| `split_broken` | `split_broken` |
| `unregistered_invite` | `unregistered_invite` |
| `unregistered_mention` | `unregistered_mention` |
| `watch_superseded` | `watch_finished` |
| `watch_verdict` | `watch_verdict` |
| `welcome_failed` | `welcome_failed` |
| `welcome_posted` | `welcome_posted` |
| `wrong_addressee` | `wrong_addressee` |
| `wrong_addressee_stripped` | `wrong_addressee` |

## 이관하지 않은 kind

| 마메치 kind | 사유 |
|---|---|
| `board_closed` | 스레드 상태판은 이관하지 않는다 (Z1, 사용자 결정) |
| `snippet` | 긴 코드블록을 파일로 떼는 기능은 이관하지 않았다. csv 블록만 떼고 실패만 기록한다 |
| `stopped` | 마메치 현재 코드에 기록 위치가 없다. 2026-09 이전 행만 남아 있다 |
| `watch_verdict_recovered` | 판정 누락 시 판정만 재요청하는 기능은 이관하지 않았다. 누락은 watch_verdict verdict=missing 으로 남는다 |

## 이번에 추가한 코어 kind

`plain_mention`, `promise_without_watch`, `promise_without_watch_retry_failed`, `awaiting_reply`, `peer_turn_limit`, `peer_skipped`, `gate_skipped`, `notion_link_fixed`, `review_format`, `welcome_posted`, `welcome_failed`, `shadow_answered`, `shadow_judged` 를 추가했다.
`awaiting_reply`, `peer_skipped`, `gate_skipped`, `welcome_posted`, `shadow_answered`, `shadow_judged` 는 장애가 아닌 일상 기록이라 `BASELINE_KINDS` 에 넣었다.
