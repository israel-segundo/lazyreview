# Future: voice + chat consensus before publish

Design-only roadmap. **Not implemented** in the consolidation/publish Phase 1 work.

## Goal

Let a human (and later voice) refine analysis + findings + the internal consolidation
report **before** the publish transform runs, so the GitHub draft reflects a settled
consensus rather than the first agent pass.

## Suggested sequence

1. **Text chat consensus** over analysis brief, review findings, and the internal
   consolidation report (`body.md` + `meta.json`).
2. **Mutate / freeze** the consensus report (and optionally findings) when the human
   is done editing via chat.
3. **Re-run publish transform** against the frozen report → new `publish_body`.
4. Human confirms in the existing submit modal (never auto-post).

```mermaid
flowchart LR
  analysis[PR_analysis]
  findings[Review_findings]
  consol[Internal_report]
  chat[Text_chat_consensus]
  freeze[Freeze_report]
  publish[Publish_transform]
  draft[publish_body]
  human[Submit_modal]
  analysis --> chat
  findings --> chat
  consol --> chat
  chat --> freeze
  freeze --> publish
  publish --> draft
  draft --> human
```

## Conversation / findings mutation model

- Store chat turns separately from consolidations (new tables), keyed by PR + head SHA
  (and optionally consolidation id).
- Chat may propose edits to kept findings, stance, or `recommended_action`; apply only
  when the human accepts a turn’s patch.
- While chat is open, treat the consolidation report as draft: clear `ready_to_post`
  and `publish_body` if the frozen report changes.
- On freeze: write updated `body.md` / `meta.json` (or DB equivalents), then enqueue the
  same publish transform used today.
- Regenerating consolidation starts a new report and invalidates prior chat freeze for
  that consolidation.

## STT / TTS adapters

Keep speech out of the chat core. Behind narrow interfaces:

```text
SpeechToText.transcribe(audio) -> text
TextToSpeech.synthesize(text) -> audio
```

Swap providers (ElevenLabs, Gemini, Whisper, browser Web Speech, etc.) without changing
turn storage, consensus patches, or the publish pipeline. Voice is an input/output skin
on the same text chat.

## Non-goals (this future phase still)

- Auto-posting to GitHub
- Shipping voice in the consolidation/publish Phase 1 ship
- Replacing the Settings-editable publish prompt
- Changing review-run prompts to consume analysis (separate optional later)
