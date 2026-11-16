"use strict";

// Every user-visible string of the console lives in one dictionary per language
// (P14: console.ui_language, `en` or `ko`). Both dictionaries have the same keys;
// tests/test_console_staging.py checks that. Use double-quoted strings only here.
const UI_TEXT = Object.freeze({
  en: Object.freeze({
    title: "Dispatch console",
    navLabel: "Views",
    tabQueue: "Triage",
    tabLanes: "Lanes",
    tabService: "Service",
    tabProposals: "Scope",
    proposalsTitle: (count, min) => count + " scope proposal(s); suggested after " + min + " consistent event(s)",
    proposalsEmpty: "No open or suggested scope proposals.",
    confirm: "Confirm",
    reject: "Reject",
    defer: "Defer",
    conflict: "conflict",
    effective: "in effect",
    imported: "imported",
    current: (lane, source) => lane + " (" + source + ")",
    proposalDone: (status) => "Proposal " + status,
    learnedTitle: "Learned scope",
    learnedEmpty: "No confirmed scope overrides in the ledger.",
    exportButton: "Export YAML",
    exportHint: "Merge into the instance config; the ledger override is already in effect.",
    excludeChannel: "Exclude channel",
    excludeChannelTitle: "Exclude every post from this channel: closes its open items and stops routing it (undo in the Scope tab)",
    notMember: "not a member",
    notMemberTitle: "The membership cache says you are not a member of this channel",
    channelExcluded: (channel, count) => "Channel " + channel + " excluded; " + count + " open item(s) closed. Undo in the Scope tab",
    exclusionsTitle: "Excluded channels (ledger)",
    exclusionsEmpty: "No channel exclusions in the ledger.",
    exportExclusions: "Export excluded_channels YAML",
    undo: "Undo",
    undoDone: (channel) => "Channel exclusion ended: " + channel,
    journalActions: {
      assign: "Assigned", direct: "Sent to the direct lane", exclude: "Excluded",
      exclude_channel: "Channel excluded", done: "Done", commit: "GO applied",
      lane_ack: "Acked", lane_done: "Done", lane_reroute: "Rerouted", lane_wake: "Wake requested",
    },
    didAction: (name, count, lane) => name + (lane ? " (" + lane + ")" : "") + ": " + count + " item(s)",
    undoButton: (seconds) => "Undo (" + seconds + "s)",
    undoing: "Undoing...",
    undoResult: (c) => ["Restored " + c.restored, c.conflict ? "conflict " + c.conflict : null,
      c.not_found ? "not found " + c.not_found : null,
      c.wake_sent ? "wake already sent " + c.wake_sent : null,
      c.wake_removed ? "wake requests removed " + c.wake_removed : null,
      c.binding_conflict ? "thread binding changed meanwhile, kept " + c.binding_conflict : null].filter(Boolean).join(", "),
    sessionIs: (text) => "Session: " + text,
    badgeNoSession: "no session",
    badgePaused: "paused",
    badgeSessionOff: "session off",
    laneButtonTitle: (text) => "Session: " + text,
    wakeNow: "Wake now",
    wakeNowTitle: (assigned, acked) => "Re-deliver this lane's open items (assigned " + assigned + ", acked " + acked + ") and wake its session",
    wakeItem: "Wake this item",
    wakeItemTitle: "Re-deliver this item and wake the lane session",
    wakeRefused: {
      unregistered: "Lane is not in the ledger", paused: "Lane is paused", no_session: "Lane has no session",
      not_live: "Lane session is not live", ambiguous: "Lane session id matches several live sessions",
      wake_off: "Wakes are switched off (wake.off or wake_policy.enabled false)",
    },
    wakeQueued: (count) => "Wake requests: " + count,
    wakeCapped: (count, total) => "woke the newest " + count + " of " + total + "; press again for the rest",
    wakeAlreadyQueued: (count) => count + " already waiting for a wake",
    dmBadge: "DM",
    dmBadgeTitle: "Direct message: you are deciding which lane handles this person's DMs",
    dmProposalsTitle: "DM scope proposals",
    dmClaimsTitle: "DM claims",
    undoFailed: (detail) => "Undo failed: " + detail,
    laneHandled: "Handled",
    recentActions: "Recent actions",
    recentEmpty: "No console actions recorded yet.",
    undoExpired: "expired",
    undoUndone: "undone",
    tabDeliveries: "Deliveries",
    deliveriesTitle: (total) => total + " delivery attempt(s)",
    deliveriesEmpty: "No delivery attempts in this window.",
    deliveriesSummary: (s) => "sent " + s.sent + " · failed " + s.failed + " · skipped " + s.skipped + " · unconfirmed " + s.unconfirmed,
    deliveryAll: "all",
    deliveryWindows: { h24: "24h", h48: "48h", d7: "7d", all: "all" },
    deliveryPurposes: { lane: "lane", human_queue: "human queue" },
    deliveryStatuses: {
      sent: "sent", failed: "failed", skipped: "skipped", skipped_no_session: "skipped: no live session",
      skipped_ambiguous: "skipped: ambiguous session", skipped_paused: "skipped: paused or wake off",
      dry_run: "dry run", timeout: "timeout",
    },
    deliveryKinds: {
      new: "new", backfill: "backfill", reroute: "reroute", reminder: "reminder", digest: "digest",
      card: "card", attention: "attention",
    },
    relayOutcomes: { delivered: "delivered", not_delivered: "not delivered", unknown: "relay result unknown" },
    deliveryColumns: {
      time: "Time", kind: "Kind", lane: "Lane", target: "Target session", items: "Items",
      result: "Result", receipt: "Receipt", cost: "Cost and time",
    },
    deliveryFilters: { lane: "Lane", purpose: "Purpose", status: "Status", hours: "Window" },
    noTargetSession: "no session",
    receivedAt: (time, minutes) => "✓ " + time + " (+" + minutes + "m)",
    unconfirmedFor: (minutes) => "unconfirmed " + minutes + "m",
    decisionAt: (time) => "decision " + time,
    itemThread: "thread",
    itemMissing: "not in ledger",
    repeated: (count, time) => "x" + count + (time ? " (last " + time + ")" : ""),
    openInSlack: "Open in Slack (new tab)",
    groupDm: "group DM",
    deliveryLogStarted: (when) => "Log started: " + when,
    legacyDeliveries: (count) => count + " earlier record(s) in the old format (no details)",
    searchPlaceholder: "Search id, channel, author or text",
    searchButton: "Search",
    loading: "Loading...",
    loadFailed: "Could not load data from the console server.",
    refreshed: (when) => "Updated " + when,
    refresh: "Refresh",
    queueTitle: (total) => total + " item(s) waiting for a lane",
    tabQueueCount: (count) => "Triage (" + count + ")",
    queueSplit: (undecided, direct, name) => "Lane needed " + undecided + " · " + (name || "Operator") + " direct " + direct,
    queueFilterLabel: "Show",
    queueFilterAll: "All",
    queueFilterUndecided: "Lane needed",
    queueFilterDirect: (name) => (name || "Operator") + " direct",
    directRowTitle: (lane) => "In the direct lane " + lane + ": only you handle it. Assign moves it to another lane (reroute); Done and Exclude close it",
    sessionAttached: "session attached",
    sessionAttachedTitle: "A session is attached to the direct lane; it may handle this item too",
    directInQueue: (name) => "Operator items are in the queue",
    directAlready: "Already in the direct lane",
    sessionDown: "session down",
    sessionDownTitle: "A session is attached to the direct lane but it is not live; nobody else is handling this item",
    queueEmpty: "The human queue is empty.",
    showing: (first, last, total) => "Showing " + first + "-" + last + " of " + total,
    previous: "Previous",
    next: "Next",
    assign: "Assign",
    direct: "Direct",
    directTitle: (lane) => "Assign to " + lane,
    exclude: "Exclude",
    done: "Done",
    notePlaceholder: "Optional note",
    goButton: (count) => "GO (" + count + ")",
    goTitle: "Apply every staged decision",
    cancelAll: "Cancel all",
    applyNow: "Apply now",
    applyNowTitle: "Apply this row's staged decision right away",
    legend: (direct) => "Assign = hand to a session · Direct = to the " + direct + " inbox · Done = already handled or no reply needed · Exclude = posts like this need not reach the queue",
    stagedChip: (label) => "Staged: " + label,
    stagedElsewhere: (count) => count + " staged item(s) on other pages",
    committing: "Applying...",
    commitSummary: (c) => "Applied " + c.applied + ", conflict " + c.conflict + ", not found " + c.not_found + ", error " + c.error,
    outcome: (status, detail) => (status === "conflict" ? "Handled elsewhere meanwhile" : status === "not_found" ? "No longer in the ledger" : "Not applied") + (detail ? " (" + detail + ")" : ""),
    channelHint: "If posts from this channel keep coming, you can propose excluding the channel in the Scope tab",
    openScope: "Open the Scope tab",
    noSuggestionToStage: "This item has no suggested lane",
    registryBadge: (status) => "registry " + status,
    registryTitle: (row) => [row.id, row.title, row.owner ? "owner: " + row.owner : null,
      row.next_action ? "next: " + row.next_action : null].filter(Boolean).join(" / "),
    registryDone: "Done per registry (stage)",
    registryDoneTitle: (note) => "Stage Done with note " + note,
    registryDoneAll: (count) => "Stage all registry-closed (" + count + ")",
    findOwner: "Find owner",
    findOwnerTitle: "Look up who owns this channel or thread (ledger, config, registry, harvest)",
    ownerBest: (lane, tier, source) => "Best: " + lane + " (" + tier + ", " + source + ")",
    ownerNone: "No candidate with a lane.",
    ownerPick: (lane) => "Stage assign to " + lane,
    ownerRerouteSet: (lane) => "Reroute target set to " + lane + "; press Reroute to apply",
    ownerRegistry: (row) => "Registry: " + (row.id || "-") + " / " + (row.status || "-") + (row.owner ? " / " + row.owner : ""),
    ownerLookupTitle: "Owner lookup",
    ownerLookupHint: "Channel id, optional message or thread ts. Read-only.",
    ownerChannelPlaceholder: "Channel id",
    ownerTsPlaceholder: "ts (optional)",
    ownerLookupButton: "Find owner",
    keysHelp: "Keys: g = focus GO; Enter on a focused row = stage the suggested lane; Esc = unstage the focused row; z = undo the last action while its notice shows",
    queueColumns: {
      when: "When",
      channel: "Channel",
      author: "Author",
      attention: "Attention",
      suggestion: "Suggestion",
    },
    noBody: "No body stored",
    rootLabel: "Thread root",
    rootMissing: "Root not in ledger",
    truncated: "(truncated)",
    context: (shown, total) => "Thread context: last " + shown + " of " + total + " message(s)",
    later: "later",
    suggested: (lane, reason) => "Suggested: " + lane + (reason ? " (" + reason + ")" : ""),
    noSuggestion: "No classifier suggestion",
    pendingReason: (reason) => "Queued because: " + reason,
    hints: "Hints:",
    hintRule: (channel) => "rule for " + channel,
    hintHistory: (count) => count + " recent",
    attention: (flag) => "attention: " + flag,
    origin: (value) => "origin: " + value,
    edited: "edited",
    files: "files",
    bot: "bot",
    reply: "reply",
    age: (text) => text + " ago",
    actionApplied: (count) => count + " item(s) updated",
    actionFailed: (count, detail) => count + " item(s) not updated: " + detail,
    requestFailed: (detail) => "Request failed: " + detail,
    lanesTitle: "Lanes",
    openInbox: "Open inbox",
    session: "Session",
    noSession: "no session",
    addressedBy: (kind) => "(" + kind + ")",
    live: "live",
    notLive: "not live",
    paused: "paused",
    unregistered: "not in ledger",
    today: "Today",
    todayCounts: (c) => "new " + c.new + ", assigned " + c.assigned + ", acked " + c.acked + ", done " + c.done,
    open: "Open items",
    oldestUnacked: "Oldest unacknowledged",
    lastDelivery: "Last delivery",
    lastReminder: "Last reminder wake",
    inboxFile: "Inbox file",
    none: "-",
    back: "Back to lanes",
    laneInbox: (lane) => "Lane " + lane + " inbox",
    fullRoots: "Show full root bodies",
    laneEmpty: "No pending items in this lane.",
    ownerReplied: (when) => "Owner replied at " + when,
    ack: "Ack",
    reroute: "Reroute",
    rerouteTo: "Reroute to",
    state: (value) => "state: " + value,
    kind: (value) => "kind: " + value,
    serviceTitle: "Service",
    heartbeat: "Heartbeat",
    noHeartbeat: "No heartbeat file found.",
    stale: "stale",
    fresh: "fresh",
    counters: "Counters",
    wake: "Wake switch",
    wakeOff: "wake.off present: wakes are blocked",
    wakeOn: "wake.off absent",
    wakeConfig: (enabled) => "wake_policy.enabled: " + enabled,
    humanQueue: "Human queue",
    adapters: "Notification adapters",
    reconcile: "Recent reconcile passes",
    noReconcile: "No reconcile pass in the current log.",
    warnings: "Recent warnings and errors",
    noWarnings: "No warnings or errors in the current log.",
    gaps: "Ingest gaps",
    noGaps: "No ingest gaps recorded.",
    searchTitle: (query) => "Search: " + query,
    searchEmpty: "No matching messages.",
    searchTruncated: "Showing the first 200 matches.",
    close: "Close",
    columns: {
      id: "Id", when: "When", channel: "Channel", author: "Author", state: "State", lane: "Lane",
      reason: "Route reason", note: "Note", text: "Text", index: "#", kind: "Kind",
      purpose: "Purpose", dryRun: "Dry run", at: "At", counts: "Counts", level: "Level",
      logger: "Logger", message: "Message", source: "Source", detected: "Detected",
      lastEvent: "Last event", resumed: "Resumed", catchup: "Caught up", key: "Field", value: "Value",
      scopeKey: "Key", name: "Name", evidence: "Evidence", firstSeen: "First seen", lastSeen: "Last seen",
      status: "Status", lanes: "Lanes", current: "Current default", actions: "Actions", by: "By",
      inConfig: "In config", confidence: "Tier", session: "Session",
      action: "Action", items: "Items", actor: "By", undo: "Undo", partner: "Partner",
    },
    yes: "yes",
    no: "no",
    units: { s: "s", m: "m", h: "h", d: "d", bytes: "B", kib: "KiB", mib: "MiB" },
  }),
  ko: Object.freeze({
    title: "디스패치 콘솔",
    navLabel: "보기",
    tabQueue: "큐",
    tabLanes: "레인",
    tabService: "서비스",
    tabProposals: "범위",
    proposalsTitle: (count, min) => "범위 제안 " + count + "건 (일관된 근거 " + min + "건부터 추천)",
    proposalsEmpty: "열려 있거나 추천된 범위 제안이 없음.",
    confirm: "확정",
    reject: "거절",
    defer: "보류",
    conflict: "충돌",
    effective: "적용 중",
    imported: "가져옴",
    current: (lane, source) => lane + " (" + source + ")",
    proposalDone: (status) => "제안 상태: " + status,
    learnedTitle: "학습된 범위",
    learnedEmpty: "원장에 확정된 범위 재정의가 없음.",
    exportButton: "YAML 내보내기",
    exportHint: "인스턴스 설정에 병합. 원장 재정의는 이미 적용 중.",
    excludeChannel: "채널 제외",
    excludeChannelTitle: "이 채널 글을 모두 제외: 열린 항목을 닫고 라우팅을 멈춤 (범위 탭에서 되돌리기)",
    notMember: "미가입 채널",
    notMemberTitle: "멤버십 캐시 기준 내가 가입하지 않은 채널",
    channelExcluded: (channel, count) => "채널 " + channel + " 제외됨, 열린 항목 " + count + "건 닫음. 범위 탭에서 되돌리기",
    exclusionsTitle: "제외된 채널(원장)",
    exclusionsEmpty: "원장에 채널 제외 없음.",
    exportExclusions: "excluded_channels YAML 내보내기",
    undo: "되돌리기",
    undoDone: (channel) => "채널 제외 해제: " + channel,
    journalActions: {
      assign: "배정함", direct: "내가 처리로 보냄", exclude: "제외함",
      exclude_channel: "채널 제외함", done: "끝 처리함", commit: "GO 적용",
      lane_ack: "확인함", lane_done: "끝 처리함", lane_reroute: "재배정함", lane_wake: "깨우기 요청",
    },
    didAction: (name, count, lane) => name + (lane ? " (" + lane + ")" : "") + " " + count + "건",
    undoButton: (seconds) => "되돌리기 (" + seconds + "초)",
    undoing: "되돌리는 중...",
    undoResult: (c) => ["복구 " + c.restored + "건", c.conflict ? "충돌 " + c.conflict + "건" : null,
      c.not_found ? "원장에 없음 " + c.not_found + "건" : null,
      c.wake_sent ? "깨우기는 이미 나감 " + c.wake_sent + "건" : null,
      c.wake_removed ? "깨우기 요청 취소 " + c.wake_removed + "건" : null,
      c.binding_conflict ? "스레드 바인딩이 그사이 바뀌어 유지 " + c.binding_conflict + "건" : null].filter(Boolean).join(", "),
    sessionIs: (text) => "세션: " + text,
    badgeNoSession: "세션 없음",
    badgePaused: "일시 정지",
    badgeSessionOff: "세션 꺼짐",
    laneButtonTitle: (text) => "세션: " + text,
    wakeNow: "지금 깨우기",
    wakeNowTitle: (assigned, acked) => "이 레인의 열린 항목(배정 " + assigned + "건, 확인 " + acked + "건)을 다시 전달하고 세션을 깨움",
    wakeItem: "이 항목 깨우기",
    wakeItemTitle: "이 항목을 다시 전달하고 레인 세션을 깨움",
    wakeRefused: {
      unregistered: "원장에 없는 레인", paused: "레인이 일시 정지 상태", no_session: "레인에 세션 없음",
      not_live: "레인 세션이 꺼져 있음", ambiguous: "세션 id가 살아 있는 세션 여러 개와 일치",
      wake_off: "깨우기 꺼짐 (wake.off 또는 wake_policy.enabled false)",
    },
    wakeQueued: (count) => "깨우기 요청 " + count + "건",
    wakeCapped: (count, total) => total + "건 중 최신 " + count + "건만 깨움. 나머지는 다시 누르기",
    wakeAlreadyQueued: (count) => "이미 깨우기 대기 중 " + count + "건",
    dmBadge: "DM",
    dmBadgeTitle: "개인 DM: 이 사람의 DM을 어느 레인이 맡을지 정하는 중",
    dmProposalsTitle: "DM 범위 제안",
    dmClaimsTitle: "DM 소관 확정",
    undoFailed: (detail) => "되돌리기 실패: " + detail,
    laneHandled: "처리됨",
    recentActions: "최근 작업",
    recentEmpty: "기록된 콘솔 작업 없음.",
    undoExpired: "만료",
    undoUndone: "되돌림",
    tabDeliveries: "전달",
    deliveriesTitle: (total) => "전달 시도 " + total + "건",
    deliveriesEmpty: "이 기간에 전달 시도 없음.",
    deliveriesSummary: (s) => "전송 " + s.sent + " · 실패 " + s.failed + " · 건너뜀 " + s.skipped + " · 미확인 " + s.unconfirmed,
    deliveryAll: "전체",
    deliveryWindows: { h24: "24시간", h48: "48시간", d7: "7일", all: "전체" },
    deliveryPurposes: { lane: "레인", human_queue: "사람 큐" },
    deliveryStatuses: {
      sent: "전송", failed: "실패", skipped: "건너뜀", skipped_no_session: "건너뜀: 세션 꺼짐",
      skipped_ambiguous: "건너뜀: 세션 모호", skipped_paused: "건너뜀: 일시정지 또는 깨우기 꺼짐",
      dry_run: "드라이런", timeout: "시간 초과",
    },
    deliveryKinds: {
      new: "신규", backfill: "백필", reroute: "재배정", reminder: "리마인더", digest: "다이제스트",
      card: "카드", attention: "주의",
    },
    relayOutcomes: { delivered: "전달됨", not_delivered: "전달 안 됨", unknown: "릴레이 결과 불명" },
    deliveryColumns: {
      time: "시각", kind: "종류", lane: "레인", target: "대상 세션", items: "항목 수",
      result: "전송 결과", receipt: "수신 확인", cost: "비용과 소요 시간",
    },
    deliveryFilters: { lane: "레인", purpose: "용도", status: "상태", hours: "기간" },
    noTargetSession: "세션 없음",
    receivedAt: (time, minutes) => "✓ " + time + " (+" + minutes + "분)",
    unconfirmedFor: (minutes) => "미확인 " + minutes + "분",
    decisionAt: (time) => "결정 " + time,
    itemThread: "스레드",
    itemMissing: "원장에 없음",
    repeated: (count, time) => count + "회" + (time ? " (마지막 " + time + ")" : ""),
    openInSlack: "Slack에서 열기 (새 탭)",
    groupDm: "그룹 DM",
    deliveryLogStarted: (when) => "기록 시작: " + when,
    legacyDeliveries: (count) => "이전 방식 기록 " + count + "건 (상세 없음)",
    searchPlaceholder: "id, 채널, 작성자, 본문 검색",
    searchButton: "검색",
    loading: "불러오는 중...",
    loadFailed: "콘솔 서버에서 데이터를 불러오지 못함.",
    refreshed: (when) => "갱신 " + when,
    refresh: "새로고침",
    queueTitle: (total) => "레인 배정 대기 " + total + "건",
    tabQueueCount: (count) => "큐 (" + count + ")",
    queueSplit: (undecided, direct, name) => "레인 결정 필요 " + undecided + " · " + (name || "운영자") + " 직접 " + direct,
    queueFilterLabel: "보기",
    queueFilterAll: "전체",
    queueFilterUndecided: "레인 결정 필요",
    queueFilterDirect: (name) => (name || "운영자") + " 직접",
    directRowTitle: (lane) => "직접 레인 " + lane + " 항목: 내가 처리할 글. 배정 = 다른 레인으로 재배정, 끝·제외 = 닫기",
    sessionAttached: "세션 있음",
    sessionAttachedTitle: "직접 레인에 세션이 연결돼 있어 그 세션도 이 항목을 볼 수 있음",
    directInQueue: (name) => (name || "운영자") + " 직접 글은 큐에 있습니다",
    directAlready: "이미 직접 레인에 있음",
    sessionDown: "세션 꺼짐",
    sessionDownTitle: "직접 레인에 세션 값은 있지만 꺼져 있어 이 항목을 보는 세션이 없음",
    queueEmpty: "사람 큐가 비어 있음.",
    showing: (first, last, total) => total + "건 중 " + first + "-" + last,
    previous: "이전",
    next: "다음",
    assign: "배정",
    direct: "내가 처리",
    directTitle: (lane) => lane + " 인박스로 배정",
    exclude: "제외(큐에서 빼기)",
    done: "끝(처리됨·답 불필요)",
    notePlaceholder: "메모(선택)",
    goButton: (count) => "GO (" + count + ")",
    goTitle: "예정된 결정을 모두 적용",
    cancelAll: "모두 취소",
    applyNow: "즉시 적용",
    applyNowTitle: "이 행의 예정 결정만 바로 적용",
    legend: (direct) => "배정 = 세션에 맡김 · 내가 처리 = " + direct + " 인박스로 · 끝 = 이미 처리했거나 답 불필요 · 제외 = 이런 글은 큐에 안 와도 됨",
    stagedChip: (label) => "예정: " + label,
    stagedElsewhere: (count) => "다른 페이지에 예정 " + count + "건",
    committing: "적용 중...",
    commitSummary: (c) => "적용 " + c.applied + ", 충돌 " + c.conflict + ", 없음 " + c.not_found + ", 오류 " + c.error,
    outcome: (status, detail) => (status === "conflict" ? "그사이 다른 곳에서 처리됨" : status === "not_found" ? "원장에 더 이상 없음" : "적용 안 됨") + (detail ? " (" + detail + ")" : ""),
    channelHint: "이 채널 글이 계속 오면 Scope 탭에서 채널 제외를 제안할 수 있음",
    openScope: "범위(Scope) 탭 열기",
    noSuggestionToStage: "이 항목에는 제안 레인이 없음",
    registryBadge: (status) => "레지스트리 " + status,
    registryTitle: (row) => [row.id, row.title, row.owner ? "소유: " + row.owner : null,
      row.next_action ? "다음: " + row.next_action : null].filter(Boolean).join(" / "),
    registryDone: "레지스트리대로 끝 예약",
    registryDoneTitle: (note) => "메모 " + note + " 로 끝 예약",
    registryDoneAll: (count) => "레지스트리 종결 전부 예약 (" + count + ")",
    findOwner: "소관 찾기",
    findOwnerTitle: "이 채널·스레드의 소관 찾기 (원장, 설정, 레지스트리, 수확)",
    ownerBest: (lane, tier, source) => "최선: " + lane + " (" + tier + ", " + source + ")",
    ownerNone: "레인이 있는 후보 없음.",
    ownerPick: (lane) => lane + " 배정 예약",
    ownerRerouteSet: (lane) => "재배정 대상을 " + lane + "(으)로 설정함. 재배정을 눌러 적용",
    ownerRegistry: (row) => "레지스트리: " + (row.id || "-") + " / " + (row.status || "-") + (row.owner ? " / " + row.owner : ""),
    ownerLookupTitle: "소관 찾기",
    ownerLookupHint: "채널 id, 선택으로 메시지·스레드 ts. 읽기 전용.",
    ownerChannelPlaceholder: "채널 id",
    ownerTsPlaceholder: "ts (선택)",
    ownerLookupButton: "소관 찾기",
    keysHelp: "단축키: g = GO로 이동 · 행에서 Enter = 제안 레인 배정 예정 · Esc = 행의 예정 취소 · z = 알림이 떠 있는 동안 마지막 작업 되돌리기",
    queueColumns: {
      when: "시각",
      channel: "채널",
      author: "작성자",
      attention: "주의",
      suggestion: "제안",
    },
    noBody: "저장된 본문 없음",
    rootLabel: "스레드 원글",
    rootMissing: "원글이 원장에 없음",
    truncated: "(잘림)",
    context: (shown, total) => "스레드 맥락: 전체 " + total + "건 중 최근 " + shown + "건",
    later: "이후",
    suggested: (lane, reason) => "제안: " + lane + (reason ? " (" + reason + ")" : ""),
    noSuggestion: "분류기 제안 없음",
    pendingReason: (reason) => "큐 사유: " + reason,
    hints: "힌트:",
    hintRule: (channel) => channel + " 규칙",
    hintHistory: (count) => "최근 " + count + "건",
    attention: (flag) => "주의: " + flag,
    origin: (value) => "출처: " + value,
    edited: "수정됨",
    files: "파일",
    bot: "봇",
    reply: "답글",
    age: (text) => text + " 전",
    actionApplied: (count) => count + "건 반영",
    actionFailed: (count, detail) => count + "건 미반영: " + detail,
    requestFailed: (detail) => "요청 실패: " + detail,
    lanesTitle: "레인",
    openInbox: "인박스 열기",
    session: "세션",
    noSession: "세션 없음",
    addressedBy: (kind) => "(" + kind + ")",
    live: "살아 있음",
    notLive: "꺼짐",
    paused: "일시정지",
    unregistered: "원장에 없음",
    today: "오늘",
    todayCounts: (c) => "신규 " + c.new + ", 배정 " + c.assigned + ", 확인 " + c.acked + ", 완료 " + c.done,
    open: "열린 항목",
    oldestUnacked: "가장 오래된 미확인",
    lastDelivery: "마지막 전달",
    lastReminder: "마지막 리마인더 깨우기",
    inboxFile: "인박스 파일",
    none: "-",
    back: "레인 목록으로",
    laneInbox: (lane) => "레인 " + lane + " 인박스",
    fullRoots: "원글 전문 보기",
    laneEmpty: "이 레인에 대기 항목 없음.",
    ownerReplied: (when) => "오너 답장 " + when,
    ack: "확인",
    reroute: "재배정",
    rerouteTo: "재배정 대상",
    state: (value) => "상태: " + value,
    kind: (value) => "종류: " + value,
    serviceTitle: "서비스",
    heartbeat: "하트비트",
    noHeartbeat: "하트비트 파일 없음.",
    stale: "오래됨",
    fresh: "최신",
    counters: "카운터",
    wake: "깨우기 스위치",
    wakeOff: "wake.off 있음: 깨우기 차단 중",
    wakeOn: "wake.off 없음",
    wakeConfig: (enabled) => "wake_policy.enabled: " + enabled,
    humanQueue: "사람 큐",
    adapters: "알림 어댑터",
    reconcile: "최근 대사 패스",
    noReconcile: "현재 로그에 대사 패스 없음.",
    warnings: "최근 경고와 오류",
    noWarnings: "현재 로그에 경고나 오류 없음.",
    gaps: "수집 공백",
    noGaps: "기록된 수집 공백 없음.",
    searchTitle: (query) => "검색: " + query,
    searchEmpty: "일치하는 메시지 없음.",
    searchTruncated: "처음 200건만 표시.",
    close: "닫기",
    columns: {
      id: "Id", when: "시각", channel: "채널", author: "작성자", state: "상태", lane: "레인",
      reason: "라우팅 사유", note: "메모", text: "본문", index: "#", kind: "종류",
      purpose: "용도", dryRun: "드라이런", at: "시각", counts: "개수", level: "수준",
      logger: "로거", message: "메시지", source: "소스", detected: "감지",
      lastEvent: "마지막 이벤트", resumed: "재개", catchup: "따라잡음", key: "항목", value: "값",
      scopeKey: "키", name: "이름", evidence: "근거", firstSeen: "처음 확인", lastSeen: "마지막 확인",
      status: "상태", lanes: "레인", current: "현재 기본값", actions: "작업", by: "확정자",
      inConfig: "설정에 있음", confidence: "단계", session: "세션",
      action: "작업 종류", items: "항목 수", actor: "실행자", undo: "되돌리기", partner: "상대",
    },
    yes: "예",
    no: "아니오",
    units: { s: "초", m: "분", h: "시간", d: "일", bytes: "B", kib: "KiB", mib: "MiB" },
  }),
});

