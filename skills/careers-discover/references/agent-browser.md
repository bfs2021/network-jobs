# agent-browser cheat sheet

`agent-browser` is optional and only needed for boards that render listings with
JavaScript. It is a **global tool**, not a project dependency: `network-jobs setup`
installs it when missing and upgrades it only when it is below
`agentBrowser.minVersion` in `package.json`. `network-jobs doctor` reports the
installed version.

## Read this first: commands are grouped

`agent-browser --help` lists nested subcommands under headings (`Network:`,
`Storage:`, `Tabs:`) that read like top-level commands. They are not:

```bash
agent-browser har start             # ✗ Unknown command: har
agent-browser network har start     # ✓
```

A first `Unknown command: X` means "try `<group> X`". Check
`agent-browser <group> --help` before concluding the build lacks the feature.

## Navigation and reading

```bash
agent-browser open <url>        # navigate (launches the browser on first use)
agent-browser read <url>        # agent-readable text of a page
agent-browser snapshot          # accessibility tree with @refs for clicking
agent-browser close             # shut the session down
```

## Interacting

```bash
agent-browser click @e12                     # click a snapshot ref
agent-browser find text "Load more" click    # click by visible text
agent-browser scroll down 800                # scroll (up|down|left|right [px])
agent-browser mouse wheel 2000               # wheel events, for lazy lists
agent-browser eval 'document.querySelectorAll("a[href*=job]").length'
```

`eval` is the cheapest way to count rendered cards or read a container's
`scrollHeight` before deciding whether pagination is even reachable.

## Network inspection (the useful part for careers pages)

```bash
agent-browser network requests                  # URL + method + status per call
agent-browser network requests --filter graphql  # narrow by pattern
agent-browser network requests --clear          # reset before a reload
```

`network requests` lists URLs and statuses but **not request bodies**, so it
tells you whether a board fetches a jobs API but not what it POSTs.

To capture bodies (form-encoded or JSON payloads, signed GraphQL variables),
record a HAR:

```bash
agent-browser network har start
agent-browser open "https://example.com/jobs"   # trigger the requests
agent-browser network har stop ./capture.har    # path belongs to stop, not start
```

Without an explicit path, `stop` writes to `~/.agent-browser/tmp/har/` and
prints where. `start` ignores a path argument.

## When to use it at all

Try `curl` first. Escalate to the browser only when the page returns an empty
shell, a JS-only root, or a soft bot wall — and stop after the documented budget
rather than retrying a hard wall. Record the version in the fetch-log
(`agentBrowserVersion`) whenever a browser run decides the outcome, so a later
run can tell a site change from a tool change.
