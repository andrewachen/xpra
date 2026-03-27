#!/bin/bash
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Watch an in-progress xpra Windows CI build and print artifact links on success.
# ABOUTME: Falls back to most recent successful build if none are active. Uses whiptail to select if multiple.

set -euo pipefail

REPO="andrewachen/xpra"
WORKFLOW="windows.yml"

# Terminal-aware dialog sizing: truncate labels to fit whiptail chrome
TERM_COLS=$(tput cols 2>/dev/null || echo 120)
DIALOG_WIDTH=$(( TERM_COLS - 4 ))
(( DIALOG_WIDTH > 160 )) && DIALOG_WIDTH=160
(( DIALOG_WIDTH < 40 )) && DIALOG_WIDTH=40
# Leave room for whiptail border (4) + run ID tag column (~12) + padding (4)
MAX_LABEL=$(( DIALOG_WIDTH - 20 ))

truncate_label() {
    local label="$1"
    if (( ${#label} > MAX_LABEL )); then
        echo "${label:0:$((MAX_LABEL - 1))}…"
    else
        echo "$label"
    fi
}

# Elapsed time as human-readable "Xs" / "Xm" / "Xh Ym"
ELAPSED_JQ='((now - (.created_at | fromdateiso8601)) | floor) as $s |
  if $s < 60 then "\($s)s"
  elif $s < 3600 then "\($s / 60 | floor)m"
  else "\($s / 3600 | floor)h \($s % 3600 / 60 | floor)m"
  end'
LABEL_JQ='"\(.head_branch)  \(.head_sha[0:7])  \('"$ELAPSED_JQ"')  —  \(.head_commit.message | split("\n")[0])"'

RUNS_JSON=$(jq -s '(.[0].workflow_runs + .[1].workflow_runs) | sort_by(.created_at) | reverse' \
    <(gh api "/repos/$REPO/actions/workflows/$WORKFLOW/runs?status=in_progress&per_page=10") \
    <(gh api "/repos/$REPO/actions/workflows/$WORKFLOW/runs?status=queued&per_page=10"))

COUNT=$(echo "$RUNS_JSON" | jq 'length')

show_artifacts() {
    local run_id="$1"
    echo "Artifact links:"
    echo
    gh api "/repos/$REPO/actions/runs/$run_id/artifacts" \
        --jq ".artifacts[] | \"  \(.name): https://github.com/$REPO/actions/runs/$run_id/artifacts/\(.id)\""
}

if [ "$COUNT" -eq 0 ]; then
    echo "No Windows builds in progress or queued."
    echo
    IFS=$'\t' read -r LATEST_ID LATEST_LABEL < <(
        gh api "/repos/$REPO/actions/workflows/$WORKFLOW/runs?status=success&per_page=1" \
            --jq ".workflow_runs[0] | [(.id | tostring), $LABEL_JQ] | @tsv")
    echo "Most recent successful build:"
    echo "  $LATEST_LABEL"
    echo
    show_artifacts "$LATEST_ID"
    exit 0
fi

if [ "$COUNT" -eq 1 ]; then
    RUN_ID=$(echo "$RUNS_JSON" | jq -r '.[0].id')
    LABEL=$(echo "$RUNS_JSON" | jq -r ".[0] | $LABEL_JQ")
    echo "Watching: $LABEL"
else
    MENU_ITEMS=()
    while IFS=$'\t' read -r id label; do
        MENU_ITEMS+=("$id" "$(truncate_label "$label")")
    done < <(echo "$RUNS_JSON" | jq -r ".[] | [(.id | tostring), $LABEL_JQ] | @tsv")
    LIST_HEIGHT=$(( COUNT < 10 ? COUNT : 10 ))
    RUN_ID=$(whiptail --menu "Select build to watch" 15 "$DIALOG_WIDTH" "$LIST_HEIGHT" "${MENU_ITEMS[@]}" 3>&1 1>&2 2>&3) || exit 1
    LABEL=$(echo "$RUNS_JSON" | jq -r --arg id "$RUN_ID" ".[] | select(.id == (\$id | tonumber)) | $LABEL_JQ")
    echo "Watching: $LABEL"
fi

echo
trap '' INT
if ( trap - INT; gh run watch --repo "$REPO" --exit-status "$RUN_ID" ); then
    trap - INT
    echo
    show_artifacts "$RUN_ID"
elif [ $? -eq 130 ]; then
    echo
    echo "Interrupted."
    exit 130
else
    echo
    echo "Build failed: https://github.com/$REPO/actions/runs/$RUN_ID"
    exit 1
fi