// The active dictionary; start() switches it to console.ui_language.
let UI = UI_TEXT.en;

const VIEWS = { "/": "queue", "/lanes": "lanes", "/service": "service", "/proposals": "proposals" };
const QUEUE_ACTIONS = ["assign", "direct", "exclude", "exclude_channel", "done"];
// Staged triage decisions survive polling and reloads (P14); per-viewer browser storage.
const STAGE_KEY = "slack-dispatch.console.staged.v1";
const MAX_COMMIT = 200;
const state = {
  config: null,
  view: "queue",
  queueOffset: 0,
  queueCards: new Map(),
  staged: new Map(),
  committing: false,
  pendingMsg: null,
  lane: null,
  laneFull: false,
  previousView: "queue",
  registryClosed: [],
  ownerCard: null,
  // P21: lane -> session identity (queue lane button titles) and the open lane's wake state.
  laneSessions: {},
  laneWake: null,
  // P25: the queue filter (all, undecided, direct) and the last counts for the tab label.
  queueKind: "all",
  queueCounts: null,
};
const QUEUE_KINDS = ["all", "undecided", "direct"];
const QUEUE_KIND_KEY = "slack-dispatch.console.queue-kind.v1";
// P19 undo: the bottom toast of the latest immediate action or GO commit, and lane rows
// that show "Handled · Undo" inline until their window ends (node -> entry).
const undoState = { toast: null, inline: new Map(), ticker: null, busy: false };

