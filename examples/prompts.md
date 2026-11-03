# Example prompts

Only **Python command-line applications** are built and verified so far. A good prompt names what the program does, its inputs and outputs,
and asks for unit tests. Use only the standard library unless you say otherwise: the sandbox that runs the tests has no network and no
`pip install`, so a project that needs a third-party package fails its tests.

```powershell
.\devpilot.cmd build "Create a Python command-line tool that converts a CSV file to JSON. Take the input and output paths as arguments, report bad rows clearly, and include unit tests."
```

More that suit the current engine:

- `Create a Python student attendance tracker that can add students, record daily attendance and calculate attendance percentages. Use only the standard library and include unit tests.`
- `Create a Python command-line expense splitter: add people and expenses, then show who owes whom and how much, with unit tests.`
- `Create a Python habit tracker that records daily habits and reports current and longest streaks, with unit tests.`
- `Create a weather forecast application in Python that reports temperature, humidity and wind speed for a city, using a replaceable weather data provider, with clear errors for unknown cities and unit tests that use a fake provider.`
- `Create a Python module that counts the words in a text, ignoring case, with unit tests.`

What will be declined (not built as something else): requests for React or other front ends, websites, web APIs (FastAPI, Flask, Django),
mobile apps and full-stack applications. Their verification runtimes have not passed a real end-to-end test yet.

## What a build costs

Before anything is sent, `devpilot build` shows the spending limit (default **$0.50** for one build, GPT and OpenCode together) and asks you
to type `approve 0.50`. The real cost is not known beforehand: it depends on the plan and on how many repairs the tests need. The limit is
enforced in code before every model request, and a build that reaches it stops and says so. Your OpenAI invoice is the authority on what
was billed. Raise or lower the limit with `--max-usd` (at most $2.00).

## Where the results go

A verified build is saved to `.\devpilot-output\<name>-<id>\` (the files) and `.\devpilot-output\<name>-<id>.zip`. The ZIP holds the source,
the tests, `DEVPILOT-HOW-TO-RUN.txt`, and `devpilot-verification\` (the report, and a manifest of what was included and what was left out,
such as `.env` files, keys and caches). A build that does not pass its tests produces no ZIP.
