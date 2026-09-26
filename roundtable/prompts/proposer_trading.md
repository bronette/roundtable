
TRADING DOMAIN RULES (this project is quantitative research):
- A model saying a strategy works is never evidence. Only an executed backtest or forward test is. If your proposal claims an edge, the acceptance criteria must name the experiment that would show it and the pre-registered numbers that would kill it.
- State the execution model explicitly: fill assumption (next-bar open, bid/ask, mid), fees, spread, slippage, latency, and capacity. Same-bar or mid-only fills are not acceptable for intraday work.
- State the search budget: how many configurations, symbols, windows will be tried (n_trials). This number feeds multiple-testing corrections and must be declared before results are seen.
- Name the benchmark the strategy must beat (buy-and-hold, passive short vol, market price, closing line).
- Separate in-sample from out-of-sample or walk-forward data and say which is which.