// ---------- helpers ----------

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "className") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

function age(seconds) {
  if (seconds === null || seconds === undefined) return UI.none;
  const u = UI.units;
  if (seconds < 60) return seconds + u.s;
  if (seconds < 3600) return Math.floor(seconds / 60) + u.m;
  if (seconds < 86400) return Math.floor(seconds / 3600) + u.h + " " + Math.floor((seconds % 3600) / 60) + u.m;
  return Math.floor(seconds / 86400) + u.d + " " + Math.floor((seconds % 86400) / 3600) + u.h;
}

function bytes(value) {
  if (value === null || value === undefined) return UI.none;
  const u = UI.units;
  if (value < 1024) return value + " " + u.bytes;
  if (value < 1048576) return (value / 1024).toFixed(1) + " " + u.kib;
  return (value / 1048576).toFixed(1) + " " + u.mib;
}

// P20: the server resolves names (config, then ledger, then id); `group DM` is the one
// channel name the page translates.
const GROUP_DM = "group DM";
const LINK_ICON = "↗";

function named(ref) {
  if (!ref || !ref.id) return UI.none;
  if (ref.name === GROUP_DM) return UI.groupDm;
  return ref.name ? ref.name : ref.id;
}

// A user or channel shown by name with its id on hover (title), so it can still be copied.
function nameSpan(ref, props) {
  return el("span", Object.assign({ title: ref && ref.id ? ref.id : null, text: named(ref) }, props || {}));
}

// A channel label: `#name` for configured names, the resolved label (`#name`, `DM:...`,
// group DM) as is, `#id` when only the id is known.
function channelText(ref) {
  if (!ref || !ref.id) return UI.none;
  const name = ref.name;
  if (!name) return "#" + ref.id;
  if (name === GROUP_DM) return UI.groupDm;
  return name.startsWith("#") || name.startsWith("DM:") ? name : "#" + name;
}

// Scope tab names (P20: resolved by the server; null shows "-").
function scopeName(name) {
  return name === GROUP_DM ? UI.groupDm : name;
}

// P20: a small link icon that opens the Slack permalink in a new tab; nothing without one
// (console.workspace_domain unset).
function linkIcon(url) {
  if (!url) return null;
  return el("a", { className: "permalink", href: url, target: "_blank", rel: "noopener noreferrer",
    title: UI.openInSlack, "aria-label": UI.openInSlack, text: LINK_ICON });
}

// P21: a lane session as `<title> (<live name>, <id8>)`; without a title the live name or the
// configured value leads. Null without a session value.
function identityText(identity) {
  if (!identity || !identity.value) return null;
  const name = identity.live ? identity.name : null;
  const head = identity.title || name || identity.value;
  const inner = [name, identity.id8].filter((part) => part && part !== head);
  return head + (inner.length ? " (" + inner.join(", ") + ")" : "");
}

// The badge for a session that cannot be woken now: no session, paused or off. Null when live.
function identityBadge(identity) {
  const state = identity ? identity.state : "no_session";
  if (state === "no_session") return el("span", { className: "badge bad", text: UI.badgeNoSession });
  if (state === "paused") return el("span", { className: "badge warn", text: UI.badgePaused });
  if (!identity.live) return el("span", { className: "badge bad", text: UI.badgeSessionOff });
  return null;
}

function identityLine(identity) {
  const text = identityText(identity);
  return el("span", { className: "session-identity", title: identity && identity.id ? identity.id : null },
    el("span", { className: "dot" + (identity && identity.live ? " live" : "") }),
    text ? UI.sessionIs(text) : null, " ", identityBadge(identity));
}

// The Wake now button of a lane (card or inbox header), disabled with the reason as tooltip.
function wakeButton(lane, wake, extraClass) {
  const allowed = Boolean(wake && wake.allowed);
  const reason = wake && wake.reason ? (UI.wakeRefused[wake.reason] || wake.detail || wake.reason) : null;
  const button = el("button", { className: "wake-now" + (extraClass ? " " + extraClass : ""), text: UI.wakeNow,
    disabled: !allowed, title: allowed ? UI.wakeNowTitle(wake.assigned, wake.acked) : reason,
    onclick: () => wakeLane(lane, null) });
  // Some browsers show no tooltip on a disabled button; the wrapper carries the reason too.
  return el("span", { className: "wake-wrap", title: allowed ? null : reason }, button);
}

function bodyBlock(text, truncated) {
  if (text === null || text === undefined) return el("div", { className: "body empty", text: UI.noBody });
  return el("div", { className: "body" }, text, truncated ? " " + UI.truncated : null);
}

let statusTimer = null;
function status(message, isError) {
  const node = document.getElementById("status");
  node.textContent = message || "";
  node.className = "status" + (message ? " show" : "") + (isError ? " error" : "");
  clearTimeout(statusTimer);
  if (message) statusTimer = setTimeout(() => status(""), isError ? 12000 : 5000);
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    throw new Error(UI.loadFailed);
  }
  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!response.ok) {
    const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : String(response.status);
    throw new Error(UI.requestFailed(detail));
  }
  return data;
}

function reportResults(data) {
  const failed = data.results.filter((row) => !row.ok);
  if (failed.length) {
    const detail = failed.map((row) => row.id + " " + row.error + (row.detail ? " (" + row.detail + ")" : "")).join("; ");
    status(UI.actionFailed(failed.length, detail), true);
  } else {
    status(UI.actionApplied(data.applied));
  }
  return new Set(data.results.filter((row) => row.ok).map((row) => row.id));
}

function nowText() {
  const now = new Date();
  return [now.getHours(), now.getMinutes(), now.getSeconds()].map((part) => String(part).padStart(2, "0")).join(":");
}

// ---------- views and routing ----------

function showView(name, push) {
  state.view = name;
  for (const section of document.querySelectorAll(".view")) section.hidden = section.id !== "view-" + name;
  for (const link of document.querySelectorAll(".tabs a")) link.classList.toggle("active", link.dataset.view === name);
  if (push) {
    const path = Object.keys(VIEWS).find((key) => VIEWS[key] === name);
    if (path && location.pathname !== path) history.pushState({}, "", path);
  }
  refresh();
}

function refresh() {
  // P25: the queue tab label counts both kinds whatever view is open.
  if (state.view !== "queue") loadQueueCounts();
  if (state.view === "queue") return loadQueue();
  if (state.view === "lanes") return state.lane ? loadLaneInbox() : loadLanes();
  if (state.view === "service") return loadService();
  if (state.view === "proposals") return loadProposals();
  if (state.view === "deliveries") return loadDeliveries();
  return null;
}

// ---------- triage queue (P14: staged decisions, one GO) ----------

function loadStaged() {
  const staged = new Map();
  let raw = null;
  try {
    raw = JSON.parse(window.localStorage.getItem(STAGE_KEY) || "{}");
  } catch (error) {
    raw = null;
  }
  if (!raw || typeof raw !== "object") return staged;
  for (const [id, decision] of Object.entries(raw)) {
    if (!decision || !QUEUE_ACTIONS.includes(decision.action)) continue;
    if (decision.action === "assign" && typeof decision.lane !== "string") continue;
    staged.set(id, {
      action: decision.action,
      lane: decision.action === "assign" ? decision.lane : null,
      note: typeof decision.note === "string" ? decision.note : "",
    });
  }
  return staged;
}

