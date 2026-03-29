"""
Autonomous AI Research Loop
============================
Iteratively generates parameter/logic hypotheses, backtests them, and commits
improvements using a git ratchet. Only commits when the OUT-OF-SAMPLE score
improves — meaning the hypothesis generalises to data the optimizer never saw.

Usage:
    python scripts/autoresearch.py [--model MODEL] [--iter N] [--max-fails N]
                                   [--allow-strategy-edits] [--batch]

Examples:
    # Default overnight run (200 iterations, Gemini Flash)
    python scripts/autoresearch.py

    # Allow logic-level changes to momentum.py as well
    python scripts/autoresearch.py --allow-strategy-edits

    # Use Claude to break through a local maxima
    python scripts/autoresearch.py --model anthropic/claude-3-5-sonnet-20241022 --iter 20

    # 50% cost saving via Gemini Batch API (overnight only)
    python scripts/autoresearch.py --batch
"""

import os
import sys
import json
import argparse
import subprocess
from pathlib import Path

from dotenv import load_dotenv
load_dotenv("config/.env")

import litellm
import utils.logger as log_setup
from utils.notion_logger import NotionLogger

logger = log_setup.get_logger("autoresearch")


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def extract_momentum_block(settings_text: str) -> str:
    """
    Extracts just the MOMENTUM dict from settings.py for the LLM prompt.
    Avoids sending broker keys, watchlist, and other static config to the model.

    Token savings: ~1,400 tokens → ~400 tokens per call (~67% reduction).
    """
    lines = settings_text.splitlines()
    in_block = False
    block = []
    brace_depth = 0
    for line in lines:
        if not in_block and line.strip().startswith("MOMENTUM = {"):
            in_block = True
        if in_block:
            block.append(line)
            brace_depth += line.count("{") - line.count("}")
            if brace_depth <= 0 and len(block) > 1:
                break
    return "\n".join(block)


def parse_dual_hypothesis(content: str) -> dict:
    """
    Parses LLM response for up to two labelled code blocks:
      - ```python config/settings.py ... ```   (required)
      - ```python strategies/momentum.py ... ``` (optional)

    Falls back to the first unlabelled ```python ... ``` block for settings.
    Returns: {"settings": <code or None>, "momentum": <code or None>}
    """
    result = {"settings": None, "momentum": None}

    # Try labelled blocks first (```python config/settings.py\n...)
    if "```python" in content:
        for segment in content.split("```python")[1:]:
            first_line = segment.split("\n")[0].strip()
            code = segment.split("```")[0].strip()
            if "config/settings.py" in first_line:
                # Strip the label line itself if present
                code_lines = segment.split("\n")
                if "config/settings.py" in code_lines[0]:
                    code = "\n".join(code_lines[1:]).split("```")[0].strip()
                result["settings"] = code
            elif "strategies/momentum.py" in first_line:
                code_lines = segment.split("\n")
                if "strategies/momentum.py" in code_lines[0]:
                    code = "\n".join(code_lines[1:]).split("```")[0].strip()
                result["momentum"] = code

    # Fallback: first unlabelled ```python block → settings
    if result["settings"] is None and "```python" in content:
        result["settings"] = content.split("```python")[1].split("```")[0].strip()
    elif result["settings"] is None and "```" in content:
        result["settings"] = content.split("```")[1].split("```")[0].strip()

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Backtest runner
# ═══════════════════════════════════════════════════════════════════════════

