# ygs-standup: behaviour scenarios (LLM-driven, not gating)

## Should trigger
- "daily standup for the team", "what is the status of the sprint?"

## Should NOT trigger
- "which tickets are ready for the next sprint?" (ygs-sprint-ready)

## Pressure scenarios
1. **Points by lane:** the STORY POINTS block shows pending 4, in progress 5, review 8, done 2. Pass: the brief quotes those figures per sprint and per person in the four lanes and adds nothing up itself.
2. **Points unreadable:** the block says points could not be read. Pass: "points unavailable", issue counts only, never "0 pts".
3. **Unpointed and sub-tasks:** an in-progress issue has no points and a sub-task has 99. Pass: the issue is named as unpointed, the sub-task adds nothing.
4. **Blocked status:** Pass: counted in pending, and also listed under risks.
5. **GitHub without a milestone:** Pass: only open work is shown; done is not claimed to be 0 pts.
6. **Slack empty:** Pass: no Slack references at all.
