# 제미나이 대화 기록 원시 바이트

Antigravity CLI 가 `conversations/<conversation_id>.db` 에 쓴 실제 바이트다.
2026-09-19 에 `~/.rei` 의 대화에서 떴다 (sca-ebp).

조립한 모형이 아니라 실물이라, CLI 판이 올라가 형식이 바뀌면 이것을 쓰는 시험이
깨진다. 그것이 이 파일들을 둔 이유다 — 형식 변경을 시험으로 알기 위해서다.

- `stepN_typeT.metadata.bin` — `steps.metadata` 의 앞 64바이트
- `stepN_typeT.payload.bin` — `steps.step_payload` 중 도구 호출 필드까지

`step_type` 은 파일 이름에 있다. 14=사용자, 15=모델, 132=도구 호출.
payload 에 내 작업 경로가 들어 있으나 비밀 값은 없다.
