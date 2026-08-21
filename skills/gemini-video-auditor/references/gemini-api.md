# Gemini Developer API: key, models, pricing, and data boundaries

Documentation checked: **2026-08-19 UTC**. Pricing, quotas, model availability, SDK behavior, and data-use terms can change; re-check the official links before operational use.

## API key

Create a Gemini Developer API key in Google AI Studio:

- Key management: <https://aistudio.google.com/apikey>
- Official key guide: <https://ai.google.dev/gemini-api/docs/api-key>

The official SDK recognizes `GEMINI_API_KEY`, but this skill intentionally ignores that generic variable. It accepts only the dedicated `GEMINI_VIDEO_SKILL_KEY` and passes it explicitly to the SDK so a general runtime/chat key can never be selected silently.

Never publish a key. Store it in a local environment variable, secret manager, or a mode-`0600` environment file outside the repository. Restrict the key/project where Google Cloud controls permit it, rotate it if exposed, and never print auth errors verbatim when they may contain request details.

## Recommended free model

`gemini-2.5-flash`

Why it is the default:

- stable model identifier documented by Google;
- multimodal and video-capable;
- good price/performance and reasoning;
- standard Gemini Developer API free tier currently lists input and output as free of charge, subject to rate/quota and regional eligibility;
- compatible with the recovered workflow based on `google.genai`, Files API upload, and `models.generate_content`.

Free tier does not mean unlimited or suitable for confidential data. Google's pricing table currently marks free-tier submitted content as usable to improve Google products. Obtain organizational acceptance before uploading sensitive media.

## Optional paid models and current list price

Prices below are Gemini Developer API **standard** rates per 1 million tokens in USD, checked on 2026-08-19:

| Model | Use | Input | Output, including thinking tokens |
| --- | --- | ---: | ---: |
| `gemini-2.5-flash` | paid fast/default path | $0.30 for text/image/video; $1.00 for audio | $2.50 |
| `gemini-2.5-pro` | optional stronger reasoning/quality | $1.25 for prompts up to 200k tokens; $2.50 above 200k | $10.00 up to 200k; $15.00 above 200k |

The price of a video job is not a fixed amount per file. It depends on tokenized video/audio input, prompt size, output, thinking tokens, retries, and any additional service use. The receipt records the token counters returned by the API so an operator can apply the current price table. The script does not enable billing or switch models automatically.

A newer `gemini-3.7-flash` model is also listed with a free tier and time-bounded paid promotional pricing, but this skill keeps `gemini-2.5-flash` as the conservative compatibility default. Change models only after a dry-run and current model/API validation.

Official pricing: <https://ai.google.dev/gemini-api/docs/pricing>

## Files API and video behavior

Official references:

- Files API: <https://ai.google.dev/gemini-api/docs/files>
- Video understanding: <https://ai.google.dev/gemini-api/docs/video-understanding>
- Models: <https://ai.google.dev/gemini-api/docs/models>
- Thinking: <https://ai.google.dev/gemini-api/docs/thinking>

Current Files API documentation states:

- maximum **2 GB per file**;
- maximum **20 GB per project**;
- uploaded files are retained for up to **48 hours**;
- the Files API itself is available at no cost;
- explicit deletion is supported and is requested by this skill after every upload.

The pinned `google-genai` SDK accepts a seekable binary `IOBase` for `files.upload`. This skill uses that interface with a Linux kernel-sealed anonymous stream instead of a mutable filesystem pathname.

Gemini video understanding normally samples video at approximately **1 frame per second** by default. Rapid motion, brief screens, transient UI fields, and frame-level events may be missed. Human verification remains mandatory for operational, compliance, safety, or training authority.

## SDK pin

`scripts/requirements.txt` pins the direct dependency observed on PyPI at the documentation date:

```text
google-genai==2.18.1
```

PyPI: <https://pypi.org/project/google-genai/>

A direct pin limits accidental drift but does not make transitive dependencies immutable. Review and update the pin deliberately, run the included tests, and inspect dependency advisories before production use.
