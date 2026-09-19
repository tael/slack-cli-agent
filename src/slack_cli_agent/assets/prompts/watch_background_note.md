백그라운드로 돌릴 명령이 있으면 아래 세 줄을 그대로 실행한다. 결과 파일
이름은 이미 정해져 있다. 다른 이름을 쓰면 확인 턴이 그 결과를 찾지 못한다.

    mkdir -p .watch-out
    cat > .watch-out/<<WATCH_RUN_ID>>.sh <<'SCA_CMD_EOF'
    여기에 실제 명령을 그대로 쓴다
    SCA_CMD_EOF
    nohup sh -c 'bash -o pipefail .watch-out/<<WATCH_RUN_ID>>.sh; status=$?; printf "\n__SCA_WATCH_EXIT__=%s\n" "$status"; exit "$status"' > .watch-out/<<WATCH_RUN_ID>>.out 2>&1 &

지킬 것
- 명령을 `sh -c` 안에 직접 넣지 않는다. 명령에 따옴표가 들어가면 인용이
  깨져 종료 상태 표시가 안 남는다. 반드시 위처럼 스크립트 파일에 쓴다
- 마지막 줄은 글자 하나도 바꾸지 않는다. 종료 상태 표시를 시스템이 읽는다
- 검사를 여러 개 넣을 때는 실패를 모아 마지막에 그 상태로 끝낸다. 마지막
  명령만 성공하면 앞의 실패가 덮인다

        status=0
        첫째_검사 | tail -40 || status=1
        둘째_검사 || status=1
        exit "$status"

- 결과 파일 이름을 바꾸지 않는다. `.watch-out/<<WATCH_RUN_ID>>.out` 하나만 쓴다
- 이 턴에서 명령이 끝나기를 기다리지 않는다. 띄우고 바로 답한다
- 완료 여부는 시스템이 그 파일의 종료 상태 표시로 판정한다. 답변에 끝났다고
  쓰지 않는다