function saveStaged() {
  try {
    window.localStorage.setItem(STAGE_KEY, JSON.stringify(Object.fromEntries(state.staged)));
  } catch (error) {
    // Storage can be unavailable (private window, blocked site data); staging still works in memory.
  }
}

function sameDecision(decision, action, lane) {
  return Boolean(decision) && decision.action === action && (action !== "assign" || decision.lane === lane);
}

function decisionLabel(decision) {
  if (decision.action === "assign") return UI.assign + " " + decision.lane;
  if (decision.action === "exclude_channel") return UI.excludeChannel;
  return UI[decision.action];
}

function queueRoot() {
  const section = document.getElementById("view-queue");
  if (!section.querySelector(".queue-list")) {
    const c = UI.queueColumns;
    const header = el("div", { className: "toolbar" },
      el("strong", { className: "queue-total", text: UI.loading }),
      queueFilter(),
      el("span", { className: "muted small queue-updated" }),
      el("button", { className: "registry-all", hidden: true, onclick: () => stageRegistryClosed() }),
      el("span", { className: "spacer" }),
      el("span", { className: "muted small queue-range" }),
      el("button", { className: "queue-prev", text: UI.previous, onclick: () => pageQueue(-1) }),
      el("button", { className: "queue-next", text: UI.next, onclick: () => pageQueue(1) }),
      el("button", { text: UI.refresh, onclick: () => loadQueue() }));
    const head = el("div", { className: "queue-head small muted" },
      [c.when, c.channel, c.author, c.attention, c.suggestion].map((label) => el("span", { text: label })));
    section.append(goBar(), header, head, el("div", { className: "queue-list" }));
  }
  return section;
}

// P25: filter toggle (all, undecided, direct); the choice is a per-viewer convenience.
function queueFilter() {
  const name = state.config ? state.config.operator_name : null;
  const labels = { all: UI.queueFilterAll, undecided: UI.queueFilterUndecided, direct: UI.queueFilterDirect(name) };
  return el("span", { className: "queue-filter small", role: "group", "aria-label": UI.queueFilterLabel },
    el("span", { className: "muted", text: UI.queueFilterLabel }),
    QUEUE_KINDS.map((kind) => el("button", { className: "filter-" + kind + (state.queueKind === kind ? " chosen" : ""),
      "aria-pressed": state.queueKind === kind ? "true" : "false", text: labels[kind],
      onclick: () => setQueueKind(kind) })));
}

function paintQueueFilter() {
  const section = document.getElementById("view-queue");
  for (const kind of QUEUE_KINDS) {
    const button = section.querySelector(".queue-filter .filter-" + kind);
    if (!button) continue;
    button.classList.toggle("chosen", kind === state.queueKind);
    button.setAttribute("aria-pressed", kind === state.queueKind ? "true" : "false");
  }
}

function setQueueKind(kind) {
  if (!QUEUE_KINDS.includes(kind)) return;
  state.queueKind = kind;
  state.queueOffset = 0;
  try {
    window.localStorage.setItem(QUEUE_KIND_KEY, kind);
  } catch (error) {
    // Storage can be unavailable; the filter still works for this page.
  }
  paintQueueFilter();
  if (state.view === "queue") loadQueue();
}

function loadQueueKind() {
  try {
    const kind = window.localStorage.getItem(QUEUE_KIND_KEY);
    if (QUEUE_KINDS.includes(kind)) state.queueKind = kind;
  } catch (error) {
    // Default: both kinds.
  }
}

// P25: the tab label `Triage (N)` counts undecided and direct rows.
function paintQueueTab(counts) {
  state.queueCounts = counts || null;
  const tab = document.querySelector(".tabs a[data-view=queue]");
  if (!tab) return;
  tab.textContent = counts ? UI.tabQueueCount((counts.undecided || 0) + (counts.direct || 0)) : UI.tabQueue;
}

async function loadQueueCounts() {
  try {
    paintQueueTab(await api("/api/queue/counts"));
  } catch (error) {
    // The tab keeps its last label; the open view reports its own errors.
  }
}

function isDirectRow(item) {
  return Boolean(item) && item.queue_kind === "direct";
}

function goBar() {
  return el("div", { className: "gobar" },
    el("div", { className: "gobar-row" },
      el("button", { className: "go-button primary", text: UI.goButton(0), title: UI.goTitle, onclick: () => commitStaged() }),
      el("button", { className: "cancel-all", text: UI.cancelAll, onclick: () => cancelAll() }),
      el("span", { className: "muted small gobar-elsewhere" })),
    el("div", { className: "legend small", text: UI.legend((state.config && state.config.direct_lane) || UI.none) }),
    el("div", { className: "commit-results small", hidden: true }));
}

function updateGoBar() {
  const section = queueRoot();
  const count = state.staged.size;
  const go = section.querySelector(".go-button");
  go.textContent = state.committing ? UI.committing : UI.goButton(count);
  go.disabled = state.committing || count === 0;
  section.querySelector(".cancel-all").disabled = state.committing || count === 0;
  const elsewhere = Array.from(state.staged.keys()).filter((id) => !state.queueCards.has(id)).length;
  section.querySelector(".gobar-elsewhere").textContent = elsewhere ? UI.stagedElsewhere(elsewhere) : "";
}

function pageQueue(step) {
  const size = state.config.page_size;
  state.queueOffset = Math.max(0, state.queueOffset + step * size);
  loadQueue();
}

async function loadQueue() {
  const section = queueRoot();
  updateGoBar();
  let data;
  try {
    data = await api("/api/queue?offset=" + state.queueOffset + "&limit=" + state.config.page_size +
      "&kind=" + encodeURIComponent(state.queueKind));
  } catch (error) {
    status(error.message, true);
    return;
  }
  if (data.total && data.offset >= data.total) {
    state.queueOffset = Math.max(0, data.total - (data.total % data.limit || data.limit));
    return loadQueue();
  }
  pruneStaged(data.pending_ids);
  state.laneSessions = data.lane_sessions && typeof data.lane_sessions === "object" ? data.lane_sessions : {};
  state.registryClosed = Array.isArray(data.registry_closed) ? data.registry_closed : [];
  const registryAll = section.querySelector(".registry-all");
  registryAll.hidden = !state.registryClosed.length;
  registryAll.textContent = UI.registryDoneAll(state.registryClosed.length);
  // P25: the header shows the split of both kinds; the tab label counts them together.
  const counts = data.counts || { undecided: data.total, direct: 0 };
  paintQueueTab(counts);
  section.querySelector(".queue-total").textContent = counts.undecided + counts.direct
    ? UI.queueSplit(counts.undecided, counts.direct, state.config.operator_name) : UI.queueEmpty;
  section.querySelector(".queue-updated").textContent = UI.refreshed(nowText());
  const first = data.items.length ? data.offset + 1 : 0;
  section.querySelector(".queue-range").textContent = data.total ? UI.showing(first, data.offset + data.items.length, data.total) : "";
  section.querySelector(".queue-prev").disabled = data.offset === 0;
  section.querySelector(".queue-next").disabled = data.offset + data.items.length >= data.total;
  renderQueue(section.querySelector(".queue-list"), data.items);
  updateGoBar();
  focusPendingMessage();
}

function pruneStaged(pendingIds) {
  // Drop staged ids that left the queue (handled elsewhere); pending_ids covers every page.
  if (!Array.isArray(pendingIds) || state.committing) return;
  const pending = new Set(pendingIds);
  let changed = false;
  for (const id of Array.from(state.staged.keys())) {
    if (!pending.has(id)) {
      state.staged.delete(id);
      changed = true;
    }
  }
  if (changed) saveStaged();
}

function renderQueue(list, items) {
  // Existing rows keep their node (and so their staged marks, focus and typed note);
  // only new rows are built, so a poll never rebuilds or reorders a staged row.
  const wanted = new Set(items.map((item) => item.id));
  for (const [id, card] of state.queueCards) {
    if (!wanted.has(id)) {
      card.node.remove();
      state.queueCards.delete(id);
    }
  }
  let ref = list.firstChild;
  for (const item of items) {
    // P25: a row that became direct meanwhile drops a staged Direct (it is already direct).
    const staged = state.staged.get(item.id);
    if (isDirectRow(item) && staged && (staged.action === "direct" ||
        (staged.action === "assign" && staged.lane === state.config.direct_lane))) {
      state.staged.delete(item.id);
      saveStaged();
    }
    let card = state.queueCards.get(item.id);
    if (card && (card.item.queue_kind || "undecided") !== (item.queue_kind || "undecided")) {
      // P25: the row changed kind (sent to or out of the direct lane elsewhere): rebuild it.
      if (card.node === ref) ref = ref.nextSibling;
      card.node.remove();
      state.queueCards.delete(item.id);
      card = null;
    }
    if (card) {
      card.age.textContent = UI.age(age(item.age_seconds));
    } else {
      card = queueCard(item);
      state.queueCards.set(item.id, card);
    }
    paintCard(card);
    paintRegistry(card, item);
    paintLaneTitles(card);
    if (card.node === ref) ref = ref.nextSibling;
    else list.insertBefore(card.node, ref);
  }
}

// P21: each lane button's tooltip names the lane session (title, live name, id8).
function paintLaneTitles(card) {
  for (const entry of card.buttons) {
    if (entry.action !== "assign") continue;
    const text = identityText(state.laneSessions[entry.lane]);
    if (text) entry.node.title = UI.laneButtonTitle(text);
    else entry.node.removeAttribute("title");
  }
}

function laneOrder(item) {
  // P25: a direct row is already in the direct lane, so that lane is not offered again.
  const lanes = state.config.lanes.filter((lane) => !isDirectRow(item) || lane !== state.config.direct_lane);
  const suggested = item.suggestion && lanes.includes(item.suggestion.lane) ? item.suggestion.lane : null;
  return suggested ? [suggested].concat(lanes.filter((lane) => lane !== suggested)) : lanes;
}

