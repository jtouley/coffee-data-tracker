# Sealed test results, 2026-10-28

Model: **claude-haiku-5-5**, frozen on validation (see `eval/validation/RESULTS.md`) before this set was scored.
The rules baseline is `pred_rules.json` (commit c021723).

The 8 products the labelers marked unsure are left out of both scores, so each score covers 592 of the 600
products (298 kept by the rules, 294 dropped).

## Scores

`python score_sealed.py final pred_claude-haiku-5-5.json`

```
kept     n=298 kept-but-not-coffee=8 dropped-but-coffee=0 error 2.7% (95% CI 1.4%-5.2%)
dropped  n=294 kept-but-not-coffee=0 dropped-but-coffee=3 error 1.0% (95% CI 0.3%-3.0%)
weighted accuracy over 99,265 products: 98.27%
```

`python score_sealed.py final pred_rules.json`

```
kept     n=298 kept-but-not-coffee=32 dropped-but-coffee=0 error 10.7% (95% CI 7.7%-14.8%)
dropped  n=294 kept-but-not-coffee=0 dropped-but-coffee=7 error 2.4% (95% CI 1.2%-4.8%)
weighted accuracy over 99,265 products: 94.05%
```

## Token usage

Prediction run, 600 products, one call each:

```
wrote 600 predictions to eval/sealed/pred_claude-haiku-5-5.json; token usage {'input_tokens': 95076, 'cache_read_input_tokens': 417979, 'cache_creation_input_tokens': 1069, 'output_tokens': 4898}
```

Cached prefix (system prompt and definition): 857 tokens, from `messages.count_tokens`.
