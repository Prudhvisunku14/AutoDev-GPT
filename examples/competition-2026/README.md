# LTID AI competition 2026: example projects

Four competition packages, one per prompt. Each folder describes the project's purpose, technology, architecture, commands and test results. The packages themselves are kept outside this public repository, so this folder holds their summaries and no starter-repository source, competition documents, knowledge-base records or credentials.

These projects were prepared independently of DevPilot AI's own runs: they are examples of the competition's tasks, not output of a DevPilot build.

| Prompt | Project | Technology | Offline tests, run 6 November 2026 |
|---|---|---|---|
| A | [Northstar Studio website](prompt-a-website/README.md) | Static HTML, CSS and JavaScript | Python: 10 passed, 5 browser checks skipped (Playwright not installed); Node: 6 of 6 passed. Same results for the Drive-aligned variant |
| B | [Synthetic temperature forecasting](prompt-b-weather/README.md) | Python 3 standard library CLI | 20 of 20 passed |
| C | [Document-grounded question answering](prompt-c-rag/README.md) | Python 3 standard library CLI | 32 of 32 passed |
| D | [Explicability and evaluation evidence](prompt-d-explicability/README.md) | Report and evidence pack | Bundles Prompt A (Drive-aligned), B and C, whose tests are above |

## How the tests were run

Each package was unpacked into an empty scratch folder outside the repository and tested with its own documented commands, offline, with Python 3.12.9 and Node.js 24.14.1:

```powershell
python -m unittest discover -s tests -p "test_*.py"   # every package
node --test tests/test_logic.mjs                      # Prompt A
```

No package was installed, no dependency was downloaded and no network service was contacted.

## Packages

| Package file | Contents |
|---|---|
| `PromptA_NorthstarStudio_Submission.zip` | Prompt A website, with tests, evidence logs and a draft PR description |
| `PromptB_Weather_Submission.zip` | Prompt B forecasting application, data, tests and evidence |
| `PromptD_Explicability_Evidence_Pack.zip` | Prompt D report and evidence, with `PromptA_Website_Drive_Aligned.zip`, `PromptB_Weather_Submission.zip` and `PromptC_RAG_Submission.zip` inside |