function queueCard(item) {
  const c = UI.queueColumns;
  const ageNode = el("span", { text: UI.age(age(item.age_seconds)) });
  const chip = el("span", { className: "badge chip", hidden: true });
  const registryBadge = el("span", { className: "badge registry", hidden: true });
  const meta = el("div", { className: "meta" },
    chip,
    el("strong", { title: c.when, text: item.when || item.ts }),
    linkIcon(item.permalink),
    isDirectRow(item) ? el("span", { className: "badge direct-kind", title: UI.directRowTitle(item.lane || state.config.direct_lane),
      text: UI.queueFilterDirect(state.config.operator_name) }) : null,
    // fix1: an attached session that is not live shows `session down` instead.
    isDirectRow(item) && item.direct_session && item.direct_session_down
      ? el("span", { className: "badge bad", title: UI.sessionDownTitle, text: UI.sessionDown }) : null,
    isDirectRow(item) && item.direct_session && !item.direct_session_down
      ? el("span", { className: "badge ok", title: UI.sessionAttachedTitle, text: UI.sessionAttached }) : null,
    el("span", { title: c.channel + ": " + item.channel.id, text: channelText(item.channel) }),
    item.is_dm ? el("span", { className: "badge dm", title: UI.dmBadgeTitle, text: UI.dmBadge }) : null,
    item.not_member ? el("span", { className: "badge warn", title: UI.notMemberTitle, text: UI.notMember }) : null,
    el("span", { title: c.author + ": " + (item.author.id || ""), text: named(item.author) }),
    item.attention ? el("span", { className: "badge attention", title: c.attention, text: UI.attention(item.attention) }) : null,
    item.is_reply ? el("span", { className: "badge", text: UI.reply }) : null,
    item.origin ? el("span", { className: "badge", text: UI.origin(item.origin) }) : null,
    item.edited ? el("span", { className: "badge warn", text: UI.edited }) : null,
    item.has_files ? el("span", { className: "badge", text: UI.files }) : null,
    item.is_bot ? el("span", { className: "badge", text: UI.bot }) : null,
    registryBadge,
    ageNode,
    el("span", { className: "mono small", text: item.id }));
  const parts = [meta];
  if (item.root) {
    parts.push(el("div", { className: "root" },
      el("div", { className: "meta small" }, el("strong", { text: UI.rootLabel }),
        linkIcon(item.root.permalink),
        item.root.missing ? el("span", { text: UI.rootMissing }) : [
          el("span", { title: item.root.author.id || "", text: named(item.root.author) }),
          el("span", { text: item.root.when || "" })]),
      item.root.missing ? null : bodyBlock(item.root.body, item.root.truncated)));
  }
  parts.push(bodyBlock(item.body, false));
  if (item.context.length) {
    const total = Math.max(0, item.thread_size - 1 - (item.root && !item.root.missing ? 1 : 0));
    parts.push(el("details", { className: "context" },
      el("summary", { text: UI.context(item.context.length, total) }),
      item.context.map((row) => el("div", { className: "context-item" + (row.after ? " after" : "") },
        el("div", { className: "meta small" },
          el("span", { title: row.author.id || "", text: named(row.author) }),
          el("span", { text: row.when || "" }),
          linkIcon(row.permalink),
          row.after ? el("span", { className: "badge", text: UI.later }) : null,
          row.state ? el("span", { className: "badge", text: UI.state(row.state) }) : null),
        bodyBlock(row.body, row.truncated)))));
  }
  const hintLanes = new Map(item.hints.map((hint) => [hint.lane, hint]));
  const suggestText = item.suggestion ? UI.suggested(item.suggestion.lane, item.suggestion.reason) : UI.noSuggestion;
  parts.push(el("div", { className: "suggest small" },
    suggestText,
    item.pending_reason ? " / " + UI.pendingReason(item.pending_reason) : null,
    item.hints.length ? " / " + UI.hints + " " + item.hints.map((hint) => hint.lane + " (" +
      (hint.source === "rule" ? UI.hintRule(hint.channel) : UI.hintHistory(hint.count)) + ")").join(", ") : null));
  const note = el("input", { type: "text", placeholder: UI.notePlaceholder, maxlength: "2000",
    oninput: () => updateStagedNote(item.id, note.value) });
  const staged = state.staged.get(item.id);
  if (staged && staged.action === "done" && staged.note) note.value = staged.note;
  const buttons = [];
  const button = (action, lane, props) => {
    const node = el("button", Object.assign({ "aria-pressed": "false",
      onclick: () => toggleStage(item.id, action, lane) }, props));
    buttons.push({ node, action, lane });
    return node;
  };
  const laneButtons = laneOrder(item).map((lane) => {
    const suggested = item.suggestion && item.suggestion.lane === lane;
    const hint = hintLanes.get(lane);
    return button("assign", lane, { className: suggested ? "suggested" : (hint ? "hinted" : null), text: lane });
  });
  const applyNow = el("button", { className: "link apply-now", text: UI.applyNow, title: UI.applyNowTitle,
    onclick: () => applyNowRow(item.id) });
  const ownerBox = el("div", { className: "owner-panel small", hidden: true });
  const registrySlot = el("span", { className: "registry-slot" });
  const findOwner = state.config.owner_resolver
    ? el("button", { className: "link", text: UI.findOwner, title: UI.findOwnerTitle,
      onclick: () => toggleOwner(ownerBox, item.channel.id, item.ts, (lane) => {
        if (!sameDecision(state.staged.get(item.id), "assign", lane)) toggleStage(item.id, "assign", lane);
      }) }) : null;
  parts.push(el("div", { className: "actions" },
    el("span", { className: "muted small group-label", text: UI.assign }),
    laneButtons,
    // P25: a direct row is already direct, so the Direct button is not shown.
    state.config.direct_lane && !isDirectRow(item) ? button("direct", null, { text: UI.direct, title: UI.directTitle(state.config.direct_lane) }) : null,
    button("exclude", null, { className: "danger", text: UI.exclude }),
    button("exclude_channel", null, { className: "danger", text: UI.excludeChannel, title: UI.excludeChannelTitle }),
    note,
    button("done", null, { text: UI.done }),
    registrySlot,
    el("span", { className: "spacer" }),
    findOwner,
    applyNow));
  parts.push(ownerBox);
  const outcome = el("div", { className: "outcome small", hidden: true });
  parts.push(outcome);
  const node = el("article", { className: "card" + (isDirectRow(item) ? " direct-row" : ""), "data-id": item.id, tabindex: "0" }, parts);
  return { node, item, age: ageNode, chip, note, buttons, applyNow, outcome, registryBadge,
    registrySlot, registryNote: null };
}

// P17: the registry badge and the row's "Done per registry" button follow every poll.
// The button uses the same data as the header button (GET /api/queue registry_closed).
function paintRegistry(card, item) {
  const info = item.registry;
  card.registryBadge.hidden = !info;
  card.registryBadge.className = "badge registry" + (info && info.closed ? " ok" : "");
  card.registryBadge.textContent = info ? UI.registryBadge(info.status || UI.none) : "";
  if (info) card.registryBadge.title = UI.registryTitle(info);
  else card.registryBadge.removeAttribute("title");
  const row = state.registryClosed.find((entry) => entry && entry.id === card.item.id && entry.note);
  const note = row ? row.note : null;
  if (note === card.registryNote) return;
  card.registryNote = note;
  clear(card.registrySlot);
  if (note) {
    card.registrySlot.append(el("button", { className: "registry-done", text: UI.registryDone,
      title: UI.registryDoneTitle(note), onclick: () => stageRegistryDone(card.item.id, note) }));
  }
}

function paintCard(card) {
  const decision = state.staged.get(card.item.id);
  card.node.classList.toggle("staged", Boolean(decision));
  for (const entry of card.buttons) {
    const chosen = sameDecision(decision, entry.action, entry.lane);
    entry.node.classList.toggle("chosen", chosen);
    entry.node.setAttribute("aria-pressed", chosen ? "true" : "false");
  }
  card.chip.hidden = !decision;
  card.chip.textContent = decision ? UI.stagedChip(decisionLabel(decision)) : "";
  card.applyNow.disabled = !decision || state.committing;
}

function showOutcome(card, text) {
  card.outcome.textContent = text || "";
  card.outcome.hidden = !text;
}

function stage(id, decision) {
  if (decision) state.staged.set(id, decision); else state.staged.delete(id);
  saveStaged();
  const card = state.queueCards.get(id);
  if (card) {
    showOutcome(card, "");
    paintCard(card);
  }
  updateGoBar();
}

function toggleStage(id, action, lane) {
  if (state.committing) return;
  const card = state.queueCards.get(id);
  // P25: a direct row cannot be staged as direct again (nor assigned to the direct lane).
  if (card && isDirectRow(card.item) && (action === "direct" || (action === "assign" && lane === state.config.direct_lane))) {
    status(UI.directAlready, true);
    return;
  }
  if (sameDecision(state.staged.get(id), action, lane)) {
    stage(id, null);
    return;
  }
  stage(id, { action, lane: action === "assign" ? lane : null,
    note: action === "done" && card ? card.note.value.trim() : "" });
}

function updateStagedNote(id, value) {
  const decision = state.staged.get(id);
  if (!decision || decision.action !== "done") return;
  decision.note = value.trim();
  saveStaged();
}

function stageSuggested(id) {
  const card = state.queueCards.get(id);
  const suggestion = card && card.item.suggestion;
  if (!suggestion || !state.config.lanes.includes(suggestion.lane)) {
    status(UI.noSuggestionToStage, true);
    return;
  }
  if (!sameDecision(state.staged.get(id), "assign", suggestion.lane)) toggleStage(id, "assign", suggestion.lane);
}

// P17: stage Done with the registry note; nothing is written until GO.
function stageRegistryDone(id, note) {
  if (state.committing || !note) return;
  const card = state.queueCards.get(id);
  if (card) card.note.value = note;
  stage(id, { action: "done", lane: null, note });
}

function stageRegistryClosed() {
  if (state.committing) return;
  for (const row of state.registryClosed) {
    if (row && row.id && row.note) stageRegistryDone(row.id, row.note);
  }
}

// P17: owner candidates from GET /api/owner, shown in a panel under the row.
function ownerPanel(data, onPick) {
  const c = UI.columns;
  const best = data.best;
  const parts = [el("div", { text: best ? UI.ownerBest(best.lane, best.confidence, best.source) : UI.ownerNone })];
  if (data.registry_row) parts.push(el("div", { className: "muted", text: UI.ownerRegistry(data.registry_row) }));
  parts.push(table([c.confidence, c.lane, c.session, c.source, c.evidence], data.candidates.map((row) => [
    row.confidence,
    row.lane && onPick && state.config.lanes.includes(row.lane)
      ? el("button", { text: row.lane, title: UI.ownerPick(row.lane), onclick: () => onPick(row.lane) })
      : (row.lane || (row.session ? "session:" + row.session : UI.none)),
    // P21: the sessions table title next to the session (only with console.sessions_table).
    row.session && row.session_title ? row.session + " (" + row.session_title + ")" : row.session,
    row.source, row.evidence])));
  return parts;
}

async function toggleOwner(box, channel, ts, onPick) {
  if (!box.hidden) {
    box.hidden = true;
    return;
  }
  let data;
  try {
    data = await api("/api/owner?channel=" + encodeURIComponent(channel || "") + (ts ? "&ts=" + encodeURIComponent(ts) : ""));
  } catch (error) {
    status(error.message, true);
    return;
  }
  clear(box).append(...ownerPanel(data, onPick));
  box.hidden = false;
}

function cancelAll() {
  if (state.committing) return;
  state.staged.clear();
  saveStaged();
  for (const card of state.queueCards.values()) {
    showOutcome(card, "");
    paintCard(card);
  }
  showCommitResults(null);
  updateGoBar();
}

function stagedRequest(id, decision) {
  return { id, action: decision.action, lane: decision.lane || null,
    note: decision.action === "done" && decision.note ? decision.note : null };
}

async function commitStaged() {
  if (state.committing || !state.staged.size) return;
  const decisions = Array.from(state.staged, ([id, decision]) => stagedRequest(id, decision));
  state.committing = true;
  updateGoBar();
  for (const card of state.queueCards.values()) paintCard(card);
  const results = [];
  const undos = [];
  try {
    for (let start = 0; start < decisions.length; start += MAX_COMMIT) {
      const data = await api("/api/queue/commit", { decisions: decisions.slice(start, start + MAX_COMMIT) });
      results.push(...data.results);
      if (data.undo) undos.push(data.undo);
    }
  } catch (error) {
    status(error.message, true);
  } finally {
    state.committing = false;
  }
  settle(results);
  showUndoToast(undos);
  loadQueue();
}

async function applyNowRow(id) {
  const decision = state.staged.get(id);
  if (!decision || state.committing) return;
  const request = stagedRequest(id, decision);
  let data;
  try {
    data = await api("/api/queue/actions", { action: request.action, ids: [id], lane: request.lane, note: request.note });
  } catch (error) {
    status(error.message, true);
    return;
  }
  settle(data.results.map((row) => ({ id: row.id, action: request.action, status: row.ok ? "applied"
    : (row.error === "conflict" || row.error === "not_found" ? row.error : "error"),
    detail: row.detail || (row.ok ? null : row.error), channel_hint: row.channel_hint, channel: row.channel,
    closed: row.closed })));
  showUndoToast([data.undo]);
  loadQueue();
}

function settle(results) {
  // applied: the row leaves; conflict and not_found: unstaged with the reason shown;
  // error: stays staged so GO can retry it.
  for (const row of results) {
    const card = state.queueCards.get(row.id);
    if (row.status !== "error") state.staged.delete(row.id);
    if (row.status === "applied") {
      if (card) card.node.remove();
      state.queueCards.delete(row.id);
    } else if (card) {
      showOutcome(card, UI.outcome(row.status, row.detail));
      paintCard(card);
    }
  }
  saveStaged();
  if (results.length) showCommitResults(results);
  updateGoBar();
}

function showCommitResults(results) {
  const box = clear(queueRoot().querySelector(".commit-results"));
  box.hidden = !results;
  if (!results) return;
  const counts = { applied: 0, conflict: 0, not_found: 0, error: 0 };
  for (const row of results) counts[row.status] = (counts[row.status] || 0) + 1;
  box.append(el("div", { text: UI.commitSummary(counts) }));
  for (const row of results.filter((item) => item.status !== "applied")) {
    box.append(el("div", { className: "commit-line " + row.status },
      el("span", { className: "mono", text: row.id }), " ", UI.outcome(row.status, row.detail)));
  }
  for (const row of results.filter((item) => item.status === "applied" && item.action === "exclude_channel")) {
    box.append(el("div", { className: "channel-hint" },
      UI.channelExcluded(channelText(row.channel), row.closed || 0) + " ",
      el("a", { href: "/proposals", text: UI.openScope, onclick: (event) => { event.preventDefault(); showView("proposals", true); } })));
  }
  const hinted = results.filter((row) => row.status === "applied" && row.channel_hint);
  if (hinted.length) {
    const names = Array.from(new Set(hinted.map((row) => channelText(row.channel))));
    box.append(el("div", { className: "channel-hint" },
      UI.channelHint + " (" + names.join(", ") + ") ",
      el("a", { href: "/proposals", text: UI.openScope, onclick: (event) => { event.preventDefault(); showView("proposals", true); } })));
  }
}

function focusPendingMessage() {
  if (!state.pendingMsg) return;
  const id = state.pendingMsg;
  state.pendingMsg = null;
  const card = state.queueCards.get(id);
  if (card) {
    card.node.scrollIntoView({ block: "center" });
    card.node.classList.add("flash");
    setTimeout(() => card.node.classList.remove("flash"), 4000);
  } else {
    runSearch(id);
  }
}

function onQueueKey(event) {
  if (state.view !== "queue" || event.ctrlKey || event.metaKey || event.altKey) return;
  const target = event.target;
  const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" ||
    target.tagName === "SELECT" || target.isContentEditable);
  if (event.key === "g" && !typing) {
    const go = document.querySelector(".go-button");
    if (go) {
      event.preventDefault();
      go.focus();
    }
    return;
  }
  const row = target && target.closest ? target.closest("article.card[data-id]") : null;
  if (!row || !state.queueCards.has(row.dataset.id) || state.committing) return;
  if (event.key === "Enter" && target === row) {
    event.preventDefault();
    stageSuggested(row.dataset.id);
  } else if (event.key === "Escape" && state.staged.has(row.dataset.id)) {
    event.preventDefault();
    stage(row.dataset.id, null);
  }
}

