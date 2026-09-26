
TRADING DOMAIN CHECKLIST (required, one entry per name in trading_checks):
look_ahead_bias, survivorship_bias, data_leakage, overfitting, multiple_hypothesis_testing, unrealistic_fills, commissions_fees, spread, slippage, liquidity_capacity, queue_position, execution_latency, sample_size, regime_dependence, parameter_sensitivity.
For each: ok (addressed convincingly), concern (a real risk; say what would settle it), or not_applicable (say why). A checklist full of "ok" for a proposal that never mentions fees or fills is a false ACCEPT. Any claimed edge without an executed test behind it is NEEDS_EXPERIMENT, not ACCEPT. A search over many configurations with no correction is a major problem at minimum.