def run_backtest() -> dict:
    """
    Runs backtest.py with --eval-window 60 and returns OOS stats for the ratchet.
    Falls back to full-period stats if OOS window is empty (e.g. sparse data).
    """
    result = subprocess.run(
        ["python", "backtest.py", "--eval-window", "60"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.error(f"Backtest crash:\n{result.stderr}")
        return {"error": True}

    json_path = Path("data/backtest_results.json")
    if not json_path.exists():
        return {"error": True}

    data = json.loads(json_path.read_text())

    # Prefer OOS stats — ratchet only commits if unseen data improves
    oos = data.get("oos_stats", {})
    if oos and oos.get("total_trades", 0) > 0:
        stats = oos.copy()
        # score is pre-computed in oos_stats by backtest.py
    else:
        # Backward-compat fallback: compute score from full-period stats
        stats = data.get("stats", {}).copy()
        win_rate = stats.get("win_rate_pct", 0)
        pf = stats.get("profit_factor", 0)
        if pf == float("inf"):
            pf = 5.0
        total_trades = stats.get("total_trades", 0)
        penalty = min(1.0, total_trades / 10.0)
        stats["score"] = (win_rate * pf) * penalty

    # Surface OOS fields at top level for generate_hypothesis() prompt
    stats["oos_score"]         = stats.get("score", 0)
    stats["oos_win_rate_pct"]  = stats.get("win_rate_pct")
    stats["oos_profit_factor"] = stats.get("profit_factor")
    stats["oos_total_trades"]  = stats.get("total_trades")
    stats["oos_return_pct"]    = stats.get("return_pct")

    return stats


# ═══════════════════════════════════════════════════════════════════════════
# Hypothesis generation
# ═══════════════════════════════════════════════════════════════════════════

def generate_hypothesis(model: str, baseline: dict,
                        allow_strategy_edits: bool = False,
                        batch_mode: bool = False) -> dict:
    """
    Calls the LLM to propose a modified settings.py (and optionally momentum.py).

    Returns:
        {"settings": <new settings.py code or None>,
         "momentum": <new momentum.py code or None>}
    """
    program  = Path("research_program.md").read_text()
    settings = Path("config/settings.py").read_text()

    # Send only the MOMENTUM dict — avoids leaking broker keys / watchlist
    momentum_params = extract_momentum_block(settings)

    strategy_context     = ""
    strategy_instruction = ""
    if allow_strategy_edits:
        strategy_code = Path("strategies/momentum.py").read_text()
        strategy_context = (
            f"\n\nHere is the current `strategies/momentum.py`:\n"
            f"```python\n{strategy_code}\n```"
        )
        strategy_instruction = (
            "\n\nYou may also propose changes to `strategies/momentum.py` if you believe "
            "a logic-level change (new indicator, new condition, new exit rule) would improve "
            "performance. If so, wrap it in a second ```python strategies/momentum.py ... ``` block."
        )

    system_prompt = (
        f"{program}\n\n"
        f"Return ONLY Python source code blocks. No markdown prose.{strategy_instruction}"
    )

    user_prompt = f"""
Baseline OOS score: {baseline.get('oos_score', baseline.get('score', 0)):.2f}
OOS Win Rate: {baseline.get('oos_win_rate_pct')}%  |  OOS Profit Factor: {baseline.get('oos_profit_factor')}
OOS Trades: {baseline.get('oos_total_trades')}  |  OOS Return: {baseline.get('oos_return_pct')}%

Current MOMENTUM parameters:
```python
{momentum_params}
```
{strategy_context}

Output a modified `config/settings.py` in a ```python config/settings.py ... ``` block.
"""

    logger.info(f"Calling {model} to generate hypothesis...")
    try:
        call_kwargs = dict(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.7,
        )
        if batch_mode:
            call_kwargs["batch"] = True

        response = litellm.completion(**call_kwargs)
        content  = response.choices[0].message.content
        return parse_dual_hypothesis(content)
    except Exception as e:
        logger.error(f"LiteLLM Error: {e}")
        return {"settings": None, "momentum": None}


# ═══════════════════════════════════════════════════════════════════════════
# Alerting
# ═══════════════════════════════════════════════════════════════════════════

def alert_local_maxima(failures: int, baseline_score: float, model: str):
    """Sends a Notion alert when the script is stuck at a local maxima."""
    logger.warning(f"Hit local maxima after {failures} failures. Alerting Notion.")
    notion = NotionLogger()
    if notion.enabled:
        notion._create_entry_page(
            trade_id=8888,
            stock="AUTORESEARCH",
            action="OPEN",
            entry_price=baseline_score,
            quantity=failures,
            stop_loss=0,
            target_price=0,
            strategy="SYSTEM",
            reason=(
                f"LOCAL MAXIMA HIT. Model {model} struck a plateau at OOS Score "
                f"{baseline_score:.2f} after {failures} consecutive failures. "
                f"Awaiting manual override — try: "
                f"python scripts/autoresearch.py --model anthropic/claude-3-5-sonnet-20241022 --iter 20"
            ),
            mode="LIVE"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Autonomous AI parameter/logic optimisation loop with OOS ratchet."
    )
    parser.add_argument(
        "--model", type=str, default="gemini/gemini-1.5-flash",
        help="LiteLLM model string (default: gemini/gemini-1.5-flash)"
    )
    parser.add_argument(
        "--iter", type=int, default=200,
        help="Max iterations (default: 200)"
    )
    parser.add_argument(
        "--max-fails", type=int, default=15,
        help="Consecutive OOS failures before Notion alert (default: 15, ~7.5%% of 200)"
    )
    parser.add_argument(
        "--allow-strategy-edits", action="store_true", default=False,
        help=(
            "Allow the agent to also modify strategies/momentum.py. "
            "Disabled by default — enable when you trust the backtest is robust."
        )
    )
    parser.add_argument(
        "--batch", action="store_true", default=False,
        help=(
            "Use Gemini Batch API (50%% cost saving). Results are async — "
            "suitable for overnight runs. Not suitable for interactive use."
        )
    )
    args = parser.parse_args()

    logger.info(
        f"Starting AutoResearch | model={args.model} | iter={args.iter} | "
        f"max_fails={args.max_fails} | strategy_edits={args.allow_strategy_edits} | "
        f"batch={args.batch}"
    )

    baseline = run_backtest()
    if "error" in baseline:
        logger.error("Initial backtest failed. Cannot start.")
        sys.exit(1)

    best_score = baseline.get("oos_score", baseline.get("score", 0))
    logger.info(
        f"Baseline OOS Score: {best_score:.2f}  "
        f"(WR: {baseline.get('oos_win_rate_pct')}% | "
        f"PF: {baseline.get('oos_profit_factor')} | "
        f"Trades: {baseline.get('oos_total_trades')})"
    )

    consecutive_failures = 0
    target_settings = Path("config/settings.py")
    target_momentum = Path("strategies/momentum.py")

    for i in range(1, args.iter + 1):
        if consecutive_failures >= args.max_fails:
            alert_local_maxima(consecutive_failures, best_score, args.model)
            sys.exit(0)

        logger.info(f"\n--- Iteration {i}/{args.iter} ---")
        new_code = generate_hypothesis(
            model=args.model,
            baseline=baseline,
            allow_strategy_edits=args.allow_strategy_edits,
            batch_mode=args.batch,
        )

        if not new_code.get("settings"):
            consecutive_failures += 1
            continue

        # Write hypothesis files
        target_settings.write_text(new_code["settings"])
        if new_code.get("momentum") and args.allow_strategy_edits:
            target_momentum.write_text(new_code["momentum"])

        # Test hypothesis
        logger.info("Running backtest for hypothesis...")
        new_stats = run_backtest()

        if "error" in new_stats:
            logger.warning("Hypothesis crashed the backtest! Reverting.")
            subprocess.run(["git", "restore", "config/settings.py"])
            if args.allow_strategy_edits:
                subprocess.run(["git", "restore", "strategies/momentum.py"])
            consecutive_failures += 1
            continue

        new_score = new_stats.get("oos_score", new_stats.get("score", 0))
        logger.info(
            f"Hypothesis Result:\n"
            f"  OOS score:  {new_score:.2f}  "
            f"(WR {new_stats.get('oos_win_rate_pct')}%, "
            f"PF {new_stats.get('oos_profit_factor')}, "
            f"Trades {new_stats.get('oos_total_trades')})"
        )

        if new_score > best_score:
            logger.info(
                f"✅ OOS hypothesis improved! {best_score:.2f} → {new_score:.2f}. Committing."
            )
            best_score = new_score
            baseline   = new_stats
            consecutive_failures = 0

            subprocess.run(["git", "add", "config/settings.py"])
            if new_code.get("momentum") and args.allow_strategy_edits:
                subprocess.run(["git", "add", "strategies/momentum.py"])

            msg = (
                f"AutoResearch: OOS score {best_score:.2f} "
                f"(WR {new_stats.get('oos_win_rate_pct')}% | "
                f"PF {new_stats.get('oos_profit_factor')})"
            )
            subprocess.run(["git", "commit", "-m", msg])
        else:
            logger.info(
                f"❌ OOS score {new_score:.2f} did not beat baseline {best_score:.2f}. Reverting."
            )
            subprocess.run(["git", "restore", "config/settings.py"])
            if args.allow_strategy_edits:
                subprocess.run(["git", "restore", "strategies/momentum.py"])
            consecutive_failures += 1


if __name__ == "__main__":
    main()