// ---------- lanes ----------

function laneSection() {
  return document.getElementById("view-lanes");
}

async function loadLanes() {
  let data;
  try {
    data = await api("/api/lanes");
  } catch (error) {
    status(error.message, true);
    return;
  }
  if (state.lane) return;
  const section = clear(laneSection());
  section.append(el("div", { className: "toolbar" },
    el("strong", { text: UI.lanesTitle }),
    el("span", { className: "muted small", text: UI.refreshed(nowText()) }),
    el("span", { className: "spacer" }),
    el("button", { text: UI.refresh, onclick: () => loadLanes() })));
  // P25: without a session the direct lane is not a lane card; its items are in the queue.
  const direct = data.direct_lane;
  if (direct && !direct.listed) {
    section.append(el("div", { className: "direct-link" },
      el("a", { href: "/", text: UI.directInQueue(state.config.operator_name) + (direct.queue_count ? " (" + direct.queue_count + ")" : ""),
        onclick: (event) => { event.preventDefault(); setQueueKind("direct"); showView("queue", true); } })));
  }
  section.append(el("div", { className: "grid" }, data.lanes.filter((card) => card.listed !== false).map(laneCard)));
}

function laneCard(card) {
  const session = card.session;
  const rows = [
    // P21: the session by its title, live name and id8, or a no session / paused / off badge.
    [UI.session, card.identity ? identityLine(card.identity) : el("span", {},
      el("span", { className: "dot" + (session.live ? " live" : "") }),
      session.value ? session.value + " " + UI.addressedBy(session.addressed_by) + " " + (session.live ? UI.live : UI.notLive) : UI.noSession)],
    [UI.today, UI.todayCounts(card.today)],
    [UI.open, String(card.open)],
    [UI.oldestUnacked, age(card.oldest_unacked_seconds)],
    [UI.lastDelivery, card.last_delivery || UI.none],
    [UI.lastReminder, card.last_reminder || UI.none],
    [UI.inboxFile, bytes(card.inbox_bytes)],
  ];
  return el("article", { className: "card" },
    el("div", { className: "meta" },
      el("strong", { text: card.lane }),
      // P25 fix1: the direct lane card says when its attached session is down.
      card.session_down ? el("span", { className: "badge bad", title: UI.sessionDownTitle, text: UI.sessionDown }) : null,
      card.paused ? el("span", { className: "badge warn", text: UI.paused }) : null,
      card.registered ? null : el("span", { className: "badge bad", text: UI.unregistered })),
    el("dl", { className: "kv small" }, rows.map(([key, value]) => [el("dt", { text: key }), el("dd", {}, value)])),
    el("div", { className: "actions" }, el("button", { className: "primary", text: UI.openInbox, onclick: () => openLane(card.lane) }),
      card.wake ? wakeButton(card.lane, card.wake) : null));
}

function openLane(lane) {
  state.lane = lane;
  loadLaneInbox();
}

function laneInputsBusy() {
  const active = document.activeElement;
  if (active && laneSection().contains(active) && (active.tagName === "INPUT" || active.tagName === "SELECT")) return true;
  return Array.from(laneSection().querySelectorAll("input[type=text]")).some((input) => input.value);
}

async function loadLaneInbox(force) {
  const lane = state.lane;
  if (!force && laneSection().querySelector(".lane-inbox") && (laneInputsBusy() || laneUndoActive())) return;
  undoState.inline.clear();
  let data;
  try {
    data = await api("/api/lanes/" + encodeURIComponent(lane) + "/inbox?full=" + (state.laneFull ? "true" : "false"));
  } catch (error) {
    status(error.message, true);
    return;
  }
  if (state.lane !== lane) return;
  state.laneWake = data.wake || null;
  const section = clear(laneSection());
  const full = el("input", { type: "checkbox", onchange: (event) => { state.laneFull = event.target.checked; loadLaneInbox(true); } });
  full.checked = state.laneFull;
  section.append(el("div", { className: "toolbar lane-inbox" },
    el("button", { text: UI.back, onclick: () => { state.lane = null; loadLanes(); } }),
    el("strong", { text: UI.laneInbox(lane) }),
    // P21: the lane session identity and Wake now in the header.
    data.identity ? identityLine(data.identity) : null,
    data.wake ? wakeButton(lane, data.wake) : null,
    el("span", { className: "muted small", text: UI.refreshed(nowText()) }),
    el("span", { className: "spacer" }),
    el("label", { className: "small" }, full, " " + UI.fullRoots),
    el("button", { text: UI.refresh, onclick: () => loadLaneInbox(true) })));
  if (!data.groups.length) {
    section.append(el("div", { className: "empty-state", text: UI.laneEmpty }));
    return;
  }
  for (const group of data.groups) section.append(laneGroup(lane, group));
}

function laneGroup(lane, group) {
  const root = group.root;
  const parts = [el("div", { className: "meta" },
    el("strong", { text: root.when_full || root.when || root.ts }),
    linkIcon(root.permalink),
    el("span", { title: root.channel, text: channelText({ id: root.channel, name: root.channel_name }) }),
    el("span", { title: root.author_id || "", text: root.author }),
    el("span", { className: "badge", text: UI.state(root.state) }),
    root.missing ? el("span", { className: "badge warn", text: UI.rootMissing }) : null,
    root.pending && root.kind ? el("span", { className: "badge", text: UI.kind(root.kind) }) : null,
    root.pending && root.edited === "yes" ? el("span", { className: "badge warn", text: UI.edited }) : null,
    root.pending && root.attention && root.attention !== "-" ? el("span", { className: "badge attention", text: UI.attention(root.attention) }) : null,
    group.registry ? el("span", { className: "badge registry" + (group.registry.closed ? " ok" : ""),
      title: UI.registryTitle(group.registry), text: UI.registryBadge(group.registry.status || UI.none) }) : null,
    el("span", { className: "mono small", text: root.id }))];
  if ("body" in root) parts.push(el("div", { className: "body" }, root.body));
  if (root.pending) parts.push(...laneActions(lane, root.id, root.channel, root.ts));
  for (const item of group.items) {
    parts.push(el("div", { className: "item" },
      el("div", { className: "meta small" },
        el("strong", { title: item.author_id || "", text: item.author }),
        el("span", { text: item.when_full || item.when }),
        linkIcon(item.permalink),
        el("span", { className: "badge", text: UI.state(item.state || UI.none) }),
        el("span", { className: "badge", text: UI.kind(item.kind) }),
        item.edited === "yes" ? el("span", { className: "badge warn", text: UI.edited }) : null,
        item.attention && item.attention !== "-" ? el("span", { className: "badge attention", text: UI.attention(item.attention) }) : null,
        el("span", { className: "mono", text: item.id })),
      "body" in item ? el("div", { className: "body" }, item.body) : null,
      laneActions(lane, item.id, item.channel || root.channel, item.ts)));
  }
  if (group.owner_replied_at) {
    parts.push(el("div", { className: "suggest small" }, el("span", { className: "badge ok", text: UI.ownerReplied(group.owner_replied_when || group.owner_replied_at) })));
  }
  return el("article", { className: "card" }, parts);
}

function laneActions(lane, id, channel, ts) {
  const note = el("input", { type: "text", placeholder: UI.notePlaceholder, maxlength: "2000" });
  const target = el("select", { "aria-label": UI.rerouteTo },
    state.config.lanes.filter((other) => other !== lane).map((other) => el("option", { value: other, text: other })));
  // P17: picking an owner candidate only sets the reroute target; Reroute applies it.
  const ownerBox = el("div", { className: "owner-panel small", hidden: true });
  const pick = (picked) => {
    if (picked === lane) return;
    target.value = picked;
    status(UI.ownerRerouteSet(picked));
  };
  const findOwner = state.config.owner_resolver && channel
    ? el("button", { className: "link", text: UI.findOwner, title: UI.findOwnerTitle,
      onclick: () => toggleOwner(ownerBox, channel, ts, pick) }) : null;
  // P19: done and reroute leave the row in place with "Handled · Undo" for the undo window.
  const row = el("div", { className: "actions" },
    el("button", { text: UI.ack, onclick: () => laneAction(lane, "ack", id) }),
    note,
    el("button", { text: UI.done, onclick: () => laneAction(lane, "done", id, { note: note.value }, row) }),
    el("span", { className: "muted small", text: UI.rerouteTo }),
    target,
    el("button", { text: UI.reroute, onclick: () => laneAction(lane, "reroute", id, { lane: target.value }, row) }),
    findOwner,
    wakeItemButton(lane, id));
  return [row, ownerBox];
}

// P21: wake the lane session for this one item; disabled with the reason while refused.
function wakeItemButton(lane, id) {
  const wake = state.laneWake;
  if (!wake) return null;
  const reason = wake.reason ? (UI.wakeRefused[wake.reason] || wake.detail || wake.reason) : null;
  return el("span", { className: "wake-wrap", title: wake.allowed ? null : reason },
    el("button", { className: "link wake-item", text: UI.wakeItem, disabled: !wake.allowed,
      title: wake.allowed ? UI.wakeItemTitle : reason, onclick: () => wakeLane(lane, [id]) }));
}

// P21: POST /api/lanes/{lane}/wake; the toast offers the P19 undo (removes unconsumed requests).
async function wakeLane(lane, ids) {
  let data;
  try {
    data = await api("/api/lanes/" + encodeURIComponent(lane) + "/wake", ids ? { ids } : {});
  } catch (error) {
    status(error.message, true);
    return;
  }
  const failed = (data.results || []).filter((row) => !row.ok);
  if (failed.length) {
    status(UI.actionFailed(failed.length, failed.map((row) => row.id + " " + row.error + (row.detail ? " (" + row.detail + ")" : "")).join("; ")), true);
  } else {
    status([UI.wakeQueued(data.queued), data.capped ? UI.wakeCapped(data.queued, data.total) : null,
      data.already_queued ? UI.wakeAlreadyQueued(data.already_queued) : null].filter(Boolean).join(" / "));
  }
  showUndoToast([data.undo]);
  if (state.view === "lanes" && state.lane) loadLaneInbox(true);
  else if (state.view === "lanes") loadLanes();
}

async function laneAction(lane, action, id, extra, row) {
  const body = Object.assign({ action, ids: [id], lane: null, note: null }, extra || {});
  if (body.note === "") body.note = null;
  let data;
  try {
    data = await api("/api/lanes/" + encodeURIComponent(lane) + "/actions", body);
  } catch (error) {
    status(error.message, true);
    return;
  }
  reportResults(data);
  showUndoToast([data.undo]);
  if (data.undo && row && action !== "ack") {
    markLaneRow(row, data.undo);
    return;
  }
  loadLaneInbox(true);
}

// ---------- undo (P19) ----------

function secondsUntil(deadline) {
  return Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
}

// Show the bottom toast for the latest write. `infos` are the `undo` objects of the
// responses (null when nothing changed); a GO of more than 200 decisions has several.
function showUndoToast(infos) {
  const list = (infos || []).filter(Boolean);
  if (!list.length) return;
  const last = list[list.length - 1];
  const count = list.reduce((total, info) => total + (info.count || 0), 0);
  const name = UI.journalActions[last.action] || last.action;
  // P21: a lane wake reads "Wake requests: N" (`깨우기 요청 N건`).
  const text = last.action === "lane_wake" ? UI.wakeQueued(count) : UI.didAction(name, count, last.lane);
  undoState.toast = { ids: list.map((info) => info.action_id), text,
    deadline: Date.now() + (last.seconds_left || 0) * 1000, message: null, messageUntil: 0, button: null };
  renderToast();
  startUndoTicker();
}

function renderToast() {
  const node = document.getElementById("toast");
  if (!node) return;
  const toast = undoState.toast;
  clear(node);
  if (!toast) {
    node.hidden = true;
    return;
  }
  if (toast.message) {
    node.append(el("span", { text: toast.message }));
  } else {
    toast.button = el("button", { className: "link toast-undo", disabled: undoState.busy,
      text: undoState.busy ? UI.undoing : UI.undoButton(secondsUntil(toast.deadline)),
      onclick: () => undoActions(toast.ids) });
    node.append(el("span", { text: toast.text }), " · ", toast.button);
  }
  node.hidden = false;
}

function laneUndoActive() {
  for (const node of Array.from(undoState.inline.keys())) {
    if (!node.isConnected) undoState.inline.delete(node);
  }
  return undoState.inline.size > 0;
}

// A lane row that was done or rerouted keeps its place with "Handled · Undo (Ns)" while
// the window is open; the lane inbox is reloaded when it ends.
function markLaneRow(row, info) {
  const deadline = Date.now() + (info.seconds_left || 0) * 1000;
  const button = el("button", { className: "link", text: UI.undoButton(secondsUntil(deadline)),
    onclick: () => undoActions([info.action_id]) });
  clear(row).append(el("span", { className: "badge ok", text: UI.laneHandled }), " · ", button);
  row.classList.add("handled");
  undoState.inline.set(row, { id: info.action_id, deadline, button });
  startUndoTicker();
}

