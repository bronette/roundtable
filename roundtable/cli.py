"""roundtable CLI. M0: `providers`, `call`, `calls`."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from roundtable.config import ConfigError, load_config
from roundtable.pricing import Pricing
from roundtable.providers.base import ProviderError
from roundtable.schemas import Answer
from roundtable.store import Store

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Multi-model AI collaboration orchestrator.")
console = Console()


def _load(config_path: Path):
    try:
        return load_config(config_path)
    except ConfigError as e:
        console.print(f"[red]config error:[/red] {e}")
        raise typer.Exit(2)


def _store_for(cfg) -> Store:
    runs_dir = cfg.resolve_path(cfg.runs_dir)
    return Store(runs_dir / cfg.project.name / "roundtable.db")


@app.command()
def providers(config: Path = typer.Option("project.yaml", "--config", "-c")):
    """List configured providers and whether each can be used right now."""
    cfg = _load(config)
    t = Table("provider", "type", "backend", "status", "agents")
    status = cfg.check_providers()
    for name, p in cfg.providers.items():
        roles = ", ".join(r for r, a in cfg.agents.items() if a.provider == name)
        s = status[name]
        t.add_row(name, p.type, p.cli or p.sdk or "", f"[green]{s}[/green]" if s == "ok" else f"[red]{s}[/red]", roles)
    console.print(t)


@app.command()
def call(
    role: str,
    prompt: str,
    config: Path = typer.Option("project.yaml", "--config", "-c"),
    system: str = typer.Option("You are a careful assistant. Answer precisely.", "--system"),
    raw: bool = typer.Option(False, "--raw", help="No schema; print free text."),
):
    """Send one prompt to one configured agent role and log it. Smoke test for a provider."""
    from roundtable.agents import Agent

    cfg = _load(config)
    if role not in cfg.agents:
        console.print(f"[red]unknown role {role!r}; configured: {', '.join(cfg.agents)}[/red]")
        raise typer.Exit(2)
    pricing = Pricing.load(cfg.resolve_path(cfg.pricing) if cfg.pricing else None)
    store = _store_for(cfg)
    project_id = store.create_project(name=cfg.project.name, objective="adhoc call", requirements=[],
                                      config=cfg.model_dump(mode="json"))
    run_id = store.create_run(project_id=project_id, workspace_path="", stage="ADHOC")
    try:
        agent = Agent.from_config(role, cfg, pricing)
        res = agent.call(store=store, run_id=run_id, stage="ADHOC", system=system, prompt=prompt,
                         schema=None if raw else Answer)
    except ProviderError as e:
        store.finish_run(run_id, "halted_error", str(e))
        console.print(f"[red]{type(e).__name__}:[/red] {e}")
        raise typer.Exit(1)
    store.finish_run(run_id, "done")
    c = res.completion
    console.rule(f"{role} via {c.provider} / {c.model}  ({c.latency_ms} ms, attempt {res.attempts})")
    if res.output is not None:
        console.print_json(res.output.model_dump_json())
    else:
        console.print(c.text)
    u = c.usage
    cost = pricing.cost(c.model, u)
    console.print(
        f"tokens in={u.input_tokens} cached={u.cached_input_tokens} out={u.output_tokens} "
        f"reasoning={u.reasoning_tokens} | billing={agent.billing} "
        f"| priced={'$%.4f' % cost if cost is not None else 'n/a'} "
        f"| provider-reported={'$%.4f' % c.reported_cost_usd if c.reported_cost_usd is not None else 'n/a'}")
    console.print(f"logged: run {run_id} call {res.call_id} → {store.path}")


@app.command()
def calls(run_id: str, config: Path = typer.Option("project.yaml", "--config", "-c"),
          full: bool = typer.Option(False, "--full", help="Print full prompts and responses."),
          role: str | None = typer.Option(None, "--role"), stage: str | None = typer.Option(None, "--stage"),
          failed: bool = typer.Option(False, "--failed", help="Only invalid or errored attempts."),
          as_json: bool = typer.Option(False, "--json", help="One JSON object per line.")):
    """Show every agent call in a run: who, what model, what it saw, what it produced, what it cost."""
    cfg = _load(config)
    store = _store_for(cfg)
    rows = store.calls(run_id)
    rows = [r for r in rows if (not role or r["role"] == role) and (not stage or r["stage"] == stage) and (not failed or not r["valid"])]
    if not rows:
        console.print(f"no matching calls for run {run_id}")
        raise typer.Exit(1)
    if as_json:
        for r in rows:
            print(json.dumps(dict(r)))
        return
    for r in rows:
        console.rule(f"{r['id']}  {r['stage']}/{r['role']}  {r['provider']}/{r['model']}  attempt {r['attempt']}  "
                     f"{'valid' if r['valid'] else 'INVALID'}")
        cost = f"${r['cost_usd']:.4f} ({r['cost_source']})" if r["cost_usd"] is not None else "unknown"
        console.print(f"in={r['input_tokens']} out={r['output_tokens']} cost={cost} "
                      f"billing={r['billing']} latency={r['latency_ms']}ms refs={r['context_refs_json']}")
        if r["error"]:
            console.print(f"[red]error:[/red] {r['error']}")
        if full:
            console.print("[bold]messages[/bold]")
            for m in json.loads(r["messages_json"]):
                console.print(f"  [{m['role']}] {m['content']}")
            console.print("[bold]response[/bold]")
            console.print(r["parsed_json"] or r["response_text"] or "")
    u = store.usage(run_id)["total"]
    console.print(f"\ntotal: {u['calls']} calls, {u['tokens']} tokens, "
                  f"cost={'$%.4f' % u['cost_usd'] if u['cost_known'] else 'partly unknown'}")


if __name__ == "__main__":
    app()


@app.command()
def run(config: Path = typer.Argument("project.yaml"),
        autonomy: int | None = typer.Option(None, "--autonomy", help="Override the config's autonomy level (0-4).")):
    """Run the collaboration pipeline on the project described in CONFIG."""
    from roundtable.agents import Agent
    from roundtable.pipeline import Pipeline

    cfg = _load(config)
    if autonomy is not None:
        cfg.autonomy = autonomy
    pricing = Pricing.load(cfg.resolve_path(cfg.pricing) if cfg.pricing else None)
    status = cfg.check_providers()
    bad = {r: status[a.provider] for r, a in cfg.agents.items() if status[a.provider] != "ok"}
    if bad:
        for r, why in bad.items():
            console.print(f"[red]agent {r!r} cannot run:[/red] {why}")
        raise typer.Exit(2)
    try:
        agents = {role: Agent.from_config(role, cfg, pricing) for role in cfg.agents}
    except ProviderError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2)
    store = _store_for(cfg)
    console.print(f"[bold]{cfg.project.name}[/bold]  autonomy={cfg.autonomy}  budget: {cfg.budget.max_calls} calls / "
                  f"{cfg.budget.max_tokens} tok / ${cfg.budget.max_cost_usd:.2f} / {cfg.budget.max_seconds}s / {cfg.budget.max_rounds} rounds")

    def on_event(stage: str, text: str) -> None:
        console.print(f"[dim][{stage:<10}][/dim] {text}")

    pipe = Pipeline(cfg, store, agents, on_event=on_event)
    st = pipe.run(runs_dir=cfg.resolve_path(cfg.runs_dir))
    console.rule(f"run {st.run_id}: {st.status}")
    if st.synthesis:
        console.print(f"verdict: [bold]{st.synthesis.overall_verdict}[/bold]  {st.synthesis.what_was_built}")
    u = store.usage(st.run_id)["total"]
    console.print(f"{u['calls']} calls, {u['tokens']} tokens, cost={'$%.4f' % u['cost_usd'] if u['cost_known'] else 'partly unknown'}")
    console.print(f"report: {st.report_path}")


@app.command()
def show(run_id: str, config: Path = typer.Option("project.yaml", "--config", "-c")):
    """Print the report for a run."""
    from roundtable.report import build_report

    cfg = _load(config)
    store = _store_for(cfg)
    if not store.get_run(run_id):
        console.print(f"no run {run_id}")
        raise typer.Exit(1)
    console.print(build_report(store, run_id, None))


@app.command()
def runs(config: Path = typer.Option("project.yaml", "--config", "-c"), limit: int = 20):
    """List recent runs for the project's database."""
    cfg = _load(config)
    store = _store_for(cfg)
    t = Table("run", "started", "status", "stage", "round", "objective")
    for r in store.runs(limit):
        t.add_row(r["id"], r["started_at"], r["status"], r["stage"], str(r["round"]), (r["objective"] or "")[:60].strip())
    console.print(t)


