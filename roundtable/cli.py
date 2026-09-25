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
          full: bool = typer.Option(False, "--full", help="Print full prompts and responses.")):
    """Show every agent call in a run: who, what model, what it saw, what it produced, what it cost."""
    cfg = _load(config)
    store = _store_for(cfg)
    rows = store.calls(run_id)
    if not rows:
        console.print(f"no calls for run {run_id}")
        raise typer.Exit(1)
    for r in rows:
        console.rule(f"{r['id']}  {r['stage']}/{r['role']}  {r['provider']}/{r['model']}  attempt {r['attempt']}  "
                     f"{'valid' if r['valid'] else 'INVALID'}")
        console.print(f"in={r['input_tokens']} out={r['output_tokens']} cost={r['cost_usd']} "
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