function startUndoTicker() {
  if (undoState.ticker) return;
  undoState.ticker = setInterval(undoTick, 500);
}

function undoTick() {
  const toast = undoState.toast;
  if (toast) {
    if (toast.message ? Date.now() > toast.messageUntil : secondsUntil(toast.deadline) <= 0) {
      undoState.toast = null;
      renderToast();
    } else if (!toast.message && toast.button && !undoState.busy) {
      toast.button.textContent = UI.undoButton(secondsUntil(toast.deadline));
    }
  }
  let expired = false;
  for (const [node, entry] of Array.from(undoState.inline)) {
    if (!node.isConnected) {
      undoState.inline.delete(node);
    } else if (secondsUntil(entry.deadline) <= 0) {
      undoState.inline.delete(node);
      expired = true;
    } else {
      entry.button.textContent = UI.undoButton(secondsUntil(entry.deadline));
    }
  }
  if (expired && state.view === "lanes" && state.lane && !laneUndoActive()) loadLaneInbox(true);
  if (!undoState.toast && !undoState.inline.size) {
    clearInterval(undoState.ticker);
    undoState.ticker = null;
  }
}

// Undo journal rows newest first and show the summed per-id result in the toast.
async function undoActions(ids) {
  if (undoState.busy || !ids || !ids.length) return;
  undoState.busy = true;
  renderToast();
  const totals = { restored: 0, conflict: 0, not_found: 0, wake_sent: 0, wake_removed: 0, binding_conflict: 0 };
  let failure = null;
  for (const id of ids.slice().reverse()) {
    try {
      const data = await api("/api/undo/" + encodeURIComponent(id), {});
      for (const key of Object.keys(totals)) totals[key] += (data.counts && data.counts[key]) || 0;
    } catch (error) {
      failure = error.message;
    }
  }
  undoState.busy = false;
  const done = new Set(ids);
  for (const [node, entry] of Array.from(undoState.inline)) {
    if (done.has(entry.id)) undoState.inline.delete(node);
  }
  const text = UI.undoResult(totals) + (failure ? " / " + UI.undoFailed(failure) : "");
  undoState.toast = { ids: [], text, deadline: 0, message: text, messageUntil: Date.now() + 8000, button: null };
  renderToast();
  startUndoTicker();
  if (state.view === "lanes" && state.lane) loadLaneInbox(true);
  else refresh();
}

function onUndoKey(event) {
  if (event.key !== "z" || event.ctrlKey || event.metaKey || event.altKey) return;
  const target = event.target;
  const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" ||
    target.tagName === "SELECT" || target.isContentEditable);
  const toast = undoState.toast;
  if (typing || !toast || toast.message || undoState.busy || secondsUntil(toast.deadline) <= 0) return;
  event.preventDefault();
  undoActions(toast.ids);
}

function recentActionsCard(data) {
  const c = UI.columns;
  const card = el("article", { className: "card" }, el("h2", { text: UI.recentActions }));
  const rows = (data && data.actions) || [];
  if (!rows.length) {
    card.append(el("div", { className: "muted", text: UI.recentEmpty }));
    return card;
  }
  card.append(table([c.at, c.action, c.items, c.actor, c.undo], rows.map((row) => [
    el("span", { title: row.when || null, text: row.time || UI.none }),
    (UI.journalActions[row.action] || row.action) + (row.lane ? " (" + row.lane + ")" : ""),
    // P20: link icons for the first message ids (title = id), when permalinks are on.
    (row.links || []).length
      ? el("span", {}, String(row.count), " ", (row.links || []).map((link) =>
        el("span", { title: link.id }, linkIcon(link.permalink))))
      : row.count,
    row.by,
    row.undone_at ? el("span", { className: "badge", text: UI.undoUndone })
      : row.undoable ? el("button", { text: UI.undoButton(row.seconds_left), onclick: () => undoActions([row.id]) })
        : el("span", { className: "muted", text: UI.undoExpired })])));
  return card;
}

// ---------- service ----------

function table(headers, rows) {
  return el("table", {},
    el("thead", {}, el("tr", {}, headers.map((header) => el("th", { text: header })))),
    el("tbody", {}, rows.map((row) => el("tr", {}, row.map((cell) =>
      cell instanceof Node ? el("td", {}, cell) : el("td", { text: cell === null || cell === undefined ? UI.none : String(cell) }))))));
}

function yesNo(value) {
  return value ? UI.yes : UI.no;
}

async function loadService() {
  let data, recent;
  try {
    [data, recent] = await Promise.all([api("/api/service"), api("/api/actions?limit=20")]);
  } catch (error) {
    status(error.message, true);
    return;
  }
  const c = UI.columns;
  const section = clear(document.getElementById("view-service"));
  section.append(el("div", { className: "toolbar" },
    el("strong", { text: UI.serviceTitle }),
    el("span", { className: "muted small", text: UI.refreshed(nowText()) }),
    el("span", { className: "spacer" }),
    el("button", { text: UI.refresh, onclick: () => loadService() })));
  section.append(recentActionsCard(recent));
  const hb = data.heartbeat;
  const heartbeat = el("article", { className: "card" }, el("h2", { text: UI.heartbeat }));
  if (!hb) {
    heartbeat.append(el("div", { className: "muted", text: UI.noHeartbeat }));
  } else {
    heartbeat.append(el("div", { className: "meta" },
      el("span", { className: "badge " + (hb.ok ? "ok" : "bad"), text: String(hb.status) }),
      el("span", { className: "badge " + (hb.stale ? "bad" : "ok"), text: hb.stale ? UI.stale : UI.fresh }),
      el("span", { text: UI.age(age(hb.age_seconds)) })));
    const fields = ["ok", "pid", "version", "ts", "started_at", "last_event_at", "last_catchup_at", "gaps"];
    heartbeat.append(table([c.key, c.value], fields.map((key) => [key, typeof hb[key] === "boolean" ? yesNo(hb[key]) : hb[key]])));
    heartbeat.append(el("h2", { text: UI.counters }),
      table([c.key, c.value], Object.entries(hb.counters || {}).map(([key, value]) => [key, value])));
  }
  section.append(heartbeat);
  section.append(el("article", { className: "card" },
    el("h2", { text: UI.wake }),
    el("div", { className: "meta" },
      el("span", { className: "badge " + (data.wake_off ? "warn" : "ok"), text: data.wake_off ? UI.wakeOff : UI.wakeOn }),
      el("span", { text: UI.wakeConfig(data.wake_enabled_in_config) })),
    el("h2", { text: UI.humanQueue }),
    el("div", { className: "muted small", text: data.human_queue.mode + " / " + data.human_queue.digest_seconds + UI.units.s }),
    el("h2", { text: UI.adapters }),
    table([c.index, c.kind, c.purpose, c.dryRun], data.adapters.map((row) => [row.index, row.kind, row.purpose, yesNo(row.dry_run)]))));
  section.append(el("article", { className: "card" },
    el("h2", { text: UI.reconcile }),
    data.reconcile_passes.length
      ? table([c.at, c.counts], data.reconcile_passes.map((row) => [row.at,
        el("span", { className: "mono", text: Object.entries(row.counts).map(([key, value]) => key + "=" + value).join(" ") })]))
      : el("div", { className: "muted", text: UI.noReconcile }),
    el("h2", { text: UI.warnings }),
    data.warnings.length
      ? table([c.at, c.level, c.logger, c.message], data.warnings.map((row) => [row.at, row.level, row.logger,
        el("span", { className: "mono", text: row.message })]))
      : el("div", { className: "muted", text: UI.noWarnings }),
    el("h2", { text: UI.gaps }),
    data.gaps.length
      ? table([c.id, c.source, c.detected, c.lastEvent, c.resumed, c.catchup, c.note], data.gaps.map((row) =>
        [row.id, row.source, row.detected_at, row.last_event_at, row.resumed_at, row.catchup_at, row.note]))
      : el("div", { className: "muted", text: UI.noGaps })));
}

// ---------- scope proposals (P13) ----------

async function loadProposals() {
  let data;
  try {
    data = await api("/api/proposals");
  } catch (error) {
    status(error.message, true);
    return;
  }
  const c = UI.columns;
  const section = clear(document.getElementById("view-proposals"));
  section.append(el("div", { className: "toolbar" },
    el("strong", { text: UI.proposalsTitle(data.proposals.length, data.min_evidence) }),
    el("span", { className: "muted small", text: UI.refreshed(nowText()) }),
    el("span", { className: "spacer" }),
    el("button", { text: UI.refresh, onclick: () => loadProposals() })));
  // The lookup card node is kept across polls so typed input and results survive a refresh.
  if (state.config.owner_resolver) {
    if (!state.ownerCard) state.ownerCard = ownerLookupCard();
    section.append(state.ownerCard);
  }
  // P21: DM proposals and DM claims are listed under their own headings with the partner name.
  const isDm = (row) => row.kind === "dm";
  const partnerName = (row) => row.partner && row.partner.id ? nameSpan(row.partner) : UI.none;
  const proposalTable = (rows, dm) => table([c.kind, c.scopeKey, c.name].concat(dm ? [c.partner] : [],
    [c.lane, c.evidence, c.status, c.lanes, c.current, c.firstSeen, c.lastSeen, c.actions]), rows.map((row) => [
    row.kind, el("span", { className: "mono", text: row.key }), scopeName(row.name)].concat(dm ? [partnerName(row)] : [], [
    row.lane, row.evidence_count,
    el("span", {}, row.status,
      row.conflict ? el("span", { className: "badge bad", text: UI.conflict }) : null,
      row.effective ? el("span", { className: "badge ok", text: UI.effective }) : null,
      row.imported ? el("span", { className: "badge", text: UI.imported }) : null),
    row.lanes.map((item) => item.lane + " " + item.evidence_count).join(", "),
    row.current ? UI.current(row.current.lane === null ? UI.none : row.current.lane, row.current.source) : UI.none,
    row.first_seen, row.last_seen,
    el("span", {}, ["confirm", "reject", "defer"].map((action) =>
      el("button", { text: UI[action], onclick: () => proposalAction(action, row) })))])));
  const list = el("article", { className: "card" });
  const others = data.proposals.filter((row) => !isDm(row));
  const dmRows = data.proposals.filter(isDm);
  if (!data.proposals.length) {
    list.append(el("div", { className: "muted", text: UI.proposalsEmpty }));
  } else {
    if (others.length) list.append(proposalTable(others, false));
    if (dmRows.length) list.append(el("h2", { text: UI.dmProposalsTitle }), proposalTable(dmRows, true));
  }
  section.append(list);
  const learned = el("article", { className: "card" }, el("h2", { text: UI.learnedTitle }));
  const learnedTable = (rows, dm) => table([c.kind, c.scopeKey, c.name].concat(dm ? [c.partner] : [],
    [c.lane, c.by, c.at, c.inConfig]), rows.map((row) =>
    [row.kind, el("span", { className: "mono", text: row.key }), scopeName(row.name)].concat(dm ? [partnerName(row)] : [],
      [row.lane, row.confirmed_by, row.confirmed_at, yesNo(row.in_config)])));
  if (!data.learned.length) {
    learned.append(el("div", { className: "muted", text: UI.learnedEmpty }));
  } else {
    const plain = data.learned.filter((row) => !isDm(row));
    const dmClaims = data.learned.filter(isDm);
    if (plain.length) learned.append(learnedTable(plain, false));
    if (dmClaims.length) learned.append(el("h2", { text: UI.dmClaimsTitle }), learnedTable(dmClaims, true));
  }
  const output = el("pre", { className: "mono", hidden: true });
  learned.append(el("div", { className: "toolbar" },
    el("button", { text: UI.exportButton, onclick: () => exportScope(output) }),
    el("span", { className: "muted small", text: UI.exportHint })), output);
  section.append(learned);
  section.append(exclusionsCard(data.channel_exclusions || []));
}

// P17: standalone owner lookup (GET /api/owner) with a channel id and an optional ts.
function ownerLookupCard() {
  const channel = el("input", { type: "text", maxlength: "100", placeholder: UI.ownerChannelPlaceholder });
  const ts = el("input", { type: "text", maxlength: "40", placeholder: UI.ownerTsPlaceholder });
  const box = el("div", { className: "owner-panel small", hidden: true });
  const run = async (event) => {
    event.preventDefault();
    box.hidden = true;
    await toggleOwner(box, channel.value.trim(), ts.value.trim(), null);
  };
  return el("article", { className: "card" }, el("h2", { text: UI.ownerLookupTitle }),
    el("form", { className: "toolbar", onsubmit: run }, channel, ts,
      el("button", { type: "submit", text: UI.ownerLookupButton }),
      el("span", { className: "muted small", text: UI.ownerLookupHint })), box);
}