@app.command()
def resume(run_id: str, config: Path = typer.Option("project.yaml", "--config", "-c"),
           max_calls: int | None = typer.Option(None, "--max-calls"), max_tokens: int | None = typer.Option(None, "--max-tokens"),
           max_cost: float | None = typer.Option(None, "--max-cost"), max_seconds: int | None = typer.Option(None, "--max-seconds")):
    """Restart a halted run at the stage that failed. Completed calls are not repeated or re-billed.
    Budget counters continue from the run's usage; raise a cap here if the halt was a budget."""
    from roundtable.agents import Agent
    from roundtable.pipeline import Pipeline

    cfg = _load(config)
    for k, v in (("max_calls", max_calls), ("max_tokens", max_tokens), ("max_cost_usd", max_cost), ("max_seconds", max_seconds)):
        if v is not None:
            setattr(cfg.budget, k, v)
    pricing = Pricing.load(cfg.resolve_path(cfg.pricing) if cfg.pricing else None)
    store = _store_for(cfg)
    if not store.get_run(run_id):
        console.print(f"no run {run_id}")
        raise typer.Exit(1)
    try:
        agents = {role: Agent.from_config(role, cfg, pricing) for role in cfg.agents}
    except ProviderError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2)

    def on_event(stage: str, text: str) -> None:
        console.print(f"[dim][{stage:<10}][/dim] {text}")

    try:
        st = Pipeline(cfg, store, agents, on_event=on_event).resume(run_id)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.rule(f"run {st.run_id}: {st.status}")
    if st.synthesis:
        console.print(f"verdict: [bold]{st.synthesis.overall_verdict}[/bold]  {st.synthesis.what_was_built}")
    u = store.usage(st.run_id)["total"]
    console.print(f"{u['calls']} calls, {u['tokens']} tokens, cost={'$%.4f' % u['cost_usd'] if u['cost_known'] else 'partly unknown'}")
    console.print(f"report: {st.report_path}")


