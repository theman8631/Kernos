# Live Smoke Test Results

**Date:** 2026-04-07 01:08 UTC
**Result:** 6/6 passed
**Provider:** openai-codex

### PASS: Basic response
Duration: 5113ms
Response: It’s 6:07 PM local time for you.

By the way — what should I call you?
- OK: Response not empty
- OK: No error message

### PASS: /dump check
Duration: 1284ms
Response: Context dumped to data/diagnostics/context_2026-04-07T01-07-32.txt
- OK: Response mentions dump

### PASS: Router works
Duration: 4677ms
Response: Hey — I’m good. Here and paying attention.

How are you doing?
- OK: Response not empty
- OK: No error message
- OK: Router fired

### PASS: Knowledge shaping
Duration: 8454ms
Response: Not much yet.

I know:
- you’re the verified owner of this Kernos instance
- we’re talking on Discord
- your standing preferences/rules include:
  - keep responses concise unless you ask for detail
  
- OK: Response not empty
- OK: No error message

### PASS: Tool surfacing
Duration: 8815ms
Response: I can do that, but I don’t currently have web search loaded here.

If you want, I can help you set up web search/browser access so I can look up pizza places near you. If you already have a city or ne
- OK: Response not empty
- OK: Tool surfacing logged

### PASS: Code execution
Duration: 8987ms
Response: 2^100 = 1267650600228229401496703205376
- OK: Response not empty
- OK: No error message