// P15: ledger channel exclusions written by the triage `Exclude channel` action.
function exclusionsCard(rows) {
  const c = UI.columns;
  const card = el("article", { className: "card" }, el("h2", { text: UI.exclusionsTitle }));
  if (!rows.length) {
    card.append(el("div", { className: "muted", text: UI.exclusionsEmpty }));
  } else {
    card.append(table([c.channel, c.name, c.by, c.at, c.inConfig, c.actions], rows.map((row) =>
      [el("span", { className: "mono", text: row.channel }), scopeName(row.name), row.by, row.excluded_at,
        yesNo(row.in_config), el("button", { text: UI.undo, onclick: () => undoExclusion(row.channel) })])));
  }
  const output = el("pre", { className: "mono", hidden: true });
  card.append(el("div", { className: "toolbar" },
    el("button", { text: UI.exportExclusions, onclick: () => exportExclusions(output) }),
    el("span", { className: "muted small", text: UI.exportHint })), output);
  return card;
}

async function undoExclusion(channel) {
  try {
    await api("/api/channel-exclusions/undo", { channel });
  } catch (error) {
    status(error.message, true);
    return;
  }
  status(UI.undoDone(channel));
  loadProposals();
}

async function exportExclusions(output) {
  let data;
  try {
    data = await api("/api/channel-exclusions/export");
  } catch (error) {
    status(error.message, true);
    return;
  }
  output.textContent = data.yaml;
  output.hidden = false;
}

async function proposalAction(action, row) {
  let data;
  try {
    data = await api("/api/proposals/actions", { action, kind: row.kind, key: row.key, lane: row.lane });
  } catch (error) {
    status(error.message, true);
    return;
  }
  status(UI.proposalDone(data.status));
  loadProposals();
}

async function exportScope(output) {
  let data;
  try {
    data = await api("/api/proposals/export");
  } catch (error) {
    status(error.message, true);
    return;
  }
  output.textContent = data.yaml;
  output.hidden = false;
}

// ---------- search ----------

async function runSearch(query) {
  query = (query || "").trim();
  if (query.length < 2) return;
  let data;
  try {
    data = await api("/api/search?q=" + encodeURIComponent(query));
  } catch (error) {
    status(error.message, true);
    return;
  }
  if (state.view !== "search") state.previousView = state.view;
  state.view = "search";
  for (const section of document.querySelectorAll(".view")) section.hidden = section.id !== "view-search";
  const c = UI.columns;
  const section = clear(document.getElementById("view-search"));
  section.append(el("div", { className: "toolbar" },
    el("strong", { text: UI.searchTitle(data.query) }),
    el("span", { className: "spacer" }),
    el("button", { text: UI.close, onclick: () => showView(state.previousView, false) })));
  if (!data.rows.length) {
    section.append(el("div", { className: "empty-state", text: UI.searchEmpty }));
    return;
  }
  if (data.truncated) section.append(el("div", { className: "muted small", text: UI.searchTruncated }));
  section.append(el("article", { className: "card" }, table(
    [c.id, c.when, c.channel, c.author, c.state, c.lane, c.reason, c.note, c.text],
    data.rows.map((row) => [el("span", {}, el("span", { className: "mono", text: row.id }), " ", linkIcon(row.permalink)),
      row.when, nameSpan(row.channel), nameSpan(row.author),
      row.state, row.lane, row.route_reason, row.note,
      row.snippet === null ? el("span", { className: "muted", text: UI.noBody }) : row.snippet + (row.truncated ? " " + UI.truncated : "")]))));
}

// ---------- deliveries (P18) ----------

VIEWS["/deliveries"] = "deliveries";
const DELIVERY_WINDOWS = [["h24", 24], ["h48", 48], ["d7", 168], ["all", 0]];
const DELIVERY_STATUS_OPTIONS = ["sent", "failed", "skipped", "dry_run", "timeout"];
const deliveries = { lane: "", purpose: "", status: "", hours: 48, page: 1, open: new Set() };

function minutes(seconds) {
  return Math.max(0, Math.round((seconds || 0) / 60));
}

function deliverySelect(label, value, options, onchange) {
  return el("label", { className: "small muted" }, label + " ",
    el("select", { onchange: (event) => onchange(event.target.value) },
      options.map(([key, text]) => el("option", { value: key, selected: String(key) === String(value), text }))));
}

function deliveryFilters() {
  const f = UI.deliveryFilters;
  const all = ["", UI.deliveryAll];
  const lanes = (state.config.lanes || []).map((lane) => [lane, lane]).concat([["human_queue", UI.deliveryPurposes.human_queue]]);
  const set = (key) => (value) => {
    deliveries[key] = key === "hours" ? Number(value) : value;
    deliveries.page = 1;
    loadDeliveries(true);
  };
  return el("div", { className: "toolbar" },
    deliverySelect(f.lane, deliveries.lane, [all].concat(lanes), set("lane")),
    deliverySelect(f.purpose, deliveries.purpose, [all, ["lane", UI.deliveryPurposes.lane], ["human_queue", UI.deliveryPurposes.human_queue]], set("purpose")),
    deliverySelect(f.status, deliveries.status, [all].concat(DELIVERY_STATUS_OPTIONS.map((key) => [key, UI.deliveryStatuses[key]])), set("status")),
    deliverySelect(f.hours, deliveries.hours, DELIVERY_WINDOWS.map(([key, hours]) => [hours, UI.deliveryWindows[key]]), set("hours")));
}

function deliveryTarget(row) {
  if (row.target) {
    // P21: the sessions table title leads when known (only with console.sessions_table).
    return el("span", { title: row.target.session_id || null },
      row.target.title ? el("span", { className: "session-title", text: row.target.title + " " }) : null,
      row.target.name || UI.none,
      row.target.short_id ? el("span", { className: "mono muted", text: " " + row.target.short_id }) : null);
  }
  if (row.adapter && row.adapter !== "session_message") return el("span", { className: "muted", text: row.adapter });
  return el("span", { className: "muted", text: UI.noTargetSession });
}

function deliveryItems(row) {
  const details = el("details", { className: "context", open: deliveries.open.has(row.id) },
    el("summary", { text: String(row.item_count) }),
    el("ul", { className: "delivery-items" }, row.items.map((item) => el("li", {},
      el("span", { className: "mono", text: item.id }), " ", linkIcon(item.permalink), " ",
      nameSpan(item.channel), " · ", nameSpan(item.author),
      item.is_reply ? el("span", { className: "badge", text: UI.itemThread }) : null,
      item.missing ? el("span", { className: "badge warn", text: UI.itemMissing }) : null))));
  details.addEventListener("toggle", () => {
    if (details.open) deliveries.open.add(row.id); else deliveries.open.delete(row.id);
  });
  return details;
}

function deliveryResult(row) {
  const value = row.status || "";
  const tone = value === "sent" ? "ok" : (value === "failed" || value === "timeout") ? "bad" : value.startsWith("skipped") ? "warn" : "";
  const outcome = row.relay_outcome;
  const outcomeTone = outcome === "delivered" ? "ok" : outcome === "not_delivered" ? "bad" : "";
  return el("span", { title: row.detail || null },
    el("span", { className: "badge " + tone, text: UI.deliveryStatuses[value] || value }),
    outcome ? el("span", { className: "badge " + outcomeTone, text: UI.relayOutcomes[outcome] || outcome }) : null,
    row.repeat_count > 1 ? el("span", { className: "badge warn", text: UI.repeated(row.repeat_count, row.last_seen_time) }) : null);
}

function deliveryReceipt(row) {
  const r = row.receipt || {};
  if (row.purpose !== "lane") return r.decision_time ? UI.decisionAt(r.decision_time) : UI.none;
  if (r.received_time) return el("span", { className: "badge ok", text: UI.receivedAt(r.received_time, minutes(r.latency_seconds)) });
  if (row.status === "sent") return el("span", { className: "badge warn", text: UI.unconfirmedFor(minutes(r.age_seconds)) });
  return UI.none;
}

function deliveryCost(row) {
  const parts = [];
  if (row.cost_usd !== null && row.cost_usd !== undefined) parts.push("$" + Number(row.cost_usd).toFixed(4));
  if (row.duration_ms !== null && row.duration_ms !== undefined) parts.push((row.duration_ms / 1000).toFixed(1) + UI.units.s);
  return parts.length ? parts.join(" · ") : UI.none;
}

async function loadDeliveries(force) {
  const section = document.getElementById("view-deliveries");
  // A poll must not close a filter list the operator has open.
  if (!force && section.contains(document.activeElement) && document.activeElement.tagName === "SELECT") return;
  const query = new URLSearchParams({ lane: deliveries.lane, purpose: deliveries.purpose, status: deliveries.status,
    hours: String(deliveries.hours), page: String(deliveries.page) });
  let data, summary;
  try {
    [data, summary] = await Promise.all([api("/api/deliveries?" + query.toString()),
      api("/api/deliveries/summary?hours=" + deliveries.hours)]);
  } catch (error) {
    status(error.message, true);
    return;
  }
  const c = UI.deliveryColumns;
  clear(section);
  const first = data.total ? (data.page - 1) * data.page_size + 1 : 0;
  const last = Math.min(data.total, data.page * data.page_size);
  section.append(el("div", { className: "toolbar" },
    el("strong", { text: UI.deliveriesTitle(data.total) }),
    el("span", { className: "muted small", text: UI.refreshed(nowText()) }),
    el("span", { className: "spacer" }),
    el("span", { className: "muted small", text: data.total ? UI.showing(first, last, data.total) : "" }),
    el("button", { text: UI.previous, disabled: data.page <= 1, onclick: () => { deliveries.page -= 1; loadDeliveries(true); } }),
    el("button", { text: UI.next, disabled: last >= data.total, onclick: () => { deliveries.page += 1; loadDeliveries(true); } }),
    el("button", { text: UI.refresh, onclick: () => loadDeliveries(true) })));
  section.append(el("div", { className: "meta delivery-summary" }, el("strong", { text: UI.deliveriesSummary(summary) })));
  section.append(deliveryFilters());
  if (!data.rows.length) {
    // P20: an empty window is not an error: say when the log started and how many wakes the
    // older per-thread record holds for this window (no details).
    section.append(el("div", { className: "empty-state" },
      el("div", { text: UI.deliveriesEmpty }),
      data.log_started_when ? el("div", { className: "muted small", title: data.log_started_at || null,
        text: UI.deliveryLogStarted(data.log_started_when) }) : null,
      data.legacy_deliveries ? el("div", { className: "muted small", text: UI.legacyDeliveries(data.legacy_deliveries) }) : null));
    return;
  }
  section.append(el("article", { className: "card" }, table(
    [c.time, c.kind, c.lane, c.target, c.items, c.result, c.receipt, c.cost],
    data.rows.map((row) => [el("span", { title: row.when || null, text: row.time || UI.none }),
      UI.deliveryKinds[row.kind] || row.kind,
      row.purpose === "human_queue" ? UI.deliveryPurposes.human_queue : row.lane,
      deliveryTarget(row), deliveryItems(row), deliveryResult(row), deliveryReceipt(row), deliveryCost(row)]))));
}

// ---------- start ----------

function applyStaticText() {
  document.title = UI.title;
  document.documentElement.lang = UI === UI_TEXT.ko ? "ko" : "en";
  for (const node of document.querySelectorAll("[data-text]")) node.textContent = UI[node.dataset.text];
  for (const node of document.querySelectorAll("[data-placeholder]")) node.placeholder = UI[node.dataset.placeholder];
  for (const node of document.querySelectorAll("[data-aria]")) node.setAttribute("aria-label", UI[node.dataset.aria]);
}

function readHash() {
  const match = /^#msg=(.+)$/.exec(location.hash);
  if (match) state.pendingMsg = decodeURIComponent(match[1]);
}

function trackTopHeight() {
  // The GO bar sticks just below the sticky page header, whose height wraps with the width.
  const header = document.querySelector("header.top");
  const set = () => document.documentElement.style.setProperty("--top-height", header.offsetHeight + "px");
  set();
  window.addEventListener("resize", set);
}

async function start() {
  readHash();
  try {
    state.config = await api("/api/config");
  } catch (error) {
    applyStaticText();
    status(error.message, true);
    return;
  }
  UI = UI_TEXT[state.config.ui_language] || UI_TEXT.en;
  applyStaticText();
  state.staged = loadStaged();
  loadQueueKind();
  trackTopHeight();
  if (state.config.timezone_warning) status(state.config.timezone_warning, true);
  for (const link of document.querySelectorAll(".tabs a")) {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      if (link.dataset.view === "lanes") state.lane = null;
      showView(link.dataset.view, true);
    });
  }
  document.getElementById("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    runSearch(document.getElementById("search-input").value);
  });
  document.addEventListener("keydown", onQueueKey);
  document.addEventListener("keydown", onUndoKey);
  window.addEventListener("popstate", () => showView(VIEWS[location.pathname] || "queue", false));
  window.addEventListener("hashchange", () => {
    readHash();
    if (state.view === "queue") focusPendingMessage(); else showView("queue", true);
  });
  showView(VIEWS[location.pathname] || "queue", false);
  setInterval(() => { if (!document.hidden) refresh(); }, state.config.poll_seconds * 1000);
}

start();
