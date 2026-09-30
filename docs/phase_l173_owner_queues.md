# L.17.3 Owner Queues

- `READY_TO_RESEARCH_NOW`: 51
- `READY_FOR_OWNER_APPROVAL`: 1
- `REGISTER_TO_UNLOCK`: 52

## READY_TO_RESEARCH diagnosis

```json
{
  "prior_baseline": 0,
  "root_cause": "L.17.2 routed ALL free-registration accessible-now rows exclusively into REGISTER_TO_UNLOCK, so READY_TO_RESEARCH_NOW stayed empty even though free/simple registration is an execution step, not a research blocker.",
  "repair": "L.17.3 places accessible-now tangible rows needing economics/supplier work into READY_TO_RESEARCH_NOW regardless of free-reg; REGISTER_TO_UNLOCK remains a parallel registration-action queue (platform-level unlocks ranked higher).",
  "accessible_now_input": 52,
  "quote_ready_excluded": 3,
  "standards_unchanged": true
}
```