@app.command("call-show")
def call_show(call_id: str, config: Path = typer.Option("project.yaml", "--config", "-c"),
              as_json: bool = typer.Option(False, "--json", help="Print the row as JSON.")):
    """Everything about one agent call: the exact messages, the reply, the command line, tokens, cost."""
    cfg = _load(config)
    store = _store_for(cfg)
    r = store.call(call_id)
    if not r:
        console.print(f"no call {call_id}")
        raise typer.Exit(1)
    row = dict(r)
    if as_json:
        console.print_json(json.dumps(row))
        return
    console.rule(f"{row['id']}  run {row['run_id']}  {row['stage']}/{row['role']}  attempt {row['attempt']}")
    console.print(f"provider={row['provider']} model={row['model']} billing={row['billing']} valid={bool(row['valid'])}")
    console.print(f"tokens in={row['input_tokens']} cached={row['cached_input_tokens']} out={row['output_tokens']} "
                  f"reasoning={row['reasoning_tokens']} latency={row['latency_ms']}ms")
    cost = f"${row['cost_usd']:.4f} ({row['cost_source']})" if row["cost_usd"] is not None else "unknown"
    console.print(f"cost={cost} reported={row['reported_cost_usd']} request_id={row['request_id']}")
    console.print(f"started={row['started_at']} finished={row['finished_at']} schema={row['schema_name']} refs={row['context_refs_json']}")
    if row["error"]:
        console.print(f"[red]error:[/red] {row['error']}")
    if row["argv_json"]:
        console.print("[bold]command[/bold]")
        console.print(" ".join(json.loads(row["argv_json"])))
    console.print("[bold]messages[/bold]")
    for m in json.loads(row["messages_json"]):
        console.rule(m["role"], style="dim")
        console.print(m["content"])
    console.rule("response", style="dim")
    console.print(row["parsed_json"] or row["response_text"] or "(none)")
